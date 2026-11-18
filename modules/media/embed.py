#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""FFmpeg 字幕烧录、编码器探测，以及超时后的降级重试。

方法体与拆分前保持一致。会随调用变化的依赖（任务读写、取消检查）经
task_manager 门面解析，因此 ``patch.object(task_manager, "update_task")``
仍然生效。编码器参数和 FFmpeg 路径是无状态依赖，直接引用原模块。
"""

import json
import os
import shlex
import shutil
import subprocess

from modules.ffmpeg_manager import get_ffmpeg_path, get_ffprobe_path
from modules.pipeline.cancel import TaskCancelledError
from modules.video_encoder_params import (
    VUI_REPORT_NOTHING,
    VUI_REPORT_USER_NO_VUI,
    VUI_REPORT_USER_OWNS_CODEC,
    VUI_REPORT_USER_VUI,
    VUI_REPORT_WRITTEN,
    build_audio_params,
    build_encoder_params,
    custom_params_vui_report,
    format_quality_value,
    normalize_color_metadata,
    normalize_cpu_codec,
    parse_encoder_config,
    private_param_option_name,
    recommend_quality,
    resolve_color_metadata,
)

PROCESS_TERMINATE_WAIT_SECONDS = 5
# 字幕烧录超时估算参数（见 TaskManager._estimate_embed_timeout）。
EMBED_TIMEOUT_MIN_SECONDS = 1800
EMBED_TIMEOUT_MAX_SECONDS = 10800
# libx265 的编码耗时实测约为同 preset 下 libx264 的 5 倍（仓库自带 FFmpeg
# N-123313，带 subtitles 滤镜的真实烧录，1080p30 20s：veryfast 1.70s -> 8.44s）。
# 若沿用 x264 的估算，长视频会被超时强杀；而 CPU 路径失败后只剩「x265 -> x264」
# 一级降级，被超时杀掉时连这一级也救不回成片，只能整任务失败。故按实测倍数放大。
EMBED_TIMEOUT_X265_FACTOR = 5.0
EMBED_TIMEOUT_MAX_SECONDS_X265 = 28800


def _facade_call(name):
    def _call(*args, **kwargs):
        import modules.task_manager as task_manager
        return getattr(task_manager, name)(*args, **kwargs)

    _call.__name__ = name
    _call.__qualname__ = name
    return _call


get_task = _facade_call("get_task")
update_task = _facade_call("update_task")
_raise_if_cancelled = _facade_call("_raise_if_cancelled")
is_task_cancelled = _facade_call("is_task_cancelled")
_as_bool = _facade_call("_as_bool")


class _TaskStatesProxy:
    def __getitem__(self, key):
        import modules.task_manager as task_manager
        return task_manager.TASK_STATES[key]

    def __getattr__(self, name):
        import modules.task_manager as task_manager
        return getattr(task_manager.TASK_STATES, name)


TASK_STATES = _TaskStatesProxy()


class MediaEmbedMixin:
    
    _KNOWN_HW_ENCODER_ERROR_PATTERNS = (
        # 软件编码器：libx265 不一定被编进用户的 FFmpeg（自备构建常见
        # --disable-libx265）。登记它是为了让这条错误被识别为「已知编码器错误」
        # （日志归因、hw_error_detected 的判定都读它）。
        # 注意：libx265 缺失时的实际降级能力**不**依赖这条模式 —— CPU 路径的
        # 降级级由 `_resolve_embed_retry_stages` 在 encoder == 'cpu' 时无条件返回，
        # 把它从这里删掉，`cpu`/`x265` 的降级链完全不变（有测试锁定这一点）。
        'Unknown encoder "libx265"',
        "Unknown encoder 'libx265'",
        # NVENC (NVIDIA)
        'Unknown encoder "h264_nvenc"',
        'Unknown encoder "hevc_nvenc"',
        "Unknown encoder 'h264_nvenc'",
        "Unknown encoder 'hevc_nvenc'",
        'No NVENC capable devices found',
        'Device not present',
        'No such filter: "subtitles"',
        "No such filter: 'subtitles'",
        'Cannot load libnvidia-encode',
        'Failed to query maximum',
        'Cannot find a CUDA capable',
        'CUDA_ERROR_',
        'nvcuda.dll',
        'libnvcuvid',
        'nvenc_load_functions',
        'Could not initialize',
        'OpenEncodeSession failed',
        'out of memory',
        'InitializeEncoder failed',
        'GPU is not in the list',
        # QSV (Intel)
        'Unknown encoder "h264_qsv"',
        'Unknown encoder "hevc_qsv"',
        "Unknown encoder 'h264_qsv'",
        "Unknown encoder 'hevc_qsv'",
        'MFXInit',
        'Error initializing an internal MFX session',
        'Error creating a MFX session',
        'Error initializing the MFX video session',
        'Unable to find',
        'No device',
        'Device creation failed',
        'iHD_drv_video.so',
        'i965_drv_video.so',
        # AMF (AMD Windows)
        'Unknown encoder "h264_amf"',
        'Unknown encoder "hevc_amf"',
        "Unknown encoder 'h264_amf'",
        "Unknown encoder 'hevc_amf'",
        'AMF initialization failed',
        'AMFContext',
        'AMF failed',
        'amfrt64.dll',
        # VAAPI (AMD/Intel Linux)
        'Unknown encoder "h264_vaapi"',
        'Unknown encoder "hevc_vaapi"',
        "Unknown encoder 'h264_vaapi'",
        "Unknown encoder 'hevc_vaapi'",
        'Error creating a VAAPI device',
        'Failed to create VAAPI device',
        'Failed to initialise VAAPI connection',
        'No VA display found for device',
        'vaInitialize',
        'vaDeriveImage',
        '/dev/dri/renderD128',
        'libva',
        'hwupload',
        'A hardware device reference is required to upload frames to.',
        'Failed to configure output pad on Parsed_hwupload',
        'Driver does not support the required nvenc API version',
        'The minimum required Nvidia driver for nvenc is',
        'libamfrt64.so',
        "failed to load library 'libX11.so.6'",
        'cannot open shared object file',
        'Assertion in generated code',
        # 编码器不接受某个选项：多见于老版本 FFmpeg 或老驱动不支持
        # 新引入的质量增强参数（AQ / 前瞻 / 多遍 / blbrc 等）。
        # 命中后走「关闭质量增强 → CPU」的分级降级，而不是直接掉到 CPU。
        'Unrecognized option',
        'Option not found',
        'Error setting option',
        'Error applying encoder options',
        'Error parsing options',
        'Invalid option',
    )

    # 「参数类」错误的子集：只有关闭质量增强才可能治好。
    # 其余已知硬件错误属于设备/驱动类（设备被占用、驱动崩溃、显存不足、
    # 编码器不存在等），关闭增强不会改变任何东西 —— 必须先按同一硬编器重跑
    # 一遍整部视频再轮到 CPU，纯属白跑（长视频代价极高）。
    _HW_OPTION_ERROR_PATTERNS = (
        'Unrecognized option',
        'Option not found',
        'Error setting option',
        'Error applying encoder options',
        'Error parsing options',
        'Invalid option',
    )

    _HW_PROBE_DEFAULT_SIZE = '640x480'
    _HW_PROBE_RETRY_SIZE = '1280x720'
    _HW_PROBE_DURATION = '0.1'
    _HW_PROBE_DIMENSION_ERROR_PATTERNS = (
        'frame dimension less than the minimum supported value',
        'invalid param (8)',
    )

    @staticmethod
    def _short_error_text(error_output, limit=240):
        text = str(error_output or '').replace('\n', ' ').strip()
        if not text:
            return ''
        return text[:limit]

    @staticmethod
    def _summarize_cmd(cmd, limit=300):
        summary = ' '.join(str(x) for x in (cmd or []))
        if len(summary) <= limit:
            return summary
        return summary[:limit]

    @classmethod
    def _is_probe_dimension_error(cls, error_output):
        if not error_output:
            return False
        text = str(error_output).lower()
        return any(pattern in text for pattern in cls._HW_PROBE_DIMENSION_ERROR_PATTERNS)

    @classmethod
    def _should_keep_nvidia_preference_on_probe_failure(cls, nvenc_listed, nvidia_device_visible, detect_error):
        return bool(
            nvenc_listed
            and nvidia_device_visible
            and cls._is_probe_dimension_error(detect_error)
        )

    @classmethod
    def _build_hw_probe_cmd(cls, ffmpeg_bin, encoder_name, probe_size=None):
        size = probe_size or cls._HW_PROBE_DEFAULT_SIZE
        color_src = f"color=c=black:s={size}:d={cls._HW_PROBE_DURATION}"
        encoder_name_lower = str(encoder_name or '').lower().strip()
        test_cmd = [ffmpeg_bin, '-hide_banner', '-loglevel', 'error']

        if encoder_name_lower == 'h264_vaapi':
            test_cmd.extend([
                '-vaapi_device', '/dev/dri/renderD128',
                '-f', 'lavfi', '-i', color_src,
                '-vf', 'format=nv12,hwupload',
                '-frames:v', '1',
                '-c:v', 'h264_vaapi',
                '-qp', '23',
            ])
        elif encoder_name_lower == 'hevc_vaapi':
            test_cmd.extend([
                '-vaapi_device', '/dev/dri/renderD128',
                '-f', 'lavfi', '-i', color_src,
                '-vf', 'format=nv12,hwupload',
                '-frames:v', '1',
                '-c:v', 'hevc_vaapi',
                '-qp', '23',
                '-profile:v', 'main',
            ])
        elif encoder_name_lower == 'h264_qsv':
            test_cmd.extend([
                '-f', 'lavfi', '-i', color_src,
                '-frames:v', '1',
                '-c:v', 'h264_qsv',
                '-global_quality', '23',
                '-look_ahead', '0',
                '-pix_fmt', 'nv12',
            ])
        elif encoder_name_lower == 'hevc_qsv':
            test_cmd.extend([
                '-f', 'lavfi', '-i', color_src,
                '-frames:v', '1',
                '-c:v', 'hevc_qsv',
                '-global_quality', '23',
                '-look_ahead', '0',
                '-profile:v', 'main',
                '-pix_fmt', 'nv12',
            ])
        elif encoder_name_lower == 'h264_amf':
            test_cmd.extend([
                '-f', 'lavfi', '-i', color_src,
                '-frames:v', '1',
                '-c:v', encoder_name,
                '-usage', 'transcoding',
                '-quality', 'balanced',
                '-rc', 'cqp',
                '-qp_i', '23',
                '-qp_p', '23',
                '-qp_b', '23',
            ])
        elif encoder_name_lower == 'hevc_amf':
            test_cmd.extend([
                '-f', 'lavfi', '-i', color_src,
                '-frames:v', '1',
                '-c:v', encoder_name,
                '-usage', 'transcoding',
                '-quality', 'balanced',
                '-rc', 'qvbr',
                '-qvbr_quality_level', '24',
                '-profile:v', 'main',
            ])
        elif encoder_name_lower == 'hevc_nvenc':
            test_cmd.extend([
                '-f', 'lavfi', '-i', color_src,
                '-frames:v', '1',
                '-c:v', encoder_name,
                '-preset', 'p4',
                '-rc:v', 'vbr',
                '-cq:v', '23',
                '-profile:v', 'main',
            ])
        elif 'nvenc' in encoder_name_lower:
            test_cmd.extend([
                '-f', 'lavfi', '-i', color_src,
                '-frames:v', '1',
                '-c:v', encoder_name,
                '-preset', 'p4',
                '-rc:v', 'vbr',
                '-cq:v', '23',
            ])
        else:
            test_cmd.extend([
                '-f', 'lavfi', '-i', color_src,
                '-frames:v', '1',
                '-c:v', encoder_name,
            ])

        test_cmd.extend(['-f', 'null', '-'])
        return test_cmd

    @classmethod
    def _probe_hw_encoder_availability(cls, ffmpeg_bin, encoder_name):
        attempts = [cls._HW_PROBE_DEFAULT_SIZE]
        last_meta = None
        last_error = ''

        for idx, probe_size in enumerate(attempts):
            probe_retry = idx > 0
            test_cmd = cls._build_hw_probe_cmd(
                ffmpeg_bin=ffmpeg_bin,
                encoder_name=encoder_name,
                probe_size=probe_size,
            )
            test_result = subprocess.run(
                test_cmd,
                capture_output=True,
                text=True,
                encoding='utf-8',
                errors='replace',
                timeout=15
            )
            err_msg = (test_result.stderr or test_result.stdout or '').strip()
            if not err_msg and test_result.returncode != 0:
                err_msg = '未知错误'
            last_error = err_msg
            probe_error_short = cls._short_error_text(err_msg)
            last_meta = {
                'probe_size': probe_size,
                'probe_retry': probe_retry,
                'probe_returncode': test_result.returncode,
                'probe_error_short': probe_error_short,
                'probe_cmd_summary': cls._summarize_cmd(test_cmd),
            }

            if test_result.returncode == 0:
                return True, '', last_meta

            if idx == 0 and cls._is_probe_dimension_error(err_msg):
                attempts.append(cls._HW_PROBE_RETRY_SIZE)

        if not last_meta:
            last_meta = {
                'probe_size': cls._HW_PROBE_DEFAULT_SIZE,
                'probe_retry': False,
                'probe_returncode': -1,
                'probe_error_short': '未知错误',
                'probe_cmd_summary': '',
            }
        return False, last_error or '未知错误', last_meta

    def _parse_custom_video_params(self, task_logger):
        """解析自定义视频参数，解析失败时回退默认策略。"""
        enabled = _as_bool(self.config.get('VIDEO_CUSTOM_PARAMS_ENABLED', False))
        custom_params = str(self.config.get('VIDEO_CUSTOM_PARAMS', '')).strip()
        if not enabled or not custom_params:
            return None
        try:
            params = shlex.split(custom_params)
            if not params:
                return None
            return params
        except ValueError as e:
            task_logger.warning(f"自定义视频参数解析失败，回退默认编码参数: {e}")
            return None

    @staticmethod
    def _coerce_int(value):
        try:
            if value is None or value == '':
                return None
            return int(value)
        except (TypeError, ValueError):
            return None

    @classmethod
    def _select_audio_target_bitrate(cls, input_bit_rate):
        bit_rate = cls._coerce_int(input_bit_rate)
        if bit_rate is None or bit_rate <= 0:
            return '128k'
        if bit_rate >= 192000:
            return '192k'
        if bit_rate >= 160000:
            return '160k'
        if bit_rate >= 128000:
            return '128k'
        if bit_rate >= 96000:
            return '96k'
        return '64k'

    @classmethod
    def _build_audio_transcode_params(cls, audio_info):
        """统一音频转码参数，AAC 优先直拷，其他编码按源码率上限转 AAC。

        实现统一收敛到 video_encoder_params.build_audio_params，保留本方法作为
        类内兼容入口（既有测试与调用方签名不变）。
        """
        return build_audio_params(audio_info)

    @staticmethod
    def _estimate_embed_timeout(video_duration, cpu_codec=None):
        """根据视频时长估算 FFmpeg 超时时间（秒）。

        x264 路径的估算与历史完全一致（时长 * 3，超过 30 分钟则 * 2，下限 1800，
        上限 10800）。x265 路径按 EMBED_TIMEOUT_X265_FACTOR 放大估算与上限：
        libx265 实测比同 preset 的 libx264 慢约 5 倍，沿用 x264 的预算会让长视频
        在编码中途被强杀；而且首轮就把 overall_deadline 用完时，后续降级阶段会被
        「预算已耗尽」直接跳过 —— 连 x265 -> x264 那一级都轮不到。

        **下限同为 5 倍**（这是有意为之）：10 分钟素材的 x265 预算是 9000s 而不是
        1800s。取向保守 —— 宁可让卡住的 ffmpeg 多等，也不要在编码中途杀掉一个本来
        能跑完的任务。

        ``cpu_codec`` 只描述**这个阶段实际使用的**软编码器：硬编回退到 CPU 时，
        调用点必须先按 CPU 阶段的编码器再算一次（见 _embed_subtitle_in_video 的
        _stage_budget），否则 x265 阶段会拿到 x264 的预算。
        """
        if video_duration:
            rate = 3 if video_duration < 1800 else 2
            estimated_time = video_duration * rate
        else:
            estimated_time = 3600

        if normalize_cpu_codec(cpu_codec) == 'x265':
            estimated_time *= EMBED_TIMEOUT_X265_FACTOR
            ceiling = EMBED_TIMEOUT_MAX_SECONDS_X265
        else:
            ceiling = EMBED_TIMEOUT_MAX_SECONDS

        return int(max(EMBED_TIMEOUT_MIN_SECONDS, min(estimated_time, ceiling)))

    @staticmethod
    def _build_embed_ffmpeg_cmd(
        ffmpeg_bin,
        input_video,
        vf_filter,
        vparams,
        aparams,
        output_video,
        color_params=None,
    ):
        """构建统一的 FFmpeg 转码命令。"""
        return [
            ffmpeg_bin, '-y',
            '-i', input_video,
            '-vf', vf_filter,
            *vparams,
            # 色彩元数据必须显式写出，否则播放器会按默认色域解释导致偏色
            *(color_params or []),
            *aparams,
            '-movflags', '+faststart',
            '-progress', 'pipe:1',
            output_video
        ]

    @classmethod
    def _is_known_hw_encoder_error(cls, error_output):
        """判断是否命中已知硬编/滤镜错误，触发 CPU 回退。"""
        if not error_output:
            return False
        error_lower = error_output.lower()
        return any(err.lower() in error_lower for err in cls._KNOWN_HW_ENCODER_ERROR_PATTERNS)

    @classmethod
    def _is_hw_option_error(cls, error_output):
        """判断失败是否属于「编码器不接受某个选项」这一类。

        这一类才值得先关掉质量增强、用同一硬编器重试；设备/驱动类错误关掉增强
        没有任何作用，应先走 CPU（见 ``_resolve_embed_retry_stages``）。
        """
        if not error_output:
            return False
        error_lower = error_output.lower()
        return any(err.lower() in error_lower for err in cls._HW_OPTION_ERROR_PATTERNS)

    @staticmethod
    def _resolve_embed_retry_stages(actual_encoder, hw_quality_boost, hw_error_detected,
                                    hw_option_error=None, cpu_codec=None):
        """决定字幕烧录失败后的降级阶段顺序。

        硬件编码器失败时先尝试「关闭质量增强后用同一编码器重试」，保住硬件加速；
        只有在仍失败时才退回 CPU。若当前已经是 CPU 编码，重跑同样的命令必然
        同样失败（失败原因在滤镜或输入而不在编码器），因此不再重试 —— 这能避免
        为一个失败的长视频白跑一遍完整转码。

        ``hw_quality_boost``（是否开了质量增强）与 ``hw_option_error``（失败是否
        属于「编码器不接受某个选项」）共同决定是否需要 ``hw_no_boost`` 这一级：

        - 没开增强 → 没有可关的东西，直接 CPU；
        - 开了增强，但错误是设备/驱动类（设备被占用、驱动崩溃、显存不足、
          编码器不存在）→ 关掉增强也治不好，直接 CPU，避免按同一硬编器把整部
          视频白跑一遍；
        - 开了增强且错误是参数类，或错误文本完全未知 → 先关增强用同一硬编器
          重试，保留硬件加速。

        ``hw_option_error=None`` 时按旧语义处理（已知硬件错误即不值得关增强重试），
        便于既有调用点与测试渐进迁移。

        ``cpu_codec`` 是 CPU 软编码器取值（'x264' / 'x265'）。当用户选的软编码器是
        x265 时，CPU 路径仍有一级可降：改用 libx264 重跑。libx265 不一定被编进
        用户的 FFmpeg，切换实现时也可能带上不被接受的参数，而 libx264 在
        --enable-gpl 构建里几乎是必然存在的。x264 已是最底层，没有更下一级。

        **硬编回退到 CPU 时同样要带上这一级**：真实场景里降级链被激活的最常见原因
        就是硬编失败，而那条路径上的 CPU 阶段用的仍是用户配置的 x265 —— 若 x265
        不可用，此前没有任何下一级，整任务失败（与 README 承诺的「缺失时会自动
        降级为 libx264 并继续烧录」相反）。

        返回 'hw_no_boost' / 'cpu' / 'cpu_x264' 组成的列表，按执行顺序排列。
        """
        encoder = str(actual_encoder or '').strip().lower()
        codec = normalize_cpu_codec(cpu_codec)
        if encoder not in ('nvidia', 'intel', 'amd'):
            if encoder == 'cpu' and codec == 'x265':
                return ['cpu_x264']
            return []
        stages = []
        if hw_quality_boost:
            if hw_option_error is None:
                # 旧语义：不知道细分类型，按「已知硬件错误即不值得关增强」处理
                worth_disabling_boost = not bool(hw_error_detected)
            else:
                # 三种情形必须区分：
                #   参数类错误（hw_option_error）→ 关增强有可能治好，值得一试；
                #   已知设备/驱动类错误（hw_error_detected 且非参数类）→ 关增强无效；
                #   完全未知的错误文本（两者皆假）→ 无法判断，仍先试关增强，
                #     避免把「只是不认识某个新参数」误判成设备故障而直接掉到 CPU。
                worth_disabling_boost = bool(hw_option_error) or not bool(hw_error_detected)
            if worth_disabling_boost:
                stages.append('hw_no_boost')
        stages.append('cpu')
        if codec == 'x265':
            stages.append('cpu_x264')
        return stages

    @classmethod
    def _embed_stage_budget(cls, video_duration, cpu_codec, stage, first_stage_budget):
        """某个降级阶段按其**实际编码器**估算的预算（秒）。

        硬编回退到 CPU 时，如果这一级要跑 libx265，就必须拿到 x265 的预算（实测慢
        约 5 倍），否则长视频会在编码中途被强杀，连 x265 -> x264 那一级都轮不到
        （M2 的另一半：README 承诺「超时预算会按同一倍数放大」，而该路径此前沿用
        x264 预算）。``cpu_x264`` 阶段反而更快，用 x264 预算即可。
        """
        if stage == 'cpu':
            codec = cpu_codec
        elif stage == 'cpu_x264':
            codec = 'x264'
        else:
            # 同编码器关增强重试：沿用首轮预算（调用点已自带 300s 下限，这里再兜一次
            # 让本函数的返回值恒有下限，便于独立校验）。
            return max(300.0, float(first_stage_budget))
        return max(300.0, float(cls._estimate_embed_timeout(video_duration, codec)))

    @staticmethod
    def _drop_same_encoder_stage_after_timeout(retry_stages):
        """超时失败后剔除「关增强、同编码器重试」这一级。

        超时的含义是「这份活在这个编码器上跑不完」，同编码器关增强重试对长视频
        通常只是把整片时间再花一次（最现实的失败类型反而拿到最差的降级策略）。
        因此超时只保留「换编码器」的那几级（CPU / cpu_x264）。
        """
        try:
            return [str(stage) for stage in (retry_stages or []) if str(stage) != 'hw_no_boost']
        except Exception:
            return []

    @staticmethod
    def _describe_hw_failure(actual_encoder, *, timed_out=False, hw_option_error=False,
                             hw_error_detected=False, boost_enabled=True, returncode=None):
        """硬编失败的降级归因，供日志与排查使用（纯函数）。

        返回值是稳定的语义键：``timeout`` / ``option_error`` /
        ``option_error_no_boost`` / ``device_error`` / ``unknown``。

        ``option_error_no_boost`` 存在的意义：质量增强本来就没开时，
        「先关闭质量增强后用同一编码器重试」是空动作，照搬那句话会把人带偏。
        """
        if timed_out:
            return 'timeout'
        if hw_option_error:
            return 'option_error' if boost_enabled else 'option_error_no_boost'
        if hw_error_detected:
            return 'device_error'
        return 'unknown'

    @staticmethod
    def _finalize_embedded_video_output(temp_output_path, final_output_path):
        """优先原子替换输出文件，失败时回退复制。"""
        try:
            os.replace(temp_output_path, final_output_path)
        except OSError:
            shutil.copy2(temp_output_path, final_output_path)
            try:
                os.remove(temp_output_path)
            except Exception:
                pass

    def _embed_subtitle_in_video(self, task_id, video_path, subtitle_path, task_logger):
        """使用FFmpeg将字幕嵌入视频（修复版本 - 添加超时机制）"""
        # 保存当前状态，稍后恢复
        task_before_encoding = get_task(task_id)
        previous_status = task_before_encoding['status'] if task_before_encoding else TASK_STATES['TRANSLATING_SUBTITLE']
        
        try:
            # subprocess imported at module level
            import os
            import tempfile
            import shutil
            import re
            import time
            import threading
            import queue

            # 仅使用项目内置 ffmpeg/ffprobe，如缺失仅在 Windows 上尝试下载作为兜底
            ffmpeg_bin = get_ffmpeg_path(logger=task_logger)
            if not ffmpeg_bin or not os.path.exists(ffmpeg_bin):
                task_logger.error("未找到项目内置 FFmpeg，无法进行字幕嵌入。请确认仓库自带的 ffmpeg/ 目录完整（可重新解压或手动放置二进制）。")
                update_task(task_id, upload_progress=None, status=previous_status, silent=True)
                return None
            ffprobe_bin = get_ffprobe_path(ffmpeg_path=ffmpeg_bin, logger=task_logger)
            if not ffprobe_bin:
                task_logger.error("未能定位 ffprobe，无法继续字幕嵌入")
                update_task(task_id, upload_progress=None, status=previous_status, silent=True)
                return None
            
            # 生成嵌入字幕后的视频文件路径
            video_dir = os.path.dirname(video_path)
            video_name = os.path.splitext(os.path.basename(video_path))[0]
            embedded_video_path = os.path.join(video_dir, f"{video_name}_with_subtitle.mp4")
            
            task_logger.info("开始将字幕嵌入视频...")
            # 仅在调试时输出详细路径信息
            task_logger.debug(f"视频路径: {video_path}")
            task_logger.debug(f"字幕路径: {subtitle_path}")
            
            # 设置任务状态为转码视频中
            update_task(task_id, status=TASK_STATES['ENCODING_VIDEO'])
            _raise_if_cancelled(task_id, task_logger)
            
            def _format_bitrate_for_log(bit_rate):
                bit_rate_int = self._coerce_int(bit_rate)
                if bit_rate_int is None or bit_rate_int <= 0:
                    return 'unknown'
                if bit_rate_int >= 1000000:
                    return f"{bit_rate_int / 1000000:.2f}Mbps"
                return f"{bit_rate_int / 1000:.0f}kbps"

            def _format_size_for_log(size_bytes):
                if not isinstance(size_bytes, int) or size_bytes < 0:
                    return 'unknown'
                if size_bytes >= 1024 ** 3:
                    return f"{size_bytes / (1024 ** 3):.2f}GB"
                return f"{size_bytes / (1024 ** 2):.2f}MB"

            def _log_output_media_summary(output_path):
                try:
                    output_video_info = self._get_video_stream_info(output_path, task_logger)
                    output_audio_info = self._get_audio_stream_info(output_path, task_logger)
                    output_size_bytes = os.path.getsize(output_path) if os.path.exists(output_path) else None
                    size_ratio_text = 'unknown'
                    if (
                        isinstance(output_size_bytes, int)
                        and isinstance(input_size_bytes, int)
                        and input_size_bytes > 0
                    ):
                        size_ratio_text = f"{output_size_bytes / input_size_bytes:.2f}x"
                    task_logger.info(
                        "输出媒体摘要: video_codec=%s, video_bitrate=%s, audio_codec=%s, audio_bitrate=%s, "
                        "file_size=%s, size_ratio=%s",
                        output_video_info.get('codec_name') or 'unknown',
                        _format_bitrate_for_log(output_video_info.get('bit_rate')),
                        output_audio_info.get('codec_name') or 'unknown',
                        _format_bitrate_for_log(output_audio_info.get('bit_rate')),
                        _format_size_for_log(output_size_bytes),
                        size_ratio_text,
                    )
                except Exception as e:
                    task_logger.warning(f"记录输出媒体摘要失败: {e}")

            # 获取视频时长和流信息用于计算进度与参数
            video_duration = self._get_video_duration(video_path, task_logger)
            stream_info = self._get_video_stream_info(video_path, task_logger)
            input_width = stream_info.get('width') or 0
            input_height = stream_info.get('height') or 0
            input_fps = stream_info.get('fps') or 30
            input_video_codec = str(stream_info.get('codec_name') or '').strip().lower()
            input_video_bit_rate = self._coerce_int(stream_info.get('bit_rate'))
            input_pix_fmt = stream_info.get('pix_fmt') if isinstance(stream_info, dict) else None
            input_size_bytes = os.path.getsize(video_path) if os.path.exists(video_path) else None
            # 探测音频信息（用于跟随原视频）
            audio_info = self._get_audio_stream_info(video_path, task_logger)
            input_audio_codec = str(audio_info.get('codec_name') or '').strip().lower()
            input_audio_bit_rate = self._coerce_int(audio_info.get('bit_rate'))
            # GOP 取 2 秒一关键帧
            gop = max(24, int(round(2 * input_fps)))
            # HEVC 适合更长 GOP，4 秒可提升压缩效率 5-10%
            gop_hevc = max(48, int(round(4 * input_fps)))
            task_logger.info(
                "输入媒体摘要: video_codec=%s, video_bitrate=%s, audio_codec=%s, audio_bitrate=%s, file_size=%s",
                input_video_codec or 'unknown',
                _format_bitrate_for_log(input_video_bit_rate),
                input_audio_codec or 'unknown',
                _format_bitrate_for_log(input_audio_bit_rate),
                _format_size_for_log(input_size_bytes),
            )
            task_logger.info(f"GOP 设置: H.264={gop} (2秒), HEVC={gop_hevc} (4秒)")

            def _ffmpeg_has_filter(filter_name: str) -> bool:
                try:
                    # subprocess imported at module level
                    result = subprocess.run(
                        [ffmpeg_bin, '-hide_banner', '-filters'],
                        capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=20
                    )
                    if result.returncode == 0 and filter_name in result.stdout:
                        return True
                except Exception:
                    pass
                return False

            # 先规范字幕格式，再创建临时目录
            subtitle_ext = os.path.splitext(subtitle_path)[1].lower()
            if subtitle_ext not in ('.srt', '.ass', '.ssa', '.vtt'):
                task_logger.warning(f"未知字幕扩展名 {subtitle_ext}，默认按srt处理")
                subtitle_ext = '.srt'
            temp_dir = tempfile.mkdtemp(prefix='subtitle_embed_')
            simple_video = os.path.abspath(video_path)
            simple_output = os.path.join(video_dir, f".{video_name}_with_subtitle.tmp.mp4")
            # 字体目录（在临时目录内放置一份，避免Windows路径转义问题）
            temp_fonts_dir = os.path.join(temp_dir, "fonts")
            os.makedirs(temp_fonts_dir, exist_ok=True)
            
            try:
                # 准备临时字幕文件
                try:
                    if os.path.exists(simple_output):
                        os.remove(simple_output)
                except Exception as e:
                    task_logger.warning(f"清理旧临时输出文件失败: {e}")
                
                style_overrides = self._resolve_subtitle_style_overrides(task_logger)
                subtitle_font = self._resolve_subtitle_font(task_logger, temp_fonts_dir)
                font_family = subtitle_font.get('font_family') or subtitle_font.get('configured_font_name')
                if task_logger:
                    task_logger.debug(
                        f"当前字幕字体配置: {subtitle_font.get('configured_font_name')}"
                    )
                    if subtitle_font.get('matched_font_name'):
                        task_logger.debug(
                            f"匹配到内置字体: {subtitle_font.get('matched_font_name')}"
                        )

                if not font_family:
                    # 回退到常见的中文字体家族名称。优先使用 Noto Sans CJK SC
                    fallback_families = [
                        'Noto Sans CJK SC',
                        'Source Han Sans HW SC VF',
                        'Source Han Sans HW SC',
                        'Source Han Sans SC',
                        'Source Han Sans',
                        'Microsoft YaHei',
                        'SimHei'
                    ]
                    font_family = fallback_families[0]
                    task_logger.debug(f"使用回退字体家族名称: {font_family}")

                render_subtitle_ext = subtitle_ext
                render_subtitle_name = f"sub{render_subtitle_ext}"
                render_subtitle_path = os.path.join(temp_dir, render_subtitle_name)

                if subtitle_ext in ('.ass', '.ssa'):
                    shutil.copy2(subtitle_path, render_subtitle_path)
                    # ASS/SSA 输入此前一律「保留源样式」，用户设置的字号/颜色/
                    # 描边/背景全部静默失效，但日志照样打印「字幕外观提示」，
                    # 让人误以为配置已应用。
                    # 现在：只有用户**确实改过**外观配置时才用 force_style 覆盖
                    # 源样式（libass 的 force_style 优先于脚本内样式）；全默认
                    # 配置下仍保留创作者的源样式，避免凭空破坏用户手作的 ASS。
                    filter_segments = [
                        f"subtitles={render_subtitle_name}",
                        "fontsdir=fonts",
                        "charenc=UTF-8",
                    ]
                    if self._style_overrides_differ_from_defaults(style_overrides):
                        force_style = self._build_subtitle_force_style(
                            font_family, input_width, input_height, style_overrides
                        )
                        if force_style:
                            filter_segments.append(force_style)
                        task_logger.info(
                            "ASS/SSA 输入：检测到自定义字幕外观配置，"
                            "将以 force_style 覆盖源样式后烧录"
                        )
                    else:
                        task_logger.info(
                            "ASS/SSA 输入：未配置自定义字幕外观，保留源字幕样式进行烧录"
                        )
                else:
                    render_subtitle_ext = '.ass'
                    render_subtitle_name = "sub.ass"
                    render_subtitle_path = os.path.join(temp_dir, render_subtitle_name)
                    config = getattr(self, 'config', {}) or {}
                    if not self._convert_srt_to_ass(
                        subtitle_path,
                        render_subtitle_path,
                        task_logger,
                        video_width=input_width,
                        video_height=input_height,
                        font_family=font_family,
                        prefer_single_line=config.get('SUBTITLE_PREFER_SINGLE_LINE', True),
                        single_line_min_font_scale=config.get(
                            'SUBTITLE_SINGLE_LINE_MIN_FONT_SCALE',
                            self._ASS_SINGLE_LINE_FONT_SCALE_MIN,
                        ),
                        style_overrides=style_overrides,
                    ):
                        task_logger.error("生成清晰ASS字幕失败，无法继续嵌入字幕流程")
                        update_task(task_id, upload_progress=None, status=previous_status, silent=True)
                        return None
                    task_logger.info("已为非ASS字幕生成统一底部居中ASS临时文件")
                    filter_segments = [
                        f"subtitles={render_subtitle_name}",
                        "fontsdir=fonts",
                        "charenc=UTF-8",
                    ]
                vf_filter = ':'.join(filter_segments)

                # 若字幕滤镜不可用，提前报错并放弃嵌入
                if not _ffmpeg_has_filter('subtitles'):
                    task_logger.error("当前FFmpeg不包含 'subtitles' 滤镜（需要libass支持），无法嵌入字幕")
                    update_task(task_id, upload_progress=None, status=previous_status, silent=True)
                    return None

                # --------------------------------------------------
                # 硬件编码检测与参数生成函数
                # --------------------------------------------------
                # 缓存硬件编码器检测结果，避免重复测试
                _hw_encoder_cache = {}
                _hw_encoder_error_cache = {}
                _hw_encoder_probe_meta_cache = {}
                amd_backend_cache = None

                def _is_encoder_listed(encoder_name: str) -> bool:
                    """仅检查编码器是否在 ffmpeg -encoders 中出现。"""
                    try:
                        list_result = subprocess.run(
                            [ffmpeg_bin, '-hide_banner', '-encoders'],
                            capture_output=True,
                            text=True,
                            encoding='utf-8',
                            errors='replace',
                            timeout=10
                        )
                        encoder_text = f"{list_result.stdout or ''}\n{list_result.stderr or ''}"
                        return list_result.returncode == 0 and encoder_name in encoder_text
                    except Exception:
                        return False

                def _detect_hw_encoder(encoder_name: str) -> bool:
                    """通过实际测试编码来检测硬件编码器是否可用（而非仅检查编译列表）"""
                    if encoder_name in _hw_encoder_cache:
                        return _hw_encoder_cache[encoder_name]
                    try:
                        # 先做一次快速枚举（兼容不同 ffmpeg 版本将输出写到 stdout/stderr 的差异）
                        # 注意：枚举失败/未匹配不直接判定不可用，仍以“实际编码测试”结果为准，避免误判。
                        encoder_list_available = False
                        try:
                            list_result = subprocess.run(
                                [ffmpeg_bin, '-hide_banner', '-encoders'],
                                capture_output=True,
                                text=True,
                                encoding='utf-8',
                                errors='replace',
                                timeout=10
                            )
                            encoder_text = f"{list_result.stdout or ''}\n{list_result.stderr or ''}"
                            encoder_list_available = (
                                list_result.returncode == 0 and encoder_name in encoder_text
                            )
                        except Exception:
                            # 枚举失败不影响后续实际测试
                            pass

                        available, probe_err, probe_meta = self._probe_hw_encoder_availability(
                            ffmpeg_bin=ffmpeg_bin,
                            encoder_name=encoder_name,
                        )
                        _hw_encoder_probe_meta_cache[encoder_name] = dict(probe_meta or {})
                        _hw_encoder_cache[encoder_name] = available
                        if not available:
                            _hw_encoder_error_cache[encoder_name] = probe_err or '未知错误'
                            if not encoder_list_available:
                                task_logger.debug(
                                    f"编码器 {encoder_name} 未在 ffmpeg -encoders 列表中匹配到，且实测失败: "
                                    f"{self._short_error_text(probe_err)}"
                                )
                            else:
                                task_logger.debug(
                                    f"编码器 {encoder_name} 已编译但硬件不可用: {self._short_error_text(probe_err)}"
                                )
                            task_logger.warning(
                                f"编码器 {encoder_name} 探测失败: "
                                f"probe_size={probe_meta.get('probe_size', 'unknown')}, "
                                f"probe_retry={probe_meta.get('probe_retry', False)}, "
                                f"probe_returncode={probe_meta.get('probe_returncode', 'unknown')}, "
                                f"probe_error_short={probe_meta.get('probe_error_short', '')}"
                            )
                            task_logger.debug(
                                f"编码器 {encoder_name} 探测命令摘要: {probe_meta.get('probe_cmd_summary', '')}"
                            )
                        else:
                            _hw_encoder_error_cache[encoder_name] = ''
                            task_logger.debug(
                                f"编码器 {encoder_name} 测试成功，硬件可用: "
                                f"probe_size={probe_meta.get('probe_size', 'unknown')}, "
                                f"probe_retry={probe_meta.get('probe_retry', False)}, "
                                f"probe_returncode={probe_meta.get('probe_returncode', 0)}"
                            )
                        return available
                    except Exception as e:
                        task_logger.debug(f"检测编码器 {encoder_name} 时出错: {e}")
                        _hw_encoder_cache[encoder_name] = False
                        _hw_encoder_error_cache[encoder_name] = self._short_error_text(str(e))
                        _hw_encoder_probe_meta_cache[encoder_name] = {
                            'probe_size': 'unknown',
                            'probe_retry': False,
                            'probe_returncode': -1,
                            'probe_error_short': self._short_error_text(str(e)),
                            'probe_cmd_summary': '',
                        }
                    return False

                def _detect_amd_backend() -> str:
                    """检测 AMD 可用后端，返回 amf/vaapi/none。"""
                    nonlocal amd_backend_cache
                    if amd_backend_cache is not None:
                        return amd_backend_cache
                    if _detect_hw_encoder('hevc_amf'):
                        amd_backend_cache = 'amf'
                    elif _detect_hw_encoder('hevc_vaapi'):
                        amd_backend_cache = 'vaapi'
                    else:
                        amd_backend_cache = 'none'
                    return amd_backend_cache

                def _detect_nvidia() -> bool:
                    """检测 NVIDIA HEVC NVENC 是否可用"""
                    return _detect_hw_encoder('hevc_nvenc')

                def _detect_intel() -> bool:
                    """检测 Intel HEVC QSV 是否可用"""
                    return _detect_hw_encoder('hevc_qsv')

                def _detect_amd() -> bool:
                    """检测 AMD HEVC AMF/VAAPI 是否可用"""
                    return _detect_amd_backend() != 'none'

                def _get_best_encoder() -> str:
                    """自动检测最佳可用编码器，返回: nvidia/intel/amd/cpu"""
                    if _detect_nvidia():
                        task_logger.info("检测到 NVIDIA GPU，使用 NVENC HEVC 硬件编码")
                        return 'nvidia'
                    if _detect_intel():
                        task_logger.info("检测到 Intel GPU，使用 QSV HEVC 硬件编码")
                        return 'intel'
                    if _detect_amd():
                        task_logger.info("检测到 AMD GPU，使用 AMF/VAAPI HEVC 硬件编码")
                        return 'amd'
                    task_logger.info("未检测到可用的硬件编码器，使用 CPU 软编码")
                    return 'cpu'

                # 从配置中获取（可选）自定义视频参数
                custom_video_params = self._parse_custom_video_params(task_logger)
                # 编码器附加配置（质量模式/预设/增强开关/色彩元数据/CPU 编码器）
                encoder_settings = parse_encoder_config(getattr(self, 'config', {}) or {})

                # CPU 软编码器（x264 / x265）。与 VIDEO_ENCODER 正交：后者选硬件，
                # 这里选软编码实现。x265 输出 HEVC，实测编码耗时约为 x264 的 5 倍。
                cpu_codec = normalize_cpu_codec(encoder_settings.get('cpu_codec'))

                # tune 是按 CPU 编码器各自的白名单校验的：film / stillimage 在 x264
                # 下合法，在 x265 下会让编码直接失败。用户切到 x265 后这类取值会被
                # 丢弃，必须显式告知，否则「配置了却没生效」无从察觉。
                _configured_tune = str(
                    (getattr(self, 'config', {}) or {}).get('VIDEO_X264_TUNE', '') or ''
                ).strip()
                if _configured_tune and not encoder_settings.get('software_tune'):
                    task_logger.warning(
                        f"VIDEO_X264_TUNE='{_configured_tune}' 对 {cpu_codec} 不合法，"
                        f"已忽略该 tune（x265 不支持 film/stillimage）"
                    )

                # 固定质量值（CRF/CQ/QP，越小质量越高）。auto 模式按分辨率推荐，
                # manual 模式使用用户在设置页指定的值。
                target_quality = recommend_quality(input_height)
                effective_quality = target_quality
                if (
                    encoder_settings.get('quality_mode') == 'manual'
                    and encoder_settings.get('quality_value') is not None
                ):
                    effective_quality = float(encoder_settings['quality_value'])
                target_quality_str = format_quality_value(effective_quality)
                target_quality_int = max(0, min(51, int(round(effective_quality))))
                task_logger.info(
                    f"视频分辨率: {input_width}x{input_height}, 推荐质量={format_quality_value(target_quality)}, "
                    f"实际质量={target_quality_str} (int={target_quality_int}), "
                    f"质量模式={encoder_settings.get('quality_mode')}, "
                    f"硬件质量增强={'开' if encoder_settings.get('hw_quality_boost') else '关'}, "
                    f"硬件档位={encoder_settings.get('hw_quality_level')}"
                )

                # 色彩元数据：auto 模式透传源流信息，避免播放器按默认色域解释导致偏色
                color_params = []
                color_map = {}
                try:
                    color_map = normalize_color_metadata(
                        encoder_settings.get('color_metadata_mode'),
                        stream_info,
                    )
                    color_params = resolve_color_metadata(
                        encoder_settings.get('color_metadata_mode'),
                        stream_info,
                        logger=task_logger,
                    )
                except Exception as color_exc:
                    task_logger.debug(f"解析色彩元数据失败，跳过色彩参数: {color_exc}")
                if color_params:
                    task_logger.info(f"输出色彩元数据: {' '.join(color_params)}")

                def _video_param_ctx(amd_backend=None, cpu_codec_override=None, hw_quality_boost=None):
                    """汇总编码上下文，交由 video_encoder_params 统一构造参数。

                    cpu_codec_override 用于 x265 -> x264 降级阶段覆盖用户选择；
                    为 None 时使用配置里的 VIDEO_CPU_CODEC。这里刻意不与外层的
                    cpu_codec 同名，避免遮蔽导致「以为读的是配置、其实读的是形参」。
                    color_map 交给 encoder_params，由它把色彩 VUI 与（x265 的）
                    质量增强合并成同一条私有参数选项。

                    ``hw_quality_boost`` 显式传入时覆盖 ``encoder_settings`` 里的值。
                    降级重试必须走这条覆盖通道，**不能**就地改写 ``encoder_settings``：
                    那是闭包共享的 dict，改掉之后后面的 CPU 回退阶段读到的就是
                    「增强已关」，用户明明开着增强，回退后的成片却按关闭增强的
                    基础参数编码（0a98c26 引入的行为）。
                    """
                    ctx = {
                        'height': input_height,
                        'gop': gop,
                        'gop_hevc': gop_hevc,
                        'quality_mode': encoder_settings.get('quality_mode'),
                        'quality_value': encoder_settings.get('quality_value'),
                        'cpu_preset': encoder_settings.get('cpu_preset'),
                        'cpu_preset_hd': encoder_settings.get('cpu_preset_hd'),
                        'duration_s': video_duration,
                        'hw_quality_boost': (
                            encoder_settings.get('hw_quality_boost')
                            if hw_quality_boost is None
                            else hw_quality_boost
                        ),
                        'hw_quality_level': encoder_settings.get('hw_quality_level'),
                        'cpu_codec': cpu_codec_override or encoder_settings.get('cpu_codec'),
                        'software_tune': encoder_settings.get('software_tune'),
                        'color_map': color_map,
                        'custom_params': custom_video_params,
                    }
                    if amd_backend is not None:
                        ctx['amd_backend'] = amd_backend
                    return ctx

                # 针对软编码生成统一参数（libx264 或 libx265，由 VIDEO_CPU_CODEC 决定）。
                # 色彩 VUI 已由 build_encoder_params 合并在内，调用方不要再另行追加，
                # 否则 x265 会出现两条 -x265-params 而后者覆盖前者。
                def build_cpu_params(cpu_codec_override=None, hw_quality_boost=None):
                    return build_encoder_params(
                        'cpu',
                        _video_param_ctx(
                            cpu_codec_override=cpu_codec_override,
                            hw_quality_boost=hw_quality_boost,
                        ),
                    )

                def build_nvidia_params(hw_quality_boost=None):
                    """生成 NVIDIA NVENC HEVC 编码参数"""
                    return build_encoder_params(
                        'nvidia', _video_param_ctx(hw_quality_boost=hw_quality_boost)
                    )

                def build_intel_params(hw_quality_boost=None):
                    """生成 Intel QSV HEVC 编码参数"""
                    return build_encoder_params(
                        'intel', _video_param_ctx(hw_quality_boost=hw_quality_boost)
                    )

                def build_amd_params(hw_quality_boost=None):
                    """生成 AMD AMF/VAAPI HEVC 编码参数"""
                    return build_encoder_params(
                        'amd',
                        _video_param_ctx(_detect_amd_backend(), hw_quality_boost=hw_quality_boost),
                    )

                def is_vaapi_encoder() -> bool:
                    """检查当前是否使用 VAAPI 编码器（需要特殊的滤镜链处理）"""
                    return actual_encoder == 'amd' and _detect_amd_backend() == 'vaapi'

                # 确定使用的编码器
                encoder_pref = str(self.config.get('VIDEO_ENCODER', 'auto')).lower().strip()
                actual_encoder = encoder_pref
                
                if encoder_pref == 'auto':
                    actual_encoder = _get_best_encoder()
                elif encoder_pref == 'nvidia':
                    if not _detect_nvidia():
                        nvenc_listed = _is_encoder_listed('hevc_nvenc')
                        nvidia_device_visible = any(
                            os.path.exists(path)
                            for path in ('/dev/nvidia0', '/dev/nvidiactl', '/dev/nvidia-modeset')
                        )
                        nvenc_reason_raw = _hw_encoder_error_cache.get('hevc_nvenc') or '未知'
                        nvenc_reason = nvenc_reason_raw.replace('\n', ' ')
                        nvenc_probe_meta = _hw_encoder_probe_meta_cache.get('hevc_nvenc') or {}
                        nvenc_probe_size = nvenc_probe_meta.get('probe_size', 'unknown')
                        nvenc_probe_retry = nvenc_probe_meta.get('probe_retry', False)
                        nvenc_probe_error_short = nvenc_probe_meta.get('probe_error_short', '')
                        nvenc_probe_cmd_summary = nvenc_probe_meta.get('probe_cmd_summary', '')
                        task_logger.warning(
                            f"NVENC HEVC诊断信息: ffmpeg={ffmpeg_bin}, hevc_nvenc_listed={nvenc_listed}, "
                            f"nvidia_device_visible={nvidia_device_visible}, detect_error={nvenc_reason[:240]}, "
                            f"probe_size={nvenc_probe_size}, probe_retry={nvenc_probe_retry}, "
                            f"probe_error_short={nvenc_probe_error_short}"
                        )
                        if nvenc_probe_cmd_summary:
                            task_logger.debug(f"NVENC HEVC探测命令摘要: {nvenc_probe_cmd_summary}")

                        if self._should_keep_nvidia_preference_on_probe_failure(
                            nvenc_listed=nvenc_listed,
                            nvidia_device_visible=nvidia_device_visible,
                            detect_error=nvenc_reason_raw,
                        ):
                            task_logger.warning(
                                "NVENC HEVC 预检命中分辨率限制类错误，保留 NVIDIA 编码偏好并尝试真实转码"
                            )
                            actual_encoder = 'nvidia'
                        else:
                            task_logger.warning("配置使用 NVIDIA HEVC 编码但未检测到 NVENC，回退到自动检测")
                            task_logger.warning(
                                "NVENC 不可用常见原因：Docker 未启用 GPU 透传（gpus: all）、"
                                "主机未安装 nvidia-container-toolkit、"
                                "或 NVIDIA_DRIVER_CAPABILITIES 未包含 video,utility。"
                            )
                            actual_encoder = _get_best_encoder()
                elif encoder_pref == 'intel':
                    if not _detect_intel():
                        task_logger.warning("配置使用 Intel HEVC 编码但未检测到 QSV，回退到自动检测")
                        actual_encoder = _get_best_encoder()
                elif encoder_pref == 'amd':
                    if not _detect_amd():
                        task_logger.warning("配置使用 AMD HEVC 编码但未检测到 AMF/VAAPI，回退到自动检测")
                        actual_encoder = _get_best_encoder()

                # 根据编码器生成参数
                if actual_encoder == 'nvidia':
                    vparams = build_nvidia_params()
                    task_logger.info("使用 NVIDIA NVENC HEVC 硬件编码")
                elif actual_encoder == 'intel':
                    vparams = build_intel_params()
                    task_logger.info("使用 Intel QSV HEVC 硬件编码")
                elif actual_encoder == 'amd':
                    amd_backend = _detect_amd_backend()
                    if amd_backend == 'none':
                        task_logger.warning("AMD HEVC 编码已选中，但未检测到可用 AMF/VAAPI 后端，回退到 CPU 软编码")
                        actual_encoder = 'cpu'
                        vparams = build_cpu_params()
                    else:
                        vparams = build_amd_params()
                        encoder_name = 'AMF' if amd_backend == 'amf' else 'VAAPI'
                        task_logger.info(f"使用 AMD {encoder_name} HEVC 硬件编码（backend={amd_backend}）")
                else:
                    vparams = build_cpu_params()
                    task_logger.info(
                        f"使用 CPU 软编码 ({'libx265' if cpu_codec == 'x265' else 'libx264'})"
                    )

                # 音频优先直拷 AAC，非 AAC 再按源码率上限转 AAC
                aparams = self._build_audio_transcode_params(audio_info)

                # 软件编码的色彩 VUI 不再在这里追加：libx264/libx265 会静默忽略
                # -color_primaries/-color_trc，必须用编码器私有参数补写，而 x265 的
                # 质量增强与 VUI 必须写进**同一条** -x265-params（实测两条选项时后者
                # 完全覆盖前者）。因此合并动作放在 build_encoder_params 内部完成，
                # 这里只回显，避免两处各追加一条而互相覆盖。
                #
                # 「我们写了哪条、有没有写」由模块给出（custom_params_vui_report），
                # 不在这里扫 vparams 猜：vparams 会先原样铺开用户的自定义参数，用户给
                # **另一个**编码器写了私有参数时（切换 VIDEO_CPU_CODEC 后忘了删旧参数），
                # 扫到的第一条 -x26?-params 不是我们写的那条，日志会按错误的编码器口径
                # 说反（R5-1）。
                if custom_video_params:
                    _vui_option, _vui_codec, _vui_reason = custom_params_vui_report(
                        custom_video_params, cpu_codec, color_map
                    )
                else:
                    # 内置参数分支：没有用户参数可让位，VUI 由 build_encoder_params 直接写入
                    _vui_option = (
                        private_param_option_name(cpu_codec) if actual_encoder == 'cpu' else ''
                    )
                    _vui_codec = cpu_codec
                    _vui_reason = VUI_REPORT_WRITTEN if _vui_option else VUI_REPORT_NOTHING

                if _vui_reason == VUI_REPORT_WRITTEN:
                    _vui_index = vparams.index(_vui_option) if _vui_option in vparams else -1
                    _vui_value = (
                        vparams[_vui_index + 1]
                        if 0 <= _vui_index < len(vparams) - 1
                        else ''
                    )
                    if _vui_index >= 0:
                        task_logger.info(f"软件编码私有参数: {_vui_option} {_vui_value}")
                elif _vui_reason == VUI_REPORT_USER_NO_VUI:
                    # 用户自带该编码器的 -x26?-params 但里面没有色彩键：模块不覆盖用户
                    # 配置，因此本次**没有**补写色彩 VUI。此前这里照样打印「软件编码
                    # 私有参数: …」，看起来等同于「VUI 已写入」，用户只能从画面偏色去猜。
                    task_logger.warning(
                        "自定义视频参数里的 %s 未包含色彩键（colorprim/transfer/"
                        "colormatrix）：本次不覆盖用户配置，输出码流未补写原色与"
                        "传递特性",
                        private_param_option_name(_vui_codec),
                    )
                elif _vui_reason == VUI_REPORT_USER_VUI:
                    task_logger.info(
                        "色彩 VUI 由用户自定义的 %s 提供：本次不覆盖用户配置",
                        private_param_option_name(_vui_codec),
                    )
                elif _vui_reason == VUI_REPORT_USER_OWNS_CODEC and actual_encoder == 'cpu':
                    # 用户在不透明参数里接管了编码器选择：VIDEO_CPU_CODEC 不生效，
                    # 色彩 VUI 也按识别结果决定是否补写。不说明的话，日志看起来
                    # 像「私有参数没写」等于「没有可写的东西」。
                    task_logger.warning(
                        "自定义视频参数已指定编码器：VIDEO_CPU_CODEC 不生效；"
                        "仅在能识别为 libx264/libx265 时才补写色彩 VUI 私有参数"
                    )

                main_color_params = list(color_params)

                task_logger.info(f"视频编码参数: {' '.join(vparams)}")
                task_logger.info(f"音频编码参数: {' '.join(aparams)}")

                # 构建视频滤镜链
                # VAAPI 编码器需要特殊处理：在字幕滤镜后添加格式转换和硬件上传
                if is_vaapi_encoder():
                    # VAAPI: subtitles -> format=nv12 -> hwupload
                    final_vf = f"{vf_filter},format=nv12,hwupload"
                else:
                    final_vf = vf_filter

                cmd = self._build_embed_ffmpeg_cmd(
                    ffmpeg_bin=ffmpeg_bin,
                    input_video=simple_video,
                    vf_filter=final_vf,
                    vparams=vparams,
                    aparams=aparams,
                    output_video=simple_output,
                    color_params=main_color_params,
                )
                
                task_logger.debug(f"FFmpeg命令: {' '.join(cmd)}")
                task_logger.debug(f"临时目录: {temp_dir}")
                
                # 设置超时时间（根据视频时长估算）
                timeout = self._estimate_embed_timeout(
                    video_duration, cpu_codec if actual_encoder == 'cpu' else None
                )
                
                task_logger.debug(f"设置处理超时时间: {timeout//60} 分钟")
                
                # 执行 FFmpeg 命令并实时获取进度。
                # 抽成可复用函数：硬件编码失败时需要按「硬编关增强 → CPU」分级重试，
                # 每次都走同一套进度解析与超时/取消处理，避免逻辑分叉。
                def _execute_embed(embed_cmd, run_timeout, stage_label=''):
                    stage_suffix = f"[{stage_label}] " if stage_label else ''
                    proc = subprocess.Popen(
                        embed_cmd,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                        cwd=temp_dir,  # 在临时目录执行
                        encoding='utf-8',
                        errors='replace'  # 遇到无法解码的字符时用?替换
                    )

                    # 创建线程来读取输出，避免管道阻塞
                    stage_output_queue = queue.Queue()
                    stage_error_queue = queue.Queue()

                    def read_stream(stream, target_queue, stream_name):
                        try:
                            if stream is not None:
                                for raw_line in stream:
                                    target_queue.put(raw_line.strip())
                            else:
                                task_logger.warning(f"{stage_suffix}process.{stream_name} 为 None，无法读取输出")
                        except Exception:
                            pass
                        finally:
                            target_queue.put(None)

                    # 启动读取线程
                    output_thread = threading.Thread(
                        target=read_stream,
                        args=(proc.stdout, stage_output_queue, 'stdout'),
                        daemon=True,
                    )
                    error_thread = threading.Thread(
                        target=read_stream,
                        args=(proc.stderr, stage_error_queue, 'stderr'),
                        daemon=True,
                    )
                    output_thread.start()
                    error_thread.start()

                    # 实时解析进度
                    stage_last_time = 0
                    stage_start_time = time.time()
                    stage_last_progress_time = stage_start_time
                    stage_error_messages = []
                    stage_timed_out = False

                    while True:
                        if is_task_cancelled(task_id):
                            task_logger.info(f"{stage_suffix}检测到任务取消请求，终止FFmpeg转码")
                            proc.terminate()
                            try:
                                proc.wait(timeout=PROCESS_TERMINATE_WAIT_SECONDS)
                            except subprocess.TimeoutExpired:
                                if proc.poll() is None:
                                    proc.kill()
                            raise TaskCancelledError("任务已取消")
                        # 检查超时
                        if time.time() - stage_start_time > run_timeout:
                            task_logger.error(f"{stage_suffix}FFmpeg处理超时（{run_timeout//60}分钟），强制终止")
                            # 标记「这是超时」而不是靠错误文本推断：超时被杀时
                            # stderr 里没有设备/参数类关键字，按「未知错误」处理会
                            # 让长视频先按同一硬编器把整片再跑一遍（最现实的失败
                            # 类型反而拿到最差的降级策略）。
                            stage_timed_out = True
                            proc.terminate()
                            try:
                                proc.wait(timeout=PROCESS_TERMINATE_WAIT_SECONDS)
                            except subprocess.TimeoutExpired:
                                if proc.poll() is None:
                                    proc.kill()
                            break

                        # 检查进程状态
                        if proc.poll() is not None:
                            break

                        # 读取输出
                        try:
                            line = stage_output_queue.get(timeout=1)
                            if line is None:
                                break

                            if line.startswith('out_time_us='):
                                try:
                                    # 解析当前处理时间（微秒）
                                    time_us = int(line.split('=')[1])
                                    current_time = time_us / 1000000.0  # 转换为秒

                                    if video_duration and current_time > stage_last_time:
                                        progress = min((current_time / video_duration) * 100, 100)
                                        # 更新任务进度显示
                                        update_task(task_id, upload_progress=f"{progress:.1f}%", silent=True)
                                        stage_last_time = current_time
                                        stage_last_progress_time = time.time()
                                except (ValueError, IndexError):
                                    continue
                        except queue.Empty:
                            # 检查是否长时间没有进度更新（可能卡死了）
                            if time.time() - stage_last_progress_time > 300:  # 5分钟没有进度更新
                                task_logger.debug(f"{stage_suffix}长时间没有进度更新，可能处理卡死")
                            continue

                        # 读取错误信息
                        try:
                            error_line = stage_error_queue.get_nowait()
                            if error_line:
                                stage_error_messages.append(error_line)
                                if len(stage_error_messages) > 50:  # 限制错误信息数量
                                    stage_error_messages.pop(0)
                        except queue.Empty:
                            pass

                    # 等待进程完成
                    try:
                        proc.wait(timeout=30)  # 最多等待30秒
                    except subprocess.TimeoutExpired:
                        task_logger.error(f"{stage_suffix}进程未能在30秒内正常结束，强制终止")
                        proc.kill()

                    # 进程退出后必须把管线里剩余的错误行排空：读取线程只 start 不
                    # join 时，`Unrecognized option` / `Error setting option` 这类
                    # 参数错误恰好落在 stderr 最后几行，会被竞态漏掉 —— 而它们正是
                    # 分级降级决策依赖的信号。先 join 让线程读到 EOF，再排空队列。
                    for stream_thread in (output_thread, error_thread):
                        try:
                            stream_thread.join(timeout=1.0)
                        except Exception:
                            pass
                    for _ in range(200):
                        try:
                            drained = stage_error_queue.get_nowait()
                        except queue.Empty:
                            break
                        if drained:
                            stage_error_messages.append(drained)
                            if len(stage_error_messages) > 50:  # 限制错误信息数量
                                stage_error_messages.pop(0)

                    error_text = '\n'.join(stage_error_messages)
                    return proc.returncode, error_text, os.path.exists(simple_output), stage_timed_out

                # 超时预算必须从**首次尝试之前**开始计：此前 deadline 在首轮返回
                # 之后才建立，首轮已消耗的时间没有从预算里扣掉。
                embed_started_at = time.monotonic()
                first_stage_budget = max(300.0, float(timeout))
                process_returncode, error_output_full, output_ready, first_timed_out = _execute_embed(
                    cmd, first_stage_budget
                )

                if process_returncode == 0 and output_ready:
                    # 成功：优先原子替换，避免重复复制大文件
                    self._finalize_embedded_video_output(simple_output, embedded_video_path)
                    _log_output_media_summary(embedded_video_path)
                    # 结束日志：成功
                    task_logger.info(f"字幕嵌入完成: {embedded_video_path}")

                    # 清除进度显示并恢复之前的状态
                    update_task(task_id, upload_progress=None, status=previous_status, silent=True)
                    return embedded_video_path

                # 收集错误信息
                error_output_tail = error_output_full.strip() or "无详细错误信息"
                task_logger.error(f"字幕嵌入失败 (返回码: {process_returncode})")
                # 必须 join：直接把 splitlines() 的 list 插进 f-string 会打印
                # Python repr（`['...', '...']`），硬编失败时日志几乎不可读。
                task_logger.error(
                    "错误信息(尾部): %s", '\n'.join(error_output_tail.splitlines()[-50:])
                )

                def _rebuild_hw_params(boost_enabled):
                    """按同一硬件编码器重建参数（用于关闭质量增强后的降级重试）。

                    通过 ``hw_quality_boost`` 覆盖通道传值，不改共享 dict ——
                    就地改写会让后面的 CPU 回退阶段也读到「增强已关」。
                    """
                    if actual_encoder == 'nvidia':
                        return build_nvidia_params(hw_quality_boost=boost_enabled)
                    if actual_encoder == 'intel':
                        return build_intel_params(hw_quality_boost=boost_enabled)
                    if actual_encoder == 'amd':
                        return build_amd_params(hw_quality_boost=boost_enabled)
                    return build_cpu_params(hw_quality_boost=boost_enabled)

                # 分级降级：硬编+质量增强失败时，先尝试关闭增强保留硬件加速，
                # 仍失败才退回 CPU。直接跳 CPU 会让可用 GPU 白白闲置。
                # 但设备/驱动类错误关掉增强没有任何作用，直接走 CPU ——
                # 否则会按同一硬编器把整部视频白跑一遍。
                hw_error_detected = self._is_known_hw_encoder_error(error_output_full)
                hw_option_error = self._is_hw_option_error(error_output_full)
                boost_enabled = bool(encoder_settings.get('hw_quality_boost'))
                if actual_encoder in ('nvidia', 'intel', 'amd'):
                    failure_kind = self._describe_hw_failure(
                        actual_encoder,
                        timed_out=first_timed_out,
                        hw_option_error=hw_option_error,
                        hw_error_detected=hw_error_detected,
                        boost_enabled=boost_enabled,
                        returncode=process_returncode,
                    )
                    if failure_kind == 'timeout':
                        task_logger.warning(
                            f"硬件编码器 {actual_encoder} 处理超时，跳过关增强重试直接回退 CPU 编码"
                        )
                    elif failure_kind == 'option_error':
                        task_logger.warning(
                            f"硬件编码器 {actual_encoder} 不接受某个选项，"
                            f"先关闭质量增强后用同一编码器重试"
                        )
                    elif failure_kind == 'option_error_no_boost':
                        # 质量增强本来就没开，「先关增强重试」是空动作：此前的日志
                        # 照搬这句话，会把人往错的方向带（关闭后仍会失败）。
                        task_logger.warning(
                            f"硬件编码器 {actual_encoder} 不接受某个选项，且质量增强未开启，"
                            f"直接回退 CPU 编码"
                        )
                    elif failure_kind == 'device_error':
                        task_logger.warning(
                            f"硬件编码器 {actual_encoder} 报设备/驱动类错误，"
                            f"关闭质量增强无效，直接回退 CPU 编码"
                        )
                    else:
                        task_logger.warning(
                            f"硬件编码器 {actual_encoder} 失败（返回码: {process_returncode}），"
                            f"未检测到已知硬件错误，仍尝试降级重试"
                        )
                retry_stages = self._resolve_embed_retry_stages(
                    actual_encoder,
                    encoder_settings.get('hw_quality_boost'),
                    hw_error_detected,
                    hw_option_error,
                    cpu_codec,
                )
                if first_timed_out:
                    retry_stages = self._drop_same_encoder_stage_after_timeout(retry_stages)

                # 总预算 = 首轮尝试 + 各降级阶段各自的合理预算（从首轮**之前**开始
                # 计）。单阶段上限取该阶段编码器的估算，因此 libx265 阶段拿到的是
                # 放大后的 5 倍预算，而不会挤掉首轮的时间；阶段数由
                # _resolve_embed_retry_stages 固定（至多 3 级），总时长有界。
                # 此前 deadline 建在首轮返回之后（首轮不计入），且 CPU 回退阶段
                # 一律沿用 x264 预算 —— 长视频会在 x265 编码中途被强杀。
                def _stage_budget(stage):
                    return self._embed_stage_budget(
                        video_duration, cpu_codec, stage, first_stage_budget
                    )

                overall_deadline = embed_started_at + first_stage_budget + sum(
                    _stage_budget(stage) for stage in retry_stages
                )
                # 最坏挂起时长在日志里可见：新增 cpu_x264 一级后 20 分钟素材的
                # 总预算可达 8 小时（x265 的 CPU 阶段单级就是 5 倍），卡住的 ffmpeg
                # 光看日志无法判断上限，排查时只能猜。这里记一条 info。
                if retry_stages:
                    task_logger.info(
                        "降级重试预算：首轮 %d 分钟 + %s = 最坏总时长约 %d 分钟（%s）",
                        int(first_stage_budget // 60),
                        ' + '.join(
                            f'{stage} {int(_stage_budget(stage) // 60)} 分钟'
                            for stage in retry_stages
                        ),
                        int((overall_deadline - embed_started_at) // 60),
                        '/'.join(retry_stages),
                    )

                for stage in retry_stages:
                    if is_task_cancelled(task_id):
                        task_logger.info("检测到任务取消请求，跳过FFmpeg回退方案")
                        raise TaskCancelledError("任务已取消")

                    remaining_s = overall_deadline - time.monotonic()
                    if remaining_s <= 0:
                        task_logger.warning(
                            "回退总超时预算已耗尽，跳过剩余降级阶段: %s",
                            '/'.join(retry_stages[retry_stages.index(stage):]),
                        )
                        break

                    if stage == 'hw_no_boost':
                        stage_label = f"{actual_encoder} 关闭质量增强"
                        task_logger.warning("硬件编码或质量增强参数不被支持，尝试关闭质量增强后用同一编码器重试...")
                        stage_vparams = _rebuild_hw_params(False)
                        stage_filter = final_vf
                        stage_color_params = list(color_params)
                    elif stage == 'cpu_x264':
                        # x265 不可用（未编进 FFmpeg）或其参数不被接受：改用 libx264
                        # 重跑。滤镜链与通用色彩选项不变，只有软件编码实现换掉。
                        stage_label = 'CPU 软编码 (libx265 回退至 libx264)'
                        task_logger.warning(
                            "libx265 不可用或其参数不被接受，改用 libx264 软编码重试..."
                        )
                        stage_vparams = build_cpu_params(cpu_codec_override='x264')
                        stage_filter = vf_filter
                        stage_color_params = list(color_params)
                    else:
                        stage_label = 'CPU 软编码'
                        task_logger.warning("尝试使用CPU编码回退方案...")
                        stage_vparams = build_cpu_params()
                        stage_filter = vf_filter
                        stage_color_params = list(color_params)

                    # 每个阶段只拿「总预算减去已用掉的部分」，且单阶段不得超过**该
                    # 阶段自身编码器**的合理预算（否则硬编回退到 libx265 时会拿到
                    # x264 的预算，长视频在编码中途被杀，连 x265 -> x264 那一级都
                    # 轮不到）；用 60s 兜底避免预算耗尽时把阶段压成 0。
                    stage_budget = _stage_budget(stage)
                    stage_timeout = int(max(60.0, min(remaining_s, stage_budget)))

                    cmd_retry = self._build_embed_ffmpeg_cmd(
                        ffmpeg_bin=ffmpeg_bin,
                        input_video=simple_video,
                        vf_filter=stage_filter,
                        vparams=stage_vparams,
                        aparams=aparams,
                        output_video=simple_output,
                        color_params=stage_color_params,
                    )
                    task_logger.debug(f"回退FFmpeg命令({stage_label}): {' '.join(cmd_retry)}")

                    retry_returncode, retry_error, retry_ready, _stage_timed_out = _execute_embed(
                        cmd_retry, stage_timeout, stage_label
                    )
                    if retry_returncode == 0 and retry_ready:
                        self._finalize_embedded_video_output(simple_output, embedded_video_path)
                        _log_output_media_summary(embedded_video_path)
                        task_logger.info(f"字幕嵌入完成: {embedded_video_path}（{stage_label}回退）")
                        update_task(task_id, upload_progress=None, status=previous_status, silent=True)
                        return embedded_video_path

                    task_logger.error(f"{stage_label}回退方案仍然失败 (返回码: {retry_returncode})")
                    if retry_error:
                        task_logger.error(f"FFmpeg错误({stage_label}回退): {retry_error.splitlines()[-50:]}")

                    # 清理上一阶段可能残留的半成品输出，避免下一次重试用旧文件误判成功
                    try:
                        if os.path.exists(simple_output):
                            os.remove(simple_output)
                    except Exception as cleanup_exc:
                        task_logger.warning(f"清理回退残留输出失败: {cleanup_exc}")

                update_task(task_id, upload_progress=None, status=previous_status, silent=True)
                return None
            
            finally:
                # 清理残留临时输出
                try:
                    if simple_output and os.path.exists(simple_output):
                        os.remove(simple_output)
                except Exception as e:
                    task_logger.warning(f"清理临时输出文件失败: {e}")

                # 清理临时目录
                try:
                    shutil.rmtree(temp_dir)
                except Exception as e:
                    task_logger.warning(f"清理临时目录失败: {e}")
                    
        except subprocess.TimeoutExpired:
            task_logger.error("FFmpeg执行超时")
            update_task(task_id, upload_progress=None, status=previous_status, silent=True)
            return None
        except FileNotFoundError:
            task_logger.error("FFmpeg未安装或不在PATH中")
            update_task(task_id, upload_progress=None, status=previous_status, silent=True)
            return None
        except Exception as e:
            task_logger.error(f"嵌入字幕时发生错误: {str(e)}")
            update_task(task_id, upload_progress=None, status=previous_status, silent=True)
            return None

    def _get_video_duration(self, video_path, task_logger):
        """获取视频时长（秒）"""
        try:
            # subprocess imported at module level
            ffmpeg_bin = get_ffmpeg_path(logger=task_logger)
            if not ffmpeg_bin:
                raise FileNotFoundError('ffmpeg not found')
            ffprobe_bin = get_ffprobe_path(ffmpeg_path=ffmpeg_bin, logger=task_logger)
            if not ffprobe_bin:
                raise FileNotFoundError('ffprobe not found')
            cmd = [
                ffprobe_bin, '-v', 'quiet', '-print_format', 'json', 
                '-show_format', video_path
            ]
            
            result = subprocess.run(
                cmd, 
                capture_output=True, 
                text=True, 
                encoding='utf-8',
                errors='replace',
                timeout=60  # 添加60秒超时
            )
            
            if result.returncode == 0:
                import json
                data = json.loads(result.stdout)
                duration = float(data['format']['duration'])
                task_logger.info(f"视频时长: {duration:.2f} 秒")
                return duration
            else:
                task_logger.warning("无法获取视频时长，将无法显示转码进度")
                return None
        except subprocess.TimeoutExpired:
            task_logger.warning("获取视频时长超时")
            return None

    def _get_video_stream_info(self, video_path, task_logger):
        """获取视频流分辨率/帧率/像素格式/编码信息。"""
        # 使用类型注解明确字典值的类型
        info: dict[str, float | int | str | None] = {
            "width": None,
            "height": None,
            "fps": None,
            "pix_fmt": None,
            "codec_name": None,
            "bit_rate": None,
            # 色彩元数据：转码时透传，避免播放器按默认色域解释导致偏色
            "color_space": None,
            "color_primaries": None,
            "color_transfer": None,
            "color_range": None,
        }
        try:
            # subprocess/json handled at module level where needed
            ffmpeg_bin = get_ffmpeg_path(logger=task_logger)
            if not ffmpeg_bin:
                raise FileNotFoundError('ffmpeg not found')
            ffprobe_bin = get_ffprobe_path(ffmpeg_path=ffmpeg_bin, logger=task_logger)
            if not ffprobe_bin:
                raise FileNotFoundError('ffprobe not found')
            cmd = [
                ffprobe_bin, '-v', 'quiet', '-print_format', 'json',
                '-select_streams', 'v:0', '-show_streams', video_path
            ]
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding='utf-8',
                errors='replace',
                timeout=30
            )
            if result.returncode == 0:
                data = json.loads(result.stdout)
                streams = data.get('streams', [])
                if streams:
                    s = streams[0]
                    # 确保 info 字典不是 None
                    if info is not None:
                        info['width'] = s.get('width')
                        info['height'] = s.get('height')
                        info['pix_fmt'] = s.get('pix_fmt')
                        info['codec_name'] = s.get('codec_name')
                        info['color_space'] = s.get('color_space')
                        info['color_primaries'] = s.get('color_primaries')
                        info['color_transfer'] = s.get('color_transfer')
                        info['color_range'] = s.get('color_range')
                        bit_rate = s.get('bit_rate')
                        if bit_rate:
                            try:
                                info['bit_rate'] = int(bit_rate)
                            except Exception:
                                pass
                        r = s.get('avg_frame_rate') or s.get('r_frame_rate')
                        if r and r != '0/0':
                            try:
                                num, den = r.split('/')
                                fps = float(num) / float(den) if float(den) != 0 else 0
                                if fps > 1:
                                    # 使用类型断言解决类型问题
                                    info['fps'] = fps  # type: ignore
                            except Exception:
                                pass
            task_logger.info(f"视频流信息: {info}")
        except subprocess.TimeoutExpired:
            task_logger.warning("获取视频流信息超时")
        except Exception as e:
            task_logger.warning(f"获取视频流信息失败: {str(e)}")
        return info

    def _get_audio_stream_info(self, video_path, task_logger):
        """获取音频流信息（编码/码率/采样率等），用于音频参数设置。"""
        info: dict[str, int | str | None] = {
            "codec_name": None,
            "bit_rate": None,
            "sample_rate": None,
            "channels": None,
            "sample_fmt": None,
        }
        try:
            # subprocess/json handled at module level where needed
            ffmpeg_bin = get_ffmpeg_path(logger=task_logger)
            if not ffmpeg_bin:
                raise FileNotFoundError('ffmpeg not found')
            ffprobe_bin = get_ffprobe_path(ffmpeg_path=ffmpeg_bin, logger=task_logger)
            if not ffprobe_bin:
                raise FileNotFoundError('ffprobe not found')
            cmd = [
                ffprobe_bin, '-v', 'quiet', '-print_format', 'json',
                '-select_streams', 'a:0', '-show_streams', video_path
            ]
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding='utf-8',
                errors='replace',
                timeout=30
            )
            if result.returncode == 0:
                data = json.loads(result.stdout)
                streams = data.get('streams', [])
                if streams:
                    s = streams[0]
                    info['codec_name'] = s.get('codec_name')
                    bit_rate = s.get('bit_rate')
                    try:
                        if bit_rate:
                            info['bit_rate'] = int(bit_rate)
                    except Exception:
                        pass
                    sr = s.get('sample_rate')
                    try:
                        if sr:
                            info['sample_rate'] = int(sr)
                    except Exception:
                        pass
                    info['channels'] = s.get('channels')
                    info['sample_fmt'] = s.get('sample_fmt')
            task_logger.info(f"音频流信息: {info}")
        except subprocess.TimeoutExpired:
            task_logger.warning("获取音频流信息超时")
        except Exception as e:
            task_logger.warning(f"获取音频流信息失败: {str(e)}")
        return info
