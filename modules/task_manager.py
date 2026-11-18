#!/usr/bin/env python
# -*- coding: utf-8 -*-

import os
import sys
import time
import json
import uuid
import html
import logging
import shutil
import threading
import gc
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler
import re
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.executors.pool import ThreadPoolExecutor as APSchedulerThreadPoolExecutor
from apscheduler.schedulers.base import SchedulerNotRunningError
from .utils import get_app_root_dir, get_app_subdir
# 字幕排版已挪到 subtitle.ass_layout，运行时仍从本模块读取这三个名字，
# 因此 patch.object(task_manager, "parse_style_overrides") 继续生效。
from .subtitle_style import (
    apply_style_overrides,
    parse_style_overrides,
    diagnose_style,
)
from .notifications import (
    EVENT_TASK_ADDED,
    EVENT_TASK_COMPLETED,
    EVENT_TASK_FAILED,
    NotificationEvent,
    emit_notification_event,
)
from .tag_presets import (
    BILIBILI_TAG_LIMIT,
    PLATFORM_ACFUN,
    PLATFORM_BILIBILI,
    is_preset_tags_effective,
    merge_tags,
    parse_preset_tags,
    resolve_upload_tags,
)
import subprocess
from typing import Any, Dict
from werkzeug.security import safe_join


# 从 task_manager 拆出的实现。这里再导出公开名字，既有
# `from modules.task_manager import add_task, TaskProcessor` 保持可用。
from .pipeline.cancel import (
    TaskCancelledError,
    _ACTIVE_TASK_IDS,
    _ACTIVE_TASKS_LOCK,
    _GLOBAL_TASK_PROCESSOR_LOCK,
    _TASK_CANCEL_FLAGS,
    _TASK_CANCEL_LOCK,
    _TASK_SCHEDULING_LOCK,
    _get_active_task_ids,
    _is_task_active,
    _mark_task_active,
    _mark_task_inactive,
    _raise_if_cancelled,
    clear_task_cancel,
    get_task_cancel_event,
    is_task_cancelled,
    request_task_cancel,
)
from .pipeline.events import (
    _TASK_EVENT_LOCK,
    _TASK_EVENT_QUEUE_SIZE,
    _TASK_EVENT_SUBSCRIBERS,
    _emit_task_event,
    publish_task_event,
    register_task_updates_listener,
    unregister_task_updates_listener,
)
from .pipeline.store import (
    DB_BUSY_TIMEOUT_MS,
    DB_CONNECT_TIMEOUT_SECONDS,
    DB_DIR,
    DB_PATH,
    DB_WRITE_RETRY_SLEEP_SECONDS,
    DB_WRITE_RETRY_TIMES,
    add_task,
    clear_all_tasks,
    delete_task,
    get_all_tasks,
    get_db_connection,
    get_db_path,
    get_task,
    get_tasks_by_status,
    get_tasks_paginated,
    init_db,
    update_task,
)
from .media.embed import (
    EMBED_TIMEOUT_MAX_SECONDS,
    EMBED_TIMEOUT_MAX_SECONDS_X265,
    EMBED_TIMEOUT_MIN_SECONDS,
    EMBED_TIMEOUT_X265_FACTOR,
    PROCESS_TERMINATE_WAIT_SECONDS,
    MediaEmbedMixin,
)
from .subtitle.ass_layout import SubtitleLayoutMixin
from .media.download import VideoDownloadMixin
from .pipeline.upload import UploadMixin
from .subtitle.translate import SubtitleTranslationMixin


# 导入其他模块
# 这些导入会在函数内部使用，避免循环导入问题
# from modules.youtube_handler import download_video_data
# from modules.ai_enhancer import translate_video_metadata, generate_acfun_tags, recommend_acfun_partition
# from modules.content_moderator import AlibabaCloudModerator
# from modules.acfun_uploader import AcfunUploader

def _get_memory_usage_percent():
    """获取系统内存使用百分比，用于内存感知处理"""
    try:
        try:
            import psutil  # type: ignore
        except Exception:
            # psutil可能未安装，返回保守估计
            return 50.0
        memory = psutil.virtual_memory()
        return memory.percent
    except ImportError:
        # 如果没有psutil，返回一个保守的估计值
        return 50.0
    except Exception:
        return 50.0

def _should_reduce_concurrency():
    """判断是否应该降低并发数以节省内存"""
    memory_percent = _get_memory_usage_percent()
    return memory_percent > 80.0  # 内存使用超过80%时降低并发

# 全局变量
LOGS_DIR = get_app_subdir('logs')
DOWNLOADS_DIR = get_app_subdir('downloads')
METADATA_TRANSLATION_ERROR_CATEGORY = 'metadata_translation_failed'
CONTENT_MODERATION_ERROR_CATEGORY = 'content_moderation_failed'



def _convert_vtt_text_to_srt_text(vtt_content: str) -> str:
    """将普通/YouTube 自动字幕 VTT 文本稳健转换为 SRT 文本。"""
    import re

    def _normalize_newlines(text: str) -> str:
        normalized = str(text or '').replace('\r\n', '\n').replace('\r', '\n')
        if normalized.startswith('\ufeff'):
            normalized = normalized[1:]
        return normalized

    def _time_to_ms(value: str) -> int:
        hours, minutes, seconds = str(value).split(':')
        whole_seconds, milliseconds = str(seconds).split('.')
        return (
            int(hours) * 3600000
            + int(minutes) * 60000
            + int(whole_seconds) * 1000
            + int(milliseconds)
        )

    def _clean_text_line(line: str) -> str:
        text = html.unescape(str(line or ''))
        text = re.sub(r'<\d{2}:\d{2}:\d{2}\.\d{3}>', '', text)
        text = re.sub(r'</?[^>]+>', '', text)
        text = re.sub(r'{[^}]*}', '', text)
        text = re.sub(r'\s+', ' ', text).strip()
        return text

    def _dedupe_lines(lines):
        result = []
        seen = set()
        for line in lines:
            normalized = str(line or '').strip()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            result.append(normalized)
        return result

    def _select_cue_text(cleaned_lines):
        unique_lines = _dedupe_lines(cleaned_lines)
        if not unique_lines:
            return ''
        if len(unique_lines) == 1:
            return unique_lines[0]

        longest_line = max(unique_lines, key=lambda item: (len(item), item))
        if all(line in longest_line for line in unique_lines):
            return longest_line
        return '\n'.join(unique_lines)

    content = _normalize_newlines(vtt_content)
    blocks = re.split(r'\n\s*\n+', content.strip())
    cues = []

    for block in blocks:
        lines = [line.rstrip() for line in block.split('\n')]
        if not lines:
            continue

        first_line = lines[0].strip()
        upper_first_line = first_line.upper()
        if (
            upper_first_line.startswith('WEBVTT')
            or first_line.startswith('Kind:')
            or first_line.startswith('Language:')
            or first_line.startswith('X-TIMESTAMP-MAP=')
            or upper_first_line.startswith('NOTE')
            or upper_first_line == 'STYLE'
            or upper_first_line == 'REGION'
        ):
            continue

        time_line_index = None
        for index, line in enumerate(lines):
            if '-->' in line:
                time_line_index = index
                break
        if time_line_index is None:
            continue

        time_match = re.search(
            r'(?P<start>\d{2}:\d{2}:\d{2}\.\d{3})\s*-->\s*(?P<end>\d{2}:\d{2}:\d{2}\.\d{3})',
            lines[time_line_index],
        )
        if not time_match:
            continue

        payload_lines = lines[time_line_index + 1:]
        has_inline_timestamps = any(
            re.search(r'<\d{2}:\d{2}:\d{2}\.\d{3}>', raw_line or '')
            for raw_line in payload_lines
        )
        cleaned_lines = [_clean_text_line(line) for line in payload_lines]
        text = _select_cue_text(cleaned_lines)
        if not text:
            continue

        start = time_match.group('start')
        end = time_match.group('end')
        cues.append({
            'start': start,
            'end': end,
            'text': text,
            'has_inline_timestamps': has_inline_timestamps,
            'duration_ms': max(0, _time_to_ms(end) - _time_to_ms(start)),
        })

    if not cues:
        return ''

    optimized_cues = []
    consumed_indices = set()
    youtube_like = any(cue['has_inline_timestamps'] for cue in cues)

    if youtube_like:
        for index, cue in enumerate(cues):
            if cue['has_inline_timestamps'] or cue['duration_ms'] > 120 or index == 0:
                continue
            previous_cue = cues[index - 1]
            if not previous_cue['has_inline_timestamps']:
                continue
            optimized_cues.append({
                'start': previous_cue['start'],
                'end': cue['end'],
                'text': cue['text'],
            })
            consumed_indices.add(index - 1)
            consumed_indices.add(index)

        for index, cue in enumerate(cues):
            if index in consumed_indices or not cue['text']:
                continue
            optimized_cues.append({
                'start': cue['start'],
                'end': cue['end'],
                'text': cue['text'],
            })
    else:
        optimized_cues = [
            {'start': cue['start'], 'end': cue['end'], 'text': cue['text']}
            for cue in cues
            if cue['text']
        ]

    optimized_cues.sort(key=lambda cue: (_time_to_ms(cue['start']), _time_to_ms(cue['end'])))

    deduped_cues = []
    for cue in optimized_cues:
        if deduped_cues and deduped_cues[-1]['text'] == cue['text']:
            if _time_to_ms(cue['end']) > _time_to_ms(deduped_cues[-1]['end']):
                deduped_cues[-1]['end'] = cue['end']
            continue
        deduped_cues.append(cue)

    srt_blocks = []
    for index, cue in enumerate(deduped_cues, 1):
        srt_blocks.append(
            f"{index}\n"
            f"{cue['start'].replace('.', ',')} --> {cue['end'].replace('.', ',')}\n"
            f"{cue['text']}"
        )
    return '\n\n'.join(srt_blocks).strip()

# 确保目录存在
os.makedirs(LOGS_DIR, exist_ok=True)
os.makedirs(DOWNLOADS_DIR, exist_ok=True)

# 任务状态定义
TASK_STATES = {
    'PENDING': 'pending',                 # 等待处理
    'DOWNLOADING': 'downloading',         # 正在下载
    'DOWNLOADED': 'downloaded',           # 下载完成
    'ASR_TRANSCRIBING': 'asr_transcribing',  # 语音转写中
    'TRANSLATING_SUBTITLE': 'translating_subtitle',  # 正在翻译字幕
    'ENCODING_VIDEO': 'encoding_video',   # 正在转码视频
    'TRANSLATING': 'translating',         # 正在翻译
    'TAGGING': 'tagging',                 # 正在生成标签
    'PARTITIONING': 'partitioning',       # 正在推荐分区
    'MODERATING': 'moderating',           # 正在内容审核
    'AWAITING_REVIEW': 'awaiting_manual_review',  # 等待人工审核
    'READY_FOR_UPLOAD': 'ready_for_upload',      # 准备上传
    'UPLOADING': 'uploading',             # 正在上传
    'COMPLETED': 'completed',             # 任务完成
    'FAILED': 'failed'                    # 任务失败
}

# 所有"处理中"状态，用于 reset_stuck_tasks 和 recover_interrupted_tasks_to_pending
PROCESSING_STATES = [
    'fetching_info',
    'info_fetched',
    TASK_STATES['TRANSLATING'],
    TASK_STATES['TAGGING'],
    TASK_STATES['PARTITIONING'],
    TASK_STATES['MODERATING'],
    TASK_STATES['DOWNLOADING'],
    TASK_STATES['DOWNLOADED'],
    TASK_STATES['ASR_TRANSCRIBING'],
    TASK_STATES['TRANSLATING_SUBTITLE'],
    TASK_STATES['ENCODING_VIDEO'],
    TASK_STATES['UPLOADING'],
]

# 任务流水线断点续跑（checkpoint）
# - 目标：进程异常退出后，重启可从“最后已完成阶段”继续。
# - 存储：tasks 表新增 pipeline_checkpoint 字段，避免 downloads 目录被清空导致断点丢失。
PIPELINE_CHECKPOINT_FIELD = 'pipeline_checkpoint'

PIPELINE_STAGE_FETCH_INFO = 'fetch_info'
PIPELINE_STAGE_TRANSLATE_CONTENT = 'translate_content'
PIPELINE_STAGE_GENERATE_TAGS = 'generate_tags'
PIPELINE_STAGE_RECOMMEND_PARTITION = 'recommend_partition'
PIPELINE_STAGE_MODERATE_CONTENT = 'moderate_content'
PIPELINE_STAGE_DOWNLOAD_VIDEO = 'download_video'
PIPELINE_STAGE_TRANSLATE_SUBTITLE = 'translate_subtitle'
PIPELINE_STAGE_UPLOAD_TO_ACFUN = 'upload_to_acfun'

PIPELINE_STAGE_ORDER = [
    PIPELINE_STAGE_FETCH_INFO,
    PIPELINE_STAGE_TRANSLATE_CONTENT,
    PIPELINE_STAGE_GENERATE_TAGS,
    PIPELINE_STAGE_RECOMMEND_PARTITION,
    PIPELINE_STAGE_MODERATE_CONTENT,
    PIPELINE_STAGE_DOWNLOAD_VIDEO,
    PIPELINE_STAGE_TRANSLATE_SUBTITLE,
    PIPELINE_STAGE_UPLOAD_TO_ACFUN,
]

UPLOAD_TARGET_ACFUN = 'acfun'
UPLOAD_TARGET_BILIBILI = 'bilibili'
UPLOAD_TARGET_BOTH = 'both'
VALID_UPLOAD_TARGETS = {UPLOAD_TARGET_ACFUN, UPLOAD_TARGET_BILIBILI, UPLOAD_TARGET_BOTH}

PARTITION_FIELD_MAP = {
    UPLOAD_TARGET_ACFUN: {
        'recommended': 'recommended_partition_id_acfun',
        'selected': 'selected_partition_id_acfun',
    },
    UPLOAD_TARGET_BILIBILI: {
        'recommended': 'recommended_partition_id_bilibili',
        'selected': 'selected_partition_id_bilibili',
    },
}

_RESUME_RECOVERY_DONE = False


def normalize_upload_target(upload_target):
    """归一化任务投稿平台，默认 acfun。"""
    target = str(upload_target or '').strip().lower()
    if target not in VALID_UPLOAD_TARGETS:
        return UPLOAD_TARGET_ACFUN
    return target


def resolve_cookie_file_path(
    path_value,
    default_relative_path,
    service_name="Cookie",
    logger_obj=None,
    allow_json_txt_fallback=False
):
    """解析 Cookie 文件路径并可选在 .json/.txt 之间做兼容回退。"""
    raw_path = str(path_value or default_relative_path or '').strip()
    if not raw_path:
        return ''

    app_root = get_app_root_dir()
    resolved_path = raw_path if os.path.isabs(raw_path) else os.path.join(app_root, raw_path)
    resolved_path = os.path.normpath(resolved_path)

    if os.path.exists(resolved_path):
        return resolved_path

    if not allow_json_txt_fallback:
        return resolved_path

    base, ext = os.path.splitext(resolved_path)
    ext_lower = ext.lower()
    if ext_lower == '.json':
        alt_candidates = [base + '.txt']
    elif ext_lower == '.txt':
        alt_candidates = [base + '.json']
    else:
        alt_candidates = [resolved_path + '.json', resolved_path + '.txt']

    for alt_path in alt_candidates:
        if not os.path.exists(alt_path):
            continue
        target_logger = logger_obj if logger_obj is not None else logger
        try:
            target_logger.warning(
                f"{service_name} Cookies路径 {resolved_path} 不存在，自动回退到兼容路径: {alt_path}"
            )
        except Exception:
            pass
        return alt_path

    return resolved_path


def resolve_youtube_cookies_path(config, logger_obj=None):
    """优先解析配置路径，仅在目标缺失时兼容旧版 Cookie 存放位置。"""
    target_logger = logger_obj if logger_obj is not None else logger
    configured_path = str((config or {}).get('YOUTUBE_COOKIES_PATH', 'cookies/yt_cookies.txt') or '').strip()
    resolved_path = resolve_cookie_file_path(
        configured_path,
        'cookies/yt_cookies.txt',
        service_name='YouTube',
        logger_obj=target_logger,
    )
    if os.path.isfile(resolved_path):
        return resolved_path

    app_root = get_app_root_dir()
    file_name = os.path.basename(configured_path or 'yt_cookies.txt')
    legacy_candidates = [os.path.join(app_root, 'config', file_name)]
    if getattr(sys, 'frozen', False) and configured_path and not os.path.isabs(configured_path):
        legacy_candidates.append(os.path.join(app_root, '_internal', configured_path))

    normalized_target = os.path.normcase(os.path.normpath(resolved_path))
    seen = {normalized_target}
    for candidate in legacy_candidates:
        normalized_candidate = os.path.normcase(os.path.normpath(candidate))
        if normalized_candidate in seen:
            continue
        seen.add(normalized_candidate)
        if not os.path.isfile(candidate):
            continue
        target_logger.warning(
            "配置的YouTube Cookies文件不存在，临时回退到旧版路径: %s；"
            "请重新上传或同步 Cookies 以迁移到配置路径。",
            candidate,
        )
        return candidate

    target_logger.warning("指定的YouTube Cookies文件不存在: %s", resolved_path)
    return None


def _get_task_upload_target(task, fallback=UPLOAD_TARGET_ACFUN):
    if not task:
        return normalize_upload_target(fallback)
    task_target = task.get('upload_target')
    if not task_target:
        return normalize_upload_target(fallback)
    return normalize_upload_target(task_target)


def _task_has_upload_response(task, upload_target=None):
    if not task:
        return False
    target = normalize_upload_target(upload_target or task.get('upload_target'))
    if target == UPLOAD_TARGET_BOTH:
        return bool(task.get('acfun_upload_response')) and bool(task.get('bilibili_upload_response'))
    if target == UPLOAD_TARGET_BILIBILI:
        return bool(task.get('bilibili_upload_response'))
    return bool(task.get('acfun_upload_response'))


def _task_has_platform_upload_response(task, platform):
    if not task:
        return False
    p = normalize_upload_target(platform)
    if p == UPLOAD_TARGET_BILIBILI:
        return bool(task.get('bilibili_upload_response'))
    return bool(task.get('acfun_upload_response'))


def _build_task_notification_payload(task, overrides=None) -> dict:
    merged_task = dict(task or {})
    if overrides:
        merged_task.update(overrides)
    return {
        'task_id': str(merged_task.get('id') or '').strip(),
        'youtube_url': str(merged_task.get('youtube_url') or '').strip(),
        'upload_target': normalize_upload_target(merged_task.get('upload_target')),
        'status': str(merged_task.get('status') or '').strip(),
        'video_title_original': str(merged_task.get('video_title_original') or '').strip(),
        'video_title_translated': str(merged_task.get('video_title_translated') or '').strip(),
        'error_message': str(merged_task.get('error_message') or '').strip(),
        'asr_warning_message': str(merged_task.get('asr_warning_message') or '').strip(),
        'subtitle_warning_message': str(merged_task.get('subtitle_warning_message') or '').strip(),
        'acfun_uploaded': bool(merged_task.get('acfun_upload_response')),
        'bilibili_uploaded': bool(merged_task.get('bilibili_upload_response')),
    }


def _get_upload_platforms_for_target(upload_target):
    target = normalize_upload_target(upload_target)
    if target == UPLOAD_TARGET_BOTH:
        return [UPLOAD_TARGET_ACFUN, UPLOAD_TARGET_BILIBILI]
    if target == UPLOAD_TARGET_BILIBILI:
        return [UPLOAD_TARGET_BILIBILI]
    return [UPLOAD_TARGET_ACFUN]


def _get_effective_metadata_limits(upload_target):
    """返回当前任务应执行的标题/简介限制。

    规则：
    - both：按双平台共同最低限制执行
    - bilibili：按 bilibili 限制执行
    - acfun / 其他：按 AcFun 限制执行
    """
    target = normalize_upload_target(upload_target)
    if target == UPLOAD_TARGET_BILIBILI:
        return {
            'title_limit': 80,
            'description_limit': 2000,
        }
    return {
        'title_limit': 50,
        'description_limit': 1000,
    }


def _get_pending_upload_platforms(task, upload_target=None):
    if not task:
        return []
    target = normalize_upload_target(upload_target or task.get('upload_target'))
    platforms = _get_upload_platforms_for_target(target)
    return [p for p in platforms if not _task_has_platform_upload_response(task, p)]


def _has_partial_upload_success(task, upload_target=None):
    if not task:
        return False
    target = normalize_upload_target(upload_target or task.get('upload_target'))
    platforms = _get_upload_platforms_for_target(target)
    if len(platforms) <= 1:
        return False
    has_success = any(_task_has_platform_upload_response(task, p) for p in platforms)
    has_pending = any(not _task_has_platform_upload_response(task, p) for p in platforms)
    return has_success and has_pending


def _get_partition_field_name(platform: str, field_type: str) -> str:
    p = normalize_upload_target(platform)
    return PARTITION_FIELD_MAP.get(p, PARTITION_FIELD_MAP[UPLOAD_TARGET_ACFUN]).get(field_type, '')


def _get_task_partition_id(task, platform, prefer_selected=True):
    if not task:
        return ''

    selected_field = _get_partition_field_name(platform, 'selected')
    recommended_field = _get_partition_field_name(platform, 'recommended')
    selected_value = str(task.get(selected_field) or '').strip()
    recommended_value = str(task.get(recommended_field) or '').strip()

    if prefer_selected:
        if selected_value:
            return selected_value
        if recommended_value:
            return recommended_value
    else:
        if recommended_value:
            return recommended_value
        if selected_value:
            return selected_value

    return ''


def _safe_json_loads(value, default):
    try:
        if value is None:
            return default
        if isinstance(value, (dict, list)):
            return value
        s = str(value).strip()
        if not s:
            return default
        return json.loads(s)
    except Exception:
        return default


def _normalize_tags_list(value):
    parsed = _safe_json_loads(value, [])
    if not isinstance(parsed, list):
        return []
    tags = []
    for tag in parsed:
        tag_text = str(tag or '').strip()
        if tag_text:
            tags.append(tag_text)
    return tags


def _as_bool(value):
    """将配置值稳健转换为布尔值（兼容 bool/int/str）。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() in ('true', '1', 'on', 'yes')


def _as_int(value, default, minimum=None):
    """稳健地将值转换为整数，并可选限制最小值。"""
    try:
        if value is None:
            result = int(default)
        else:
            result = int(str(value).strip())
    except Exception:
        result = int(default)

    if minimum is not None:
        result = max(int(minimum), result)
    return result


def _normalize_task_text(value) -> str:
    return str(value or '').strip()


def _get_missing_required_translation_fields(task, config) -> list:
    if not task:
        return []

    missing_fields = []
    if _as_bool(config.get('TRANSLATE_TITLE', True)) and _normalize_task_text(task.get('video_title_original')):
        if not _normalize_task_text(task.get('video_title_translated')):
            missing_fields.append('title')

    if _as_bool(config.get('TRANSLATE_DESCRIPTION', True)) and _normalize_task_text(task.get('description_original')):
        if not _normalize_task_text(task.get('description_translated')):
            missing_fields.append('description')

    return missing_fields


def _build_missing_translation_review_message(field_names) -> str:
    labels = {
        'title': '标题',
        'description': '简介',
    }
    normalized_fields = [
        str(field_name).strip()
        for field_name in (field_names or [])
        if field_name is not None and str(field_name).strip()
    ]
    readable = [labels.get(field_name, field_name) for field_name in normalized_fields]
    if not readable:
        return "自动翻译未完成，任务已转入人工审核。"
    return f"自动翻译未完成：{'、'.join(readable)}仍缺少有效译文，任务已转入人工审核。"


#: ``_run_subtitle_qc`` 的返回值之一：用户**主动关闭**了字幕质检。
#: 必须与 ``None``（质检启用但没跑成：文件缺失 / 执行异常）区分开 —— 二者
#: 混用会造成「关掉质检反而更严格」的反向回归。
SUBTITLE_QC_DISABLED = 'disabled'


def _is_asr_subtitle_artifact(task_id, subtitle_path) -> bool:
    """判断某字幕文件是否来自 ASR（而非平台自带 / 人工提供）。

    质检与「移出复用范围」都只应约束 ASR 产物：它才是幻觉与时间轴错位的高
    风险来源。外部字幕走原有流程，既不增加 AI 调用成本，也不会被 ASR 的
    历史标记误拦。

    判据**只看文件名**（``asr_*.srt``）。任务级 ``subtitle_quality_state``
    描述的是「ASR 产物的来源可靠度」，它不构成平台字幕的罪名：此前该状态为
    ``degraded`` / ``failed`` 时本函数无条件返回 True，于是任务目录里一份
    干净的 ``video.zh.srt`` 也会被送去做质检，被拒后还会被改名破坏
    （``video.zh.srt`` → ``video.zh.rejected.txt``），用户既有产物直接丢失。
    这与门控层 ``_embed_guard(..., asr_artifact=False)`` 刻意让外部字幕不受
    质量结局约束的意图也自相矛盾。
    """
    try:
        name = os.path.basename(str(subtitle_path or '')).lower()
    except Exception:
        return False
    if not name:
        return False
    if name.startswith('asr_'):
        return True
    return name == f"asr_{str(task_id or '').lower()}.srt"


def _candidate_subtitle_paths(task, task_dir: str) -> list:
    """收集任务相关字幕文件路径，用于判断已烧录视频产物是否仍然新鲜。"""
    paths = []
    if isinstance(task, dict):
        for key in ('subtitle_path_original', 'subtitle_path_translated'):
            value = str(task.get(key) or '').strip()
            if value:
                paths.append(value)
    for ext in ('.srt', '.vtt'):
        paths.append(os.path.join(task_dir, f"asr_{os.path.basename(task_dir)}{ext}"))
    try:
        for name in os.listdir(task_dir):
            if isinstance(name, str) and name.lower().endswith(('.srt', '.vtt')):
                paths.append(os.path.join(task_dir, name))
    except Exception:
        pass
    seen = set()
    unique_paths = []
    for path in paths:
        if path and path not in seen:
            seen.add(path)
            unique_paths.append(path)
    return unique_paths


def _is_asr_enabled(config: dict) -> bool:
    """ASR总开关。"""
    return _as_bool(config.get('SPEECH_RECOGNITION_ENABLED', False))


def _subtitle_block_reasons(
    config: dict,
    quality_state,
    qc_failed,
    *,
    qc_cleared: bool = False,
    qc_enabled=None,
    qc_reason=None,
) -> list:
    """返回烧录拦截原因列表；空列表表示放行。

    ``_subtitle_embed_allowed`` 与调用方的归因记录共用这套判定，避免两处
    逻辑漂移出「拦截了但说不出为什么」的日志。

    ``qc_enabled=None`` 时从 ``config['SUBTITLE_QC_ENABLED']`` 读取，因此
    「关掉质检」的效果对直接调用本函数的代码同样可见，不需要每个调用点
    自己解析开关。

    ``qc_reason`` 是落库的 ``subtitle_qc_reason``（质检结论的归因）。
    ``qc_unavailable`` 表示「质检**没跑成**」（字幕文件缺失 / 执行异常），它与
    「质检给出明确失败结论」是两件事，必须分开判定：

    - 「没跑成」只受逃生口 ``ASR_FAILURE_BLOCKS_EMBED`` 约束 —— README 承诺该
      开关可放宽「质检没跑成」的拦截。此前它被折算成 ``subtitle_qc_failed=1``
      后与明确失败混在一起，于是**逃生口在跨运行场景下无效**：上一轮按默认配置
      落库的 ``qc_failed=1`` 会让用户打开逃生口重跑时仍被拦下。
    - 明确失败结论任何时候都拦截。落库时若已存在明确失败，``subtitle_qc_reason``
      会保留那条结论的原因（不会被写成 ``qc_unavailable``），因此上面这条规则
      不会把历史失败洗白。

    两个开关的作用域**互不重叠**，缺一不可：

    - ``ASR_FAILURE_BLOCKS_EMBED=False``（逃生口）只放宽「ASR 来源结局」
      （``failed`` / ``degraded``）与「质检没跑成」，**不放过**明确的质检失败
      结论 —— 质检结论针对的是字幕文件内容本身，与 ASR 来源可靠度是两件事。
    - ``SUBTITLE_QC_ENABLED=False``（``qc_enabled=False``）表示用户主动放弃
      质检这道防线：此时既不再看历史 ``qc_failed``，也不再要求 ``degraded``
      素材必须通过严格质检（否则「关掉质检」会变成「更严格」，与用户意图相反）。
    """
    asr_blocks = _as_bool(config.get('ASR_FAILURE_BLOCKS_EMBED', True))
    if qc_enabled is None:
        qc_enabled = _as_bool(config.get('SUBTITLE_QC_ENABLED', True))
    qc_active = bool(qc_enabled)
    state = str(quality_state or '').strip().lower()
    qc_unavailable = str(qc_reason or '').strip().lower() == 'qc_unavailable'

    reasons = []
    if state == 'failed' and asr_blocks:
        reasons.append('asr_quality_failed')
    if qc_active and not qc_cleared:
        if qc_unavailable:
            if asr_blocks:
                reasons.append('subtitle_qc_rejected')
        elif _as_bool(qc_failed):
            reasons.append('subtitle_qc_rejected')
    if state == 'degraded' and asr_blocks and qc_active and not qc_cleared:
        reasons.append('asr_quality_degraded_without_strict_qc')
    return reasons


#: 门控原因 → 落库的 ``subtitle_warning_message`` 取值。
#: UI（``templates/partials/task_row.html``、``task_card.html``）按这些取值渲染
#: tooltip，新增原因必须同步补映射，否则界面会退化成显示原始英文键名。
SUBTITLE_BLOCK_WARNING_MESSAGES = {
    'asr_quality_failed': 'asr_failed_block_embed',
    'asr_quality_degraded_without_strict_qc': 'asr_degraded_block_embed',
    'subtitle_qc_rejected': 'subtitle_qc_rejected',
    'qc_unavailable': 'qc_unavailable',
}


def _block_reason_to_warning(reason) -> str:
    """把门控原因翻译成落库用的警告文案。"""
    return SUBTITLE_BLOCK_WARNING_MESSAGES.get(str(reason or '').strip(), 'asr_failed_block_embed')


#: 表示「字幕被质量门控拒绝」的落库警告取值集合。
#: 这些取值一旦出现在任务上，就说明本轮字幕没有烧进成片，因此字幕阶段
#: **不得**被视为已完成（见 ``_is_subtitle_stage_rejected``）。
SUBTITLE_BLOCK_WARNING_VALUES = frozenset(SUBTITLE_BLOCK_WARNING_MESSAGES.values())


def _is_subtitle_stage_rejected(task, config=None) -> bool:
    """字幕阶段是否被质量门控拒绝（因此不该被 checkpoint 标为已完成）。

    判定必须与**烧录门控本身**同口径，否则同一份落库状态会得到两个相反结论，
    后果是用户看得见的那种自相矛盾（R5-3）：

    - 门控（``_subtitle_embed_allowed``）在逃生口 ``ASR_FAILURE_BLOCKS_EMBED=False``
      打开时放行 ``qc_unavailable``（质检没跑成）；
    - 而这里若只看 ``subtitle_qc_failed`` / ``quality_state`` / ``warning_message``，
      同一份 ``{qc_failed:1, reason:'qc_unavailable'}`` 仍被判「被拒」→
      ``_get_completed_stages`` 每轮都剔除 ``translate_subtitle`` →
      入口处的「跳过字幕处理（checkpoint 已完成）」永远不成立 →
      **每轮重跑都重做 ASR**。这正是 ``_mark_subtitle_qc_unavailable`` 的
      docstring 说要消除的行为，逃生口等于只做了一半。

    因此：``config`` 可用时直接复用门控裁决（``not _subtitle_embed_allowed(...)``），
    门控放行即视为「字幕阶段没有被拒」。``config`` 未知时（例如模块级工具函数在
    没有配置的上下文里调用）退回只按任务字段判定的保守口径：宁可多跑一轮字幕
    阶段，也不把「被拒绝的字幕」当成已完成而永久跳过。

    注意「跳过字幕阶段」不会连带丢掉烧录：上传前阶段
    （``_prepare_subtitle_for_upload``）按同一门控独立裁决、并会在需要时补做烧录
    （「检测到已存在翻译字幕，仅补做烧录」），因此本判定放行换来的是
    「不重跑 ASR」而不是「成片没有字幕」。
    """
    if not task:
        return False
    if config is None:
        return _subtitle_rejection_recorded_on_task(task)
    return not _subtitle_embed_allowed(
        config,
        task.get('subtitle_quality_state'),
        task.get('subtitle_qc_failed'),
        # qc_reason 必须一并传入：``qc_unavailable``（质检没跑成）与「明确的质检
        # 失败结论」在门控里是两件事，逃生口只放宽前者。
        qc_reason=task.get('subtitle_qc_reason'),
    )


def _subtitle_rejection_recorded_on_task(task) -> bool:
    """仅凭任务字段判断「字幕阶段被拦过」（配置未知时的保守判定）。

    覆盖三类信号，缺一不可：

    - ``subtitle_qc_failed == 1``：质检给出了明确失败结论；
    - ``subtitle_quality_state == 'failed'``：ASR/VAD 来源不可信；
    - ``subtitle_warning_message`` 落在拦截取值集合内：包含**质检不可用**
      （``qc_unavailable``）。这条尤其关键 —— 质检没跑成时不会有
      ``subtitle_qc_failed`` 结论，若只认布尔字段，该路径的拦截不会阻止
      checkpoint 把字幕阶段标记完成，用户重跑就被永久跳过。
    """
    if not task:
        return False
    if task.get('subtitle_qc_failed') == 1:
        return True
    if str(task.get('subtitle_quality_state') or '').strip().lower() == 'failed':
        return True
    warning = str(task.get('subtitle_warning_message') or '').strip().lower()
    return warning in SUBTITLE_BLOCK_WARNING_VALUES


def _subtitle_embed_allowed(
    config: dict,
    quality_state,
    qc_failed,
    *,
    qc_cleared: bool = False,
    qc_enabled=None,
    qc_reason=None,
    task_logger=None,
) -> bool:
    """统一烧录门控：决定当前字幕是否允许烧进成片。

    - ``quality_state == 'failed'``：ASR/VAD 来源不可信，拒绝烧录；
    - ``quality_state == 'degraded'``：字幕来源退化，必须本次通过严格质检才放行；
    - ``qc_failed`` 为真且本次未通过质检：拒绝烧录；
    - ``qc_reason == 'qc_unavailable'``：质检没跑成，只受逃生口约束（见
      ``_subtitle_block_reasons``）。

    这是「VAD/ASR 已报错却照样烧录」的修复点：此前 ``asr_warning_message``
    只写不读，告警与烧录决策之间没有任何连接边。
    判定细节与开关作用域见 ``_subtitle_block_reasons``。
    """
    reasons = _subtitle_block_reasons(
        config, quality_state, qc_failed,
        qc_cleared=qc_cleared, qc_enabled=qc_enabled, qc_reason=qc_reason,
    )
    if not reasons:
        return True

    if task_logger is not None:
        try:
            task_logger.warning(
                "字幕烧录被门控拦截: quality_state=%s, qc_failed=%s, qc_cleared=%s, "
                "asr_blocks=%s, qc_enabled=%s, reasons=%s",
                str(quality_state or '').strip().lower() or 'ok',
                bool(_as_bool(qc_failed)),
                bool(qc_cleared),
                bool(_as_bool(config.get('ASR_FAILURE_BLOCKS_EMBED', True))),
                bool(_as_bool(config.get('SUBTITLE_QC_ENABLED', True))
                     if qc_enabled is None else qc_enabled),
                reasons,
            )
        except Exception:
            pass
    return False


def _qc_result_allows_embed(qc_result, config) -> bool:
    """质检三态 + 逃生口 → 是否允许烧录：三个调用点的**唯一**判定入口。

    - ``True``：本次质检通过 → 放行；
    - ``SUBTITLE_QC_DISABLED``：用户主动关闭质检 → 放行；
    - ``None``：质检启用但没跑成（文件缺失 / 执行异常）→ 只有逃生口
      ``ASR_FAILURE_BLOCKS_EMBED=False`` 打开时才放行；
    - ``False``：明确的质检失败结论 → 任何情况下都拦截（逃生口不适用）。

    为什么必须收口到一处：此前三个调用点各写一份判定，其中上传前那条路径
    （``_prepare_subtitle_for_upload``）完全不读逃生口，于是同一份 ``asr_*.srt``
    会因为「走哪条分支 / 哪个阶段」而得到相反结论 —— README 承诺该开关可放宽
    「质检没跑成」的拦截，实际上只在部分路径成立。
    """
    if qc_result is True or qc_result is SUBTITLE_QC_DISABLED:
        return True
    if qc_result is None:
        return not _as_bool((config or {}).get('ASR_FAILURE_BLOCKS_EMBED', True))
    return False


def _parse_pipeline_checkpoint(raw_value):
    data = _safe_json_loads(raw_value, {})
    if not isinstance(data, dict):
        data = {}
    completed = data.get('completed', [])
    if not isinstance(completed, list):
        completed = []
    completed = [str(x) for x in completed if x]
    # 过滤未知stage，保持向前兼容
    completed_set = {x for x in completed if x in set(PIPELINE_STAGE_ORDER)}
    return {
        'version': int(data.get('version', 1) or 1),
        'completed': sorted(completed_set, key=lambda s: PIPELINE_STAGE_ORDER.index(s)),
        'updated_at': data.get('updated_at'),
    }


def _infer_completed_stages_from_task(task, config=None):
    """从任务字段推断已完成阶段（checkpoint 之外的兜底来源）。

    ``config`` 只影响字幕阶段的判定：它会被传给 ``_is_subtitle_stage_rejected``
    用于与烧录门控同口径（逃生口 / 质检开关）。为 ``None`` 时退回保守口径，
    不给调用方新增必填参数。
    """
    if not task:
        return set()

    completed = set()

    # 采集信息：有元数据路径/原标题/原始描述，基本可视为完成
    if task.get('metadata_json_path_local') or task.get('video_title_original') or task.get('description_original'):
        completed.add(PIPELINE_STAGE_FETCH_INFO)

    # 翻译标题/描述：任一翻译字段存在即可视为完成
    if task.get('video_title_translated') or task.get('description_translated'):
        completed.add(PIPELINE_STAGE_TRANSLATE_CONTENT)

    # 标签：字段存在（即使是空数组字符串）也认为已跑过
    if task.get('tags_generated') is not None:
        completed.add(PIPELINE_STAGE_GENERATE_TAGS)

    # 分区推荐：有推荐或已选分区
    if (
        task.get('recommended_partition_id_acfun')
        or task.get('selected_partition_id_acfun')
        or task.get('recommended_partition_id_bilibili')
        or task.get('selected_partition_id_bilibili')
    ):
        completed.add(PIPELINE_STAGE_RECOMMEND_PARTITION)

    # 审核：已有审核结果或进入人工审核
    if task.get('moderation_result') or task.get('status') == TASK_STATES['AWAITING_REVIEW']:
        completed.add(PIPELINE_STAGE_MODERATE_CONTENT)

    # 下载：本地视频路径存在且文件存在（优先），或状态已下载完成
    video_path = task.get('video_path_local')
    if task.get('status') == TASK_STATES['DOWNLOADED']:
        completed.add(PIPELINE_STAGE_DOWNLOAD_VIDEO)
    elif isinstance(video_path, str) and video_path and os.path.exists(video_path):
        completed.add(PIPELINE_STAGE_DOWNLOAD_VIDEO)

    # 字幕：译文路径存在才无条件认定完成。若被质量门控拒绝（质检失败 / 质检不可用
    # / ASR 质量结局 failed），不得把字幕阶段推断为已完成 —— 否则任务重跑会被
    # checkpoint 永久跳过，既拿不到新字幕，也无法重新质检。
    # 判定与烧录门控同口径：逃生口打开时「质检没跑成」不再算被拒（门控会放行烧录），
    # 否则用户每轮重跑都会重做 ASR（见 _is_subtitle_stage_rejected）。
    subtitle_rejected = _is_subtitle_stage_rejected(task, config)
    subtitle_keys = (
        ('subtitle_path_translated',)
        if subtitle_rejected
        else ('subtitle_path_original', 'subtitle_path_translated')
    )
    for key in subtitle_keys:
        p = task.get(key)
        if isinstance(p, str) and p and os.path.exists(p):
            completed.add(PIPELINE_STAGE_TRANSLATE_SUBTITLE)
            break

    # 上传：有上传响应或已完成
    upload_target = _get_task_upload_target(task)
    if _task_has_upload_response(task, upload_target) or task.get('status') == TASK_STATES['COMPLETED']:
        completed.add(PIPELINE_STAGE_UPLOAD_TO_ACFUN)

    return completed


def _needs_cover_moderation(task, config=None):
    config = config or {}
    if (not task or not config.get('CONTENT_MODERATION_ENABLED', False)
            or not config.get('CONTENT_MODERATION_COVER_ENABLED', False)):
        return False
    moderation = _safe_json_loads(task.get('moderation_result'), {})
    cover = moderation.get('cover') if isinstance(moderation, dict) else None
    if not isinstance(cover, dict) or not isinstance(cover.get('pass'), bool):
        return True
    if cover['pass']:
        return cover.get('status') != 'passed'
    return cover.get('status') != 'rejected'


def _get_completed_stages(task, config=None):
    cp = _parse_pipeline_checkpoint(task.get(PIPELINE_CHECKPOINT_FIELD) if task else None)
    completed = set(cp.get('completed', []) or [])
    completed |= _infer_completed_stages_from_task(task, config)
    # Legacy placeholders and disabled cover checks cannot satisfy the new policy.
    if _needs_cover_moderation(task, config):
        completed.discard(PIPELINE_STAGE_MODERATE_CONTENT)
    # 差集修正：checkpoint 只能做并集，而 ``_infer_completed_stages_from_task``
    # 只能**加**阶段、无法移除 checkpoint 里已有的项。于是「上一轮已写入
    # translate_subtitle、本轮被门控拒绝」的任务仍会带着该阶段被判定完成，
    # 用户即便修好配置重跑也只会打印「跳过字幕处理（checkpoint 已完成）」。
    # 这里显式剔除；调用方会把本函数结果写回 checkpoint，因此 DB 里的陈旧项
    # 也会随之被清理，不需要额外的迁移。
    # ``config`` 必须一路传下去：剔除判定与烧录门控同口径（见
    # ``_is_subtitle_stage_rejected``），否则逃生口打开时会出现「门控放行烧录、
    # checkpoint 却每轮把字幕阶段判为未完成」的自相矛盾。
    if _is_subtitle_stage_rejected(task, config):
        completed.discard(PIPELINE_STAGE_TRANSLATE_SUBTITLE)
    return completed


def _persist_pipeline_checkpoint(task_id, completed_stages):
    stages = [s for s in PIPELINE_STAGE_ORDER if s in set(completed_stages or set())]
    payload = {
        'version': 1,
        'completed': stages,
        'updated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
    }
    update_task(task_id, silent=True, **{PIPELINE_CHECKPOINT_FIELD: json.dumps(payload, ensure_ascii=False)})


def _mark_stage_done(task_id, completed_stages, stage):
    if stage in completed_stages:
        return completed_stages
    completed_stages = set(completed_stages or set())
    completed_stages.add(stage)
    _persist_pipeline_checkpoint(task_id, completed_stages)
    return completed_stages


def recover_interrupted_tasks_to_pending():
    """将进程意外退出后卡在“处理中状态”的任务恢复为 pending，以便重启后自动续跑。"""
    processing_states = PROCESSING_STATES

    conn = get_db_connection()
    try:
        placeholders = ','.join(['?'] * len(processing_states))
        cursor = conn.execute(
            f'SELECT id, status, upload_target, acfun_upload_response, bilibili_upload_response FROM tasks WHERE status IN ({placeholders})',
            tuple(processing_states)
        )
        rows = cursor.fetchall() or []
        if not rows:
            return 0

        now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        recovered = 0

        for row in rows:
            task_id = row['id']
            status = row['status']
            upload_target = normalize_upload_target(row['upload_target'])
            has_acfun_resp = bool(row['acfun_upload_response'])
            has_bilibili_resp = bool(row['bilibili_upload_response'])
            if upload_target == UPLOAD_TARGET_BOTH:
                has_upload_resp = has_acfun_resp and has_bilibili_resp
                has_partial_upload_resp = has_acfun_resp != has_bilibili_resp
            elif upload_target == UPLOAD_TARGET_BILIBILI:
                has_upload_resp = has_bilibili_resp
                has_partial_upload_resp = False
            else:
                has_upload_resp = has_acfun_resp
                has_partial_upload_resp = False

            # 若上传响应已存在，直接标记为 completed（避免重复上传）
            if has_upload_resp:
                conn.execute(
                    'UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?',
                    (TASK_STATES['COMPLETED'], now_str, task_id)
                )
                recovered += 1
                continue

            # 双平台仅部分成功：恢复为 failed，让 process_task 走“失败点续传”只补失败平台
            if has_partial_upload_resp:
                conn.execute(
                    'UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?',
                    (TASK_STATES['FAILED'], now_str, task_id)
                )
                recovered += 1
                continue

            # 其他处理中状态：恢复为 pending，由流水线根据checkpoint跳过已完成阶段。
            # 同时清理上一轮的字幕质检/质量结局标记，否则这些与任务终身绑定的旧标记
            # 会让恢复后的任务永久跳过字幕生成（既拿不到新字幕，也无法重新质检）。
            conn.execute(
                '''UPDATE tasks
                   SET status = ?, updated_at = ?,
                       subtitle_qc_failed = 0,
                       subtitle_qc_reason = NULL,
                       subtitle_qc_score = NULL,
                       subtitle_qc_checked_at = NULL,
                       subtitle_quality_state = NULL,
                       asr_warning_message = NULL,
                       subtitle_warning_message = NULL
                   WHERE id = ?''',
                (TASK_STATES['PENDING'], now_str, task_id)
            )
            recovered += 1

        conn.commit()
        if recovered:
            logger.info(f"断点续跑：已恢复 {recovered} 个处理中任务为 pending")
        return recovered
    except Exception as e:
        logger.warning(f"断点续跑：恢复处理中任务失败（忽略）：{e}")
        return 0
    finally:
        conn.close()

# WebSocket实时通知功能已移除，改为使用传统页面刷新方式

# 设置日志记录器
def setup_logger(name):
    """
    设置通用日志记录器
    
    Args:
        name: 日志记录器名称
        
    Returns:
        logger: 配置好的日志记录器
    """
    logger = logging.getLogger(name)
    
    if not logger.handlers:  # 避免重复添加处理器
        logger.setLevel(logging.INFO)
        
        # 文件处理器
        log_file = os.path.join(LOGS_DIR, f'{name}.log')
        file_handler = RotatingFileHandler(log_file, maxBytes=10485760, backupCount=5, encoding='utf-8')
        file_formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
        file_handler.setFormatter(file_formatter)
        file_handler.setLevel(logging.INFO)
        logger.addHandler(file_handler)
        
        # 控制台处理器
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(file_formatter)
        console_handler.setLevel(logging.INFO)
        logger.addHandler(console_handler)
        
    return logger

# 任务管理器日志
logger = setup_logger('task_manager')

# 任务处理日志
def setup_task_logger(task_id):
    """
    为特定任务设置日志记录器
    
    Args:
        task_id: 任务ID
        
    Returns:
        logger: 配置好的日志记录器
    """
    log_file = os.path.join(LOGS_DIR, f'task_{task_id}.log')
    logger = logging.getLogger(f'task_{task_id}')
    
    if not logger.handlers:  # 避免重复添加处理器
        logger.setLevel(logging.INFO)
        
        # 文件处理器 - 减少文件大小以降低内存使用
        file_handler = RotatingFileHandler(log_file, maxBytes=5242880, backupCount=3, encoding='utf-8')
        file_formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
        file_handler.setFormatter(file_formatter)
        file_handler.setLevel(logging.INFO)
        logger.addHandler(file_handler)
        
        # 确保消息不会传播到根日志记录器
        logger.propagate = False
    
    return logger


def _get_task_download_dir_real(task_id):
    """返回任务下载目录的规范绝对路径，并确保其位于 downloads 根目录内。"""
    downloads_dir_real = os.path.realpath(DOWNLOADS_DIR)
    try:
        normalized_task_id = str(uuid.UUID(str(task_id or '').strip()))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError("任务ID格式无效") from exc

    safe_task_dir = safe_join(downloads_dir_real, normalized_task_id)
    if not safe_task_dir:
        raise ValueError("任务目录非法")

    task_dir_real = os.path.realpath(safe_task_dir)
    if os.path.commonpath([task_dir_real, downloads_dir_real]) != downloads_dir_real:
        raise ValueError("任务目录非法")

    return task_dir_real


def _is_upload_stage_failure(task, config=None):
    """判断失败任务是否更适合直接重试上传，而不是重跑处理流水线。

    ``config`` 透传给 ``_get_completed_stages``，使字幕阶段是否算完成与烧录门控
    保持同一口径（见 ``_is_subtitle_stage_rejected``）。
    """
    if not task:
        return False

    upload_target = _get_task_upload_target(task)
    if not _get_pending_upload_platforms(task, upload_target):
        return False

    video_path = task.get('video_path_local')
    if not (isinstance(video_path, str) and video_path and os.path.exists(video_path)):
        return False

    if _task_has_upload_response(task, upload_target):
        return _has_partial_upload_success(task, upload_target)

    error_text = str(task.get('error_message') or '').lower()
    upload_markers = ('上传', 'upload', 'bilibili', 'acfun', '账号未登录', 'preupload')
    if any(marker in error_text for marker in upload_markers):
        return True

    completed = _get_completed_stages(task, config)
    return PIPELINE_STAGE_DOWNLOAD_VIDEO in completed and PIPELINE_STAGE_TRANSLATE_SUBTITLE in completed


def _start_background_upload_retry(task_id, config):
    """后台重试上传，避免批量重试请求阻塞 Web 响应。"""
    import threading

    def run_upload_retry():
        try:
            force_upload_task(task_id, config)
        except Exception as exc:
            logger.error(f"后台上传重试任务 {task_id} 出错: {exc}")

    threading.Thread(target=run_upload_retry, daemon=True).start()

def retry_failed_tasks(config=None):
    """重新调度所有失败的任务。"""
    failed_tasks = get_tasks_by_status(TASK_STATES['FAILED'])
    total = len(failed_tasks)
    if total == 0:
        return {
            'total': 0,
            'scheduled': 0,
            'failed_ids': []
        }

    if config is None:
        try:
            from modules.config_manager import load_config
            config = load_config()
        except Exception as e:
            logger.warning(f"加载配置失败，将使用默认配置重试失败任务: {e}")
            config = {}

    scheduled = 0
    failed_ids = []

    for task in failed_tasks:
        task_id = task['id']
        original_error = task.get('error_message')
        upload_target = _get_task_upload_target(task)

        # 失败状态兜底修复：目标平台其实都成功时，直接纠正为 completed，避免重复调度
        if _task_has_upload_response(task, upload_target):
            update_task(
                task_id,
                silent=True,
                status=TASK_STATES['COMPLETED'],
                error_message=None,
                upload_progress=None
            )
            continue

        if _is_upload_stage_failure(task, config):
            update_task(
                task_id,
                silent=True,
                status=TASK_STATES['READY_FOR_UPLOAD'],
                error_message=None,
                upload_progress=None
            )
            _start_background_upload_retry(task_id, config)
            scheduled += 1
            continue

        # 对“部分平台已成功”的失败任务，保留 FAILED 状态以触发 process_task 的失败点续传分支
        next_status = TASK_STATES['FAILED'] if _has_partial_upload_success(task, upload_target) else TASK_STATES['PENDING']
        update_task(
            task_id,
            silent=True,
            status=next_status,
            error_message=None,
            upload_progress=None
        )

        if start_task(task_id, config):
            scheduled += 1
        else:
            failed_ids.append(task_id)
            update_task(
                task_id,
                silent=True,
                status=TASK_STATES['FAILED'],
                error_message=original_error or '批量重试调度失败，请稍后重试。'
            )

    return {
        'total': total,
        'scheduled': scheduled,
        'failed_ids': failed_ids
    }


def is_metadata_translation_retryable(task):
    """判断人工审核任务是否由元数据自动翻译失败导致。"""
    if not task or task.get('status') != TASK_STATES['AWAITING_REVIEW']:
        return False

    return task.get('error_category') == METADATA_TRANSLATION_ERROR_CATEGORY


def get_metadata_translation_retry_block_reason(task, config) -> str | None:
    """返回当前配置下不能重新翻译的原因；可重试时返回 None。"""
    config = config or {}
    can_translate_title = (
        _as_bool(config.get('TRANSLATE_TITLE', True))
        and bool(_normalize_task_text(task.get('video_title_original')))
    )
    can_translate_description = (
        _as_bool(config.get('TRANSLATE_DESCRIPTION', True))
        and bool(_normalize_task_text(task.get('description_original')))
    )
    if not can_translate_title and not can_translate_description:
        return '当前未启用可用的标题或简介自动翻译，无法重新翻译。'
    if not _normalize_task_text(config.get('OPENAI_API_KEY')):
        return '未配置 OpenAI API Key，无法重新翻译。'
    return None


def retry_metadata_translation_task(task_id, config=None):
    """重置元数据翻译及其下游阶段，并重新调度任务。"""
    task = get_task(task_id)
    if not is_metadata_translation_retryable(task):
        return False
    if get_metadata_translation_retry_block_reason(task, config):
        return False
    if _is_task_active(task_id):
        logger.warning(f"任务 {task_id} 仍在运行，拒绝重新调度元数据翻译")
        return False

    reset_stages = {
        PIPELINE_STAGE_TRANSLATE_CONTENT,
        PIPELINE_STAGE_GENERATE_TAGS,
        PIPELINE_STAGE_RECOMMEND_PARTITION,
        PIPELINE_STAGE_MODERATE_CONTENT,
    }
    checkpoint = _parse_pipeline_checkpoint(task.get(PIPELINE_CHECKPOINT_FIELD))
    remaining_stages = [
        stage for stage in checkpoint.get('completed', [])
        if stage not in reset_stages
    ]
    reset_checkpoint = json.dumps({
        'version': checkpoint.get('version', 1),
        'completed': remaining_stages,
        'updated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
    }, ensure_ascii=False)

    reset_values = {
        'status': TASK_STATES['PENDING'],
        'video_title_translated': '',
        'description_translated': '',
        'tags_generated': None,
        'recommended_partition_id': None,
        'recommended_partition_id_acfun': None,
        'recommended_partition_id_bilibili': None,
        'selected_partition_id': None,
        'selected_partition_id_acfun': None,
        'selected_partition_id_bilibili': None,
        'moderation_result': None,
        'error_message': None,
        'error_category': None,
        'pipeline_checkpoint': reset_checkpoint,
        'upload_progress': None,
    }
    rollback_values = {
        key: task.get(key)
        for key in reset_values
        if key != 'status'
    }
    rollback_values['status'] = task.get('status')

    if not update_task(task_id, silent=True, **reset_values):
        return False

    try:
        if start_task(task_id, config):
            return True
    except Exception as exc:
        logger.error(f"重新调度元数据翻译任务 {task_id} 出错: {exc}")

    if not update_task(task_id, silent=True, **rollback_values):
        logger.warning(f"重新调度元数据翻译任务 {task_id} 失败，且状态回滚失败")
    return False

def delete_task_files(task_id):
    """
    删除任务相关文件

    Args:
        task_id: 任务ID

    Returns:
        success: 删除是否成功
    """
    try:
        task_dir_real = _get_task_download_dir_real(task_id)
    except (ValueError, OSError) as e:
        logger.error(f"验证任务 {task_id} 路径失败: {str(e)}")
        return False

    if os.path.exists(task_dir_real):
        try:
            shutil.rmtree(task_dir_real)
            logger.info(f"任务 {task_id} 的下载目录已删除: {task_dir_real}")
        except Exception as e:
            logger.error(f"删除任务 {task_id} 的下载目录失败: {str(e)}")
            return False

    # 封面图片现在保存在downloads目录中，无需单独删除

    return True

# 全局上传队列锁
upload_queue_lock = threading.Lock()
upload_semaphore = None
task_semaphore = None

def init_upload_semaphore(max_concurrent_uploads=1):
    """初始化上传信号量"""
    global upload_semaphore
    logger.info(f"初始化上传信号量，最大并发上传数: {max_concurrent_uploads}")
    upload_semaphore = threading.Semaphore(max_concurrent_uploads)
    logger.info(f"上传信号量初始化完成: {upload_semaphore}")

def init_task_semaphore(max_concurrent_tasks=3):
    """初始化任务并发信号量"""
    global task_semaphore
    logger.info(f"初始化任务信号量，最大并发任务数: {max_concurrent_tasks}")
    task_semaphore = threading.Semaphore(max_concurrent_tasks)
    logger.info(f"任务信号量初始化完成: {task_semaphore}")

def reset_stuck_tasks(skip_active=False, cancel_active=False):
    """重置卡住的任务"""
    import time
    current_time = time.time()
    
    # 定义超时时间（30分钟）
    timeout_seconds = 30 * 60
    
    conn = get_db_connection()
    try:
        # 查找可能卡住的任务（状态为处理中但长时间未更新）
        cursor = conn.execute('''
            SELECT id, status, updated_at 
            FROM tasks 
            WHERE status IN ({}) 
            AND datetime(updated_at) < datetime('now', '-30 minutes')
        '''.format(','.join(['?'] * len(PROCESSING_STATES))), PROCESSING_STATES)
        
        stuck_tasks = cursor.fetchall()
        
        if stuck_tasks:
            logger.warning(f"发现 {len(stuck_tasks)} 个可能卡住的任务，正在重置...")
            reset_count = 0
            
            for task in stuck_tasks:
                task_id = task[0]
                old_status = task[1]
                updated_at = task[2]

                if skip_active and _is_task_active(task_id):
                    if cancel_active:
                        request_task_cancel(task_id)
                    logger.warning(
                        f"任务 {task_id[:8]}... 仍处于活动线程中，已跳过自动重置"
                    )
                    continue
                
                # 重置为失败状态；同时清理字幕质检/质量结局标记，
                # 避免重跑时被残留标记永久锁死。
                conn.execute('''
                    UPDATE tasks 
                    SET status = ?, error_message = ?, updated_at = ?,
                        subtitle_qc_failed = 0,
                        subtitle_qc_reason = NULL,
                        subtitle_qc_score = NULL,
                        subtitle_qc_checked_at = NULL,
                        subtitle_quality_state = NULL,
                        asr_warning_message = NULL,
                        subtitle_warning_message = NULL
                    WHERE id = ?
                ''', (TASK_STATES['FAILED'], 
                      f"任务超时重置 (原状态: {old_status})",
                      datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                      task_id))
                reset_count += 1
                
                logger.info(f"重置任务 {task_id[:8]}... 从 {old_status} 到 failed")
            
            conn.commit()
            return reset_count
        else:
            logger.info("没有发现卡住的任务")
            return 0
            
    except Exception as e:
        logger.error(f"重置卡住任务时出错: {str(e)}")
        return 0
    finally:
        conn.close()

def validate_cookies(cookies_path, service_name="Unknown"):
    """验证cookies文件的有效性"""
    if not cookies_path or not os.path.exists(cookies_path):
        logger.warning(f"{service_name} Cookies文件不存在: {cookies_path}")
        return False, f"Cookies文件不存在: {cookies_path}"
    
    try:
        with open(cookies_path, 'r', encoding='utf-8') as f:
            content = f.read().strip()
        
        if not content:
            logger.warning(f"{service_name} Cookies文件为空")
            return False, "Cookies文件为空"
        
        # 基本格式验证
        if content.startswith('# Netscape HTTP Cookie File') or '\t' in content:
            # Netscape格式（#HttpOnly_ 前缀行是带 HttpOnly 标志的合法数据行，不算注释）
            lines = [
                line for line in content.split('\n')
                if line.strip() and (not line.startswith('#') or line.startswith('#HttpOnly_'))
            ]
            if not lines:
                return False, "Netscape格式cookies文件没有有效的cookie条目"
            logger.debug(f"{service_name} Netscape格式cookies, {len(lines)} 个条目")
        elif content.startswith('[') or content.startswith('{'):
            # JSON格式
            import json
            cookies_data = json.loads(content)
            if not cookies_data:
                return False, "JSON格式cookies文件为空数组"
            logger.debug(f"{service_name} JSON格式cookies, {len(cookies_data)} 个条目")
        else:
            # YouTube 允许 CSV / 空格分隔的文本表格格式，由后续的自动转换处理
            if service_name == "YouTube":
                try:
                    from modules.tools.convert_cookies import convert_to_netscape_lines
                    if convert_to_netscape_lines(content) is not None:
                        logger.debug(f"{service_name} 文本表格格式cookies可自动转换为Netscape")
                        return True, "Cookies文件格式正确"
                except Exception:
                    pass
            logger.warning(f"{service_name} Cookies文件格式不明")
            return False, "Cookies文件格式不明"
        
        return True, "Cookies文件格式正确"
        
    except Exception:
        logger.exception("验证%s Cookies文件时出错", service_name)
        return False, "验证cookies文件出错，请查看服务日志。"

# 任务处理逻辑。下载、上传、翻译、字幕排版和烧录都在 mixin 里。
# process_task 仍是流水线编排入口。
class TaskProcessor(
    SubtitleTranslationMixin,
    VideoDownloadMixin,
    UploadMixin,
    SubtitleLayoutMixin,
    MediaEmbedMixin,
):
    """任务处理器，负责任务的执行和状态管理"""
    
    def __init__(self, config=None):
        """
        初始化任务处理器
        
        Args:
            config: 配置字典，包含各种API的配置信息
        """
        self.config = dict(config or {})
        self._current_max_concurrent_tasks = _as_int(self.config.get('MAX_CONCURRENT_TASKS', 2), 2, minimum=1)
        self._current_max_concurrent_uploads = _as_int(self.config.get('MAX_CONCURRENT_UPLOADS', 1), 1, minimum=1)
        self._runtime_limit_refresh_pending = False
        self._last_deferred_limit_signature = None
        
        # 初始化上传信号量
        init_upload_semaphore(self._current_max_concurrent_uploads)
        # 初始化任务并发信号量
        init_task_semaphore(self._current_max_concurrent_tasks)
        
        self.scheduler = BackgroundScheduler(
            executors={
                'default': APSchedulerThreadPoolExecutor(max_workers=max(2, self._current_max_concurrent_tasks))
            },
            job_defaults={
                'coalesce': False,
                'max_instances': 1  # 每个任务只能有一个实例在运行，避免重复执行同一任务
            }
        )
        self.scheduler.start()
        logger.info(
            f"任务处理器初始化完成 - 最大并发任务: {self._current_max_concurrent_tasks}, "
            f"最大并发上传: {self._current_max_concurrent_uploads}"
        )

        try:
            # 断点续跑：仅在进程生命周期内执行一次，避免配置变更重建处理器时干扰运行中任务
            global _RESUME_RECOVERY_DONE
            if not _RESUME_RECOVERY_DONE:
                try:
                    recover_interrupted_tasks_to_pending()
                finally:
                    _RESUME_RECOVERY_DONE = True

            self._register_periodic_jobs()
        except Exception:
            # scheduler 已启动但初始化后续步骤失败时，必须回收它再向调用方抛错。
            self.shutdown()
            raise

    def _register_periodic_jobs(self):
        """注册或刷新周期性任务。"""
        try:
            scan_interval = _as_int(self.config.get('PENDING_SCAN_INTERVAL_SECONDS', 30), 30, minimum=5)
            self.scheduler.add_job(
                self._check_and_start_next_pending_task,
                'interval',
                seconds=scan_interval,
                id='pending_scanner',
                replace_existing=True
            )
            logger.info(f"已启动定时扫描pending任务：每 {scan_interval} 秒")
        except Exception as e:
            logger.warning(f"注册定时扫描pending任务失败（不影响主流程）：{e}")

        try:
            stuck_check_interval = _as_int(
                self.config.get('STUCK_TASK_CHECK_INTERVAL_SECONDS', 300),
                300,
                minimum=30,
            )
            self.scheduler.add_job(
                self._recover_stuck_tasks,
                'interval',
                seconds=stuck_check_interval,
                id='stuck_task_recovery',
                replace_existing=True
            )
            logger.info(f"已启动卡住任务扫描：每 {stuck_check_interval} 秒")
        except Exception as e:
            logger.warning(f"注册卡住任务扫描失败（不影响主流程）：{e}")

    def _refresh_runtime_limits(self, force=False):
        """按当前配置刷新并发上限；运行中有活动任务时延后生效。"""
        desired_tasks = _as_int(self.config.get('MAX_CONCURRENT_TASKS', 2), 2, minimum=1)
        desired_uploads = _as_int(self.config.get('MAX_CONCURRENT_UPLOADS', 1), 1, minimum=1)

        if (
            desired_tasks == self._current_max_concurrent_tasks
            and desired_uploads == self._current_max_concurrent_uploads
            and not self._runtime_limit_refresh_pending
        ):
            return True

        active_task_count = len(_get_active_task_ids())
        if active_task_count > 0 and not force:
            signature = (desired_tasks, desired_uploads)
            if signature != self._last_deferred_limit_signature:
                logger.info(
                    "检测到并发配置变更，但当前仍有活动任务运行；"
                    f"新的任务/上传并发上限将延后到空闲时生效: {signature}"
                )
                self._last_deferred_limit_signature = signature
            self._runtime_limit_refresh_pending = True
            return False

        with _TASK_SCHEDULING_LOCK:
            init_task_semaphore(desired_tasks)
            init_upload_semaphore(desired_uploads)
            self._current_max_concurrent_tasks = desired_tasks
            self._current_max_concurrent_uploads = desired_uploads
            self._runtime_limit_refresh_pending = False
            self._last_deferred_limit_signature = None
        logger.info(
            f"并发配置已生效 - 最大并发任务: {desired_tasks}, 最大并发上传: {desired_uploads}"
        )
        return True

    def refresh_config(self, config=None):
        """刷新运行时配置，而不是重建处理器实例。"""
        with _GLOBAL_TASK_PROCESSOR_LOCK:
            self.config = dict(config or {})
            self._register_periodic_jobs()
            self._refresh_runtime_limits(force=False)

    def _enqueue_task_retry(self, task_id, delay_seconds=2):
        """为未抢到执行位的任务注册一次延迟重试，避免创建等待线程。"""
        try:
            self.scheduler.add_job(
                self.schedule_task,
                'date',
                run_date=datetime.now() + timedelta(seconds=max(1, int(delay_seconds))),
                id=f'queued_retry_{task_id}',
                replace_existing=True,
                args=[task_id],
            )
        except Exception as e:
            logger.debug(f"为任务 {task_id} 注册延迟重试失败，将依赖定时扫描器: {e}")

    def _recover_stuck_tasks(self):
        """周期性清理非活动的卡住任务，并对活动卡住任务发送取消请求。"""
        try:
            reset_count = reset_stuck_tasks(skip_active=True, cancel_active=True)
            if reset_count > 0:
                logger.warning(f"自动恢复了 {reset_count} 个卡住任务，准备继续调度 pending 队列")
                self._check_and_start_next_pending_task()
        except Exception as e:
            logger.warning(f"自动恢复卡住任务失败（忽略）：{e}")
    
    def shutdown(self):
        """安全关闭调度器"""
        with _GLOBAL_TASK_PROCESSOR_LOCK:
            try:
                if self.scheduler:
                    # 防止对未运行的调度器调用shutdown引发异常
                    self.scheduler.shutdown(wait=False)
            except SchedulerNotRunningError:
                # 已经停止则忽略
                pass
            except Exception as e:
                logger.warning(f"关闭任务处理器时发生异常: {e}")
            finally:
                logger.info("任务处理器已关闭")
    
    def schedule_task(self, task_id):
        """
        调度任务处理
        
        Args:
            task_id: 任务ID
        
        Returns:
            job_id: 调度作业ID
        """
        try:
            with _TASK_SCHEDULING_LOCK:
                self._refresh_runtime_limits(force=False)

                if _is_task_active(task_id):
                    logger.warning(f"任务 {task_id} 已有活动线程，跳过重复调度")
                    return f"thread_{task_id}"

                queued_job_id = f"queued_retry_{task_id}"
                try:
                    existing_retry_job = self.scheduler.get_job(queued_job_id)
                except Exception:
                    existing_retry_job = None

                # 检查任务当前状态
                task = get_task(task_id)
                if not task:
                    logger.error(f"任务 {task_id} 不存在")
                    return None

                if task['status'] not in [TASK_STATES['PENDING'], TASK_STATES['FAILED']]:
                    logger.warning(f"任务 {task_id} 状态为 {task['status']}，不能调度")
                    return None

                global task_semaphore
                if task_semaphore is None:
                    init_task_semaphore(self._current_max_concurrent_tasks)
                assert task_semaphore is not None, "task_semaphore 应该已经初始化"
                local_task_semaphore = task_semaphore

                slot_acquired = local_task_semaphore.acquire(blocking=False)
                if not slot_acquired:
                    if existing_retry_job is None:
                        self._enqueue_task_retry(task_id, delay_seconds=2)
                    logger.info(
                        f"任务 {task_id} 当前达到并发上限，保留原状态等待后续调度，不再创建等待线程"
                    )
                    return queued_job_id

                import threading
                release_slot_on_failure = True
                task_marked_active = False

                def run_task_wrapper():
                    try:
                        logger.info(f"任务 {task_id} 开始在线程中执行")
                        self.process_task(
                            task_id,
                            slot_already_acquired=True,
                            acquired_task_semaphore=local_task_semaphore,
                        )
                    except Exception as e:
                        logger.error(f"任务 {task_id} 执行出错: {str(e)}")
                        import traceback
                        logger.error(traceback.format_exc())
                        update_task(task_id, status=TASK_STATES['FAILED'], error_message=f"执行出错: {str(e)}")
                    finally:
                        _mark_task_inactive(task_id)

                try:
                    if not _mark_task_active(task_id):
                        logger.warning(f"任务 {task_id} 在线程启动前检测到重复调度，已跳过")
                        return f"thread_{task_id}"
                    task_marked_active = True

                    thread = threading.Thread(
                        target=run_task_wrapper,
                        name=f"task_{task_id}",
                        daemon=True
                    )
                    thread.start()
                    release_slot_on_failure = False
                except Exception:
                    raise
                finally:
                    if release_slot_on_failure:
                        if task_marked_active:
                            _mark_task_inactive(task_id)
                        local_task_semaphore.release()

                logger.info(f"任务 {task_id} 已在后台线程启动")
                return f"thread_{task_id}"
            
        except Exception as e:
            logger.error(f"调度任务 {task_id} 失败: {str(e)}")
            import traceback
            logger.error(traceback.format_exc())
            # 更新任务状态为失败
            update_task(task_id, status=TASK_STATES['FAILED'], error_message=f"调度失败: {str(e)}")
            return None
    
    def process_task(self, task_id, slot_already_acquired=False, acquired_task_semaphore=None):
        """
        处理任务，包括采集信息、内容审核、下载、上传等步骤
        """
        task_logger = setup_task_logger(task_id)
        task_logger.info(f"开始处理任务 {task_id}")
        slot_acquired = bool(slot_already_acquired)
        active_task_semaphore = acquired_task_semaphore
        if slot_already_acquired and active_task_semaphore is None:
            task_logger.warning("slot_already_acquired=True 但未提供信号量实例，改为重新获取并发配额")
            slot_acquired = False
        if active_task_semaphore is None:
            global task_semaphore
            with _TASK_SCHEDULING_LOCK:
                active_task_semaphore = task_semaphore
                if active_task_semaphore is None:
                    # 兜底：根据当前配置重新初始化
                    task_logger.warning("task_semaphore 为 None，正在重新初始化...")
                    init_task_semaphore(self._current_max_concurrent_tasks)
                    active_task_semaphore = task_semaphore
            task_logger.info(f"task_semaphore 当前值: {active_task_semaphore}")
            if active_task_semaphore is None:
                task_logger.error("task_semaphore 初始化失败，无法继续执行任务")
                return
        try:
            task = get_task(task_id)
            if not task:
                logger.error(f"任务 {task_id} 不存在")
                return

            if not slot_acquired:
                task_logger.info("等待获取任务并发配额...")
                try:
                    while True:
                        if is_task_cancelled(task_id):
                            raise TaskCancelledError("任务已取消")
                        if active_task_semaphore.acquire(timeout=0.5):
                            slot_acquired = True
                            break
                    task_logger.info("获得任务并发配额，开始执行任务")
                except TaskCancelledError:
                    task_logger.info("任务在等待并发配额时已取消")
                    update_task(task_id, status=TASK_STATES['FAILED'], error_message="任务已取消")
                    return
                except Exception as _e:
                    task_logger.error(f"获取任务并发配额失败: {_e}")
                    return
            else:
                task_logger.info("任务已在调度阶段获得任务并发配额")

            _raise_if_cancelled(task_id, task_logger)
            # 断点续跑：读取checkpoint + 根据现有任务字段推断已完成阶段
            completed_stages = _get_completed_stages(task, getattr(self, 'config', None))
            # 将推断结果写回checkpoint（幂等），使后续重启更稳定
            try:
                _persist_pipeline_checkpoint(task_id, completed_stages)
            except Exception:
                pass

            # 重新获取任务，避免排队等待期间状态变化导致基于旧快照决策
            task = get_task(task_id) or task
            if task is None:
                task_logger.error("任务对象为None，终止任务处理")
                return

            # 上传失败恢复优化：若目标平台已全部成功则直接完成；若仅部分成功则仅重试失败平台
            upload_target = _get_task_upload_target(task)
            pending_platforms = _get_pending_upload_platforms(task, upload_target)
            if task.get('status') == TASK_STATES['FAILED'] and not pending_platforms and _task_has_upload_response(task, upload_target):
                task_logger.info("检测到目标平台已全部上传成功，跳过重复流程并标记完成")
                completed_stages = _mark_stage_done(task_id, completed_stages, PIPELINE_STAGE_UPLOAD_TO_ACFUN)
                update_task(task_id, status=TASK_STATES['COMPLETED'], error_message=None, upload_progress=None)
                return

            if task.get('status') == TASK_STATES['FAILED'] and _has_partial_upload_success(task, upload_target):
                done_platforms = [
                    p for p in _get_upload_platforms_for_target(upload_target)
                    if _task_has_platform_upload_response(task, p)
                ]
                task_logger.info(
                    "检测到部分平台已上传成功，进入失败点续传模式。"
                    f" 已完成平台: {done_platforms}，待重试平台: {pending_platforms}"
                )
                self._upload_to_target(task_id, task_logger)
                task = get_task(task_id)
                if task and _task_has_upload_response(task, upload_target):
                    completed_stages = _mark_stage_done(task_id, completed_stages, PIPELINE_STAGE_UPLOAD_TO_ACFUN)
                    if task.get('status') != TASK_STATES['COMPLETED']:
                        update_task(task_id, status=TASK_STATES['COMPLETED'], error_message=None, upload_progress=None)
                        task_logger.info("失败平台重试成功，任务已标记为完成")
                return

            # 1. 采集视频信息（只获取元数据和封面，不下载视频文件）
            task_logger.info(f"开始处理任务，当前task对象: {task}")
            if task is None:
                task_logger.error("任务对象为None，终止任务处理")
                return

            if PIPELINE_STAGE_FETCH_INFO in completed_stages:
                task_logger.info("跳过采集视频信息（checkpoint已完成）")
            else:
                self._fetch_video_info(task_id, task['youtube_url'], task_logger)
                _raise_if_cancelled(task_id, task_logger)
                task = get_task(task_id)
                task_logger.info(f"重新获取任务对象: {task}")
                if task is None:
                    task_logger.error("重新获取任务对象为None，终止任务处理")
                    return
                if task['status'] == TASK_STATES['FAILED']:
                    task_logger.error("采集视频信息失败，终止任务处理")
                    return
                completed_stages = _mark_stage_done(task_id, completed_stages, PIPELINE_STAGE_FETCH_INFO)

            # 2. 翻译/标签/分区推荐（如有需要）
            if self.config.get('TRANSLATE_TITLE', True) or self.config.get('TRANSLATE_DESCRIPTION', True):
                if PIPELINE_STAGE_TRANSLATE_CONTENT in completed_stages:
                    task_logger.info("跳过标题/描述翻译（checkpoint已完成）")
                else:
                    ok = self._translate_content(task_id, task_logger)
                    task = get_task(task_id)
                    if ok:
                        completed_stages = _mark_stage_done(task_id, completed_stages, PIPELINE_STAGE_TRANSLATE_CONTENT)
                    elif task is not None and task['status'] == TASK_STATES['AWAITING_REVIEW']:
                        task_logger.info("元数据翻译失败，任务已转入人工审核")
                        return
                    elif task is not None and task['status'] == TASK_STATES['FAILED']:
                        task_logger.error("元数据翻译失败，终止任务处理")
                        return
                _raise_if_cancelled(task_id, task_logger)

            # 预设标签生效时也要跑这一阶段：关闭「自动生成标签」但启用预设时，
            # 该阶段负责把预设标签落到任务上（任务卡展示、分区推荐上下文、checkpoint
            # 与上传路径口径一致），此时函数内部不会调用标签 AI。
            if self.config.get('GENERATE_TAGS', True) or is_preset_tags_effective(self.config):
                if PIPELINE_STAGE_GENERATE_TAGS in completed_stages:
                    task_logger.info("跳过标签生成（checkpoint已完成）")
                else:
                    ok = self._generate_tags(task_id, task_logger)
                    if ok:
                        completed_stages = _mark_stage_done(task_id, completed_stages, PIPELINE_STAGE_GENERATE_TAGS)
                _raise_if_cancelled(task_id, task_logger)

            if self.config.get('RECOMMEND_PARTITION', False):
                if PIPELINE_STAGE_RECOMMEND_PARTITION in completed_stages:
                    task_logger.info("跳过分区推荐（checkpoint已完成）")
                else:
                    ok = self._recommend_partition(task_id, task_logger)
                    if ok:
                        completed_stages = _mark_stage_done(task_id, completed_stages, PIPELINE_STAGE_RECOMMEND_PARTITION)

                task = get_task(task_id)
                if task is None:
                    task_logger.warning("任务对象为 None，跳过分区自动选择")
                _raise_if_cancelled(task_id, task_logger)

            # 3. 内容审核（如启用）
            if self.config.get('CONTENT_MODERATION_ENABLED', False):
                if PIPELINE_STAGE_MODERATE_CONTENT in completed_stages:
                    task_logger.info("跳过内容审核（checkpoint已完成）")
                else:
                    self._moderate_content(task_id, task_logger)
                    task = get_task(task_id)
                    if task is not None and task['status'] == TASK_STATES['AWAITING_REVIEW']:
                        # 审核不通过，进入人工审核；该阶段也视为已完成
                        completed_stages = _mark_stage_done(task_id, completed_stages, PIPELINE_STAGE_MODERATE_CONTENT)
                        task_logger.info("内容需要人工审核，暂停任务处理")
                        return
                    completed_stages = _mark_stage_done(task_id, completed_stages, PIPELINE_STAGE_MODERATE_CONTENT)
                _raise_if_cancelled(task_id, task_logger)

            # 4. 审核通过后才下载视频文件
            task = get_task(task_id)
            if task is None:
                task_logger.error("任务对象为 None，无法下载视频文件")
                return

            if PIPELINE_STAGE_DOWNLOAD_VIDEO in completed_stages:
                task_logger.info("跳过下载视频文件（checkpoint已完成）")
            else:
                download_outcome = self._download_video_file(task_id, task['youtube_url'], task_logger)
                if download_outcome == 'held':
                    task_logger.info("下载被闸门暂停，任务回到等待队列；已完成下载的编码和上传会继续")
                    return
                _raise_if_cancelled(task_id, task_logger)
                task = get_task(task_id)
                if task is not None and task['status'] == TASK_STATES['FAILED']:
                    task_logger.error("下载视频文件失败，终止任务处理")
                    return
                completed_stages = _mark_stage_done(task_id, completed_stages, PIPELINE_STAGE_DOWNLOAD_VIDEO)

            # 5. 字幕处理（翻译或烧录启用时）
            subtitle_translation_enabled = _as_bool(self.config.get('SUBTITLE_TRANSLATION_ENABLED', False))
            subtitle_embed_enabled = _as_bool(self.config.get('SUBTITLE_EMBED_IN_VIDEO', True))
            if subtitle_translation_enabled or subtitle_embed_enabled:
                if PIPELINE_STAGE_TRANSLATE_SUBTITLE in completed_stages:
                    task_logger.info("跳过字幕处理（checkpoint已完成）")
                else:
                    ok = self._translate_subtitle(task_id, task_logger)
                    task = get_task(task_id)
                    if ok:
                        completed_stages = _mark_stage_done(task_id, completed_stages, PIPELINE_STAGE_TRANSLATE_SUBTITLE)
                    if task is not None and task['status'] == TASK_STATES['FAILED']:
                        task_logger.error("字幕处理失败，继续执行后续步骤")
                _raise_if_cancelled(task_id, task_logger)

            # 6. 上传
            if self.config.get('AUTO_MODE_ENABLED', False):
                # 若已有上传响应，避免重复上传
                task = get_task(task_id)
                upload_target = _get_task_upload_target(task)
                if task and _task_has_upload_response(task, upload_target):
                    task_logger.info("检测到已有上传响应，跳过上传（避免重复上传）")
                    completed_stages = _mark_stage_done(task_id, completed_stages, PIPELINE_STAGE_UPLOAD_TO_ACFUN)
                elif PIPELINE_STAGE_UPLOAD_TO_ACFUN in completed_stages:
                    task_logger.info("跳过上传（checkpoint已完成）")
                else:
                    self._upload_to_target(task_id, task_logger)
                    _raise_if_cancelled(task_id, task_logger)
                    task = get_task(task_id)
                    upload_target = _get_task_upload_target(task)
                    if task and task.get('status') == TASK_STATES['AWAITING_REVIEW']:
                        task_logger.info("上传前校验发现缺失译文，任务已转入人工审核")
                        return
                    if task and (_task_has_upload_response(task, upload_target) or task.get('status') == TASK_STATES['COMPLETED']):
                        completed_stages = _mark_stage_done(task_id, completed_stages, PIPELINE_STAGE_UPLOAD_TO_ACFUN)

            # 任务处理完成后，根据是否已上传到目标平台决定状态
            task = get_task(task_id)
            if task is not None:
                upload_target = _get_task_upload_target(task)
                if task['status'] == TASK_STATES['AWAITING_REVIEW']:
                    task_logger.info("任务当前处于人工审核状态，保留待审核状态")
                elif task['status'] != TASK_STATES['COMPLETED'] and task['status'] != TASK_STATES['FAILED']:
                    # 如果没有开启自动上传或者上传失败，则标记为"准备上传"
                    if not self.config.get('AUTO_MODE_ENABLED', False) or not _task_has_upload_response(task, upload_target):
                        update_task(task_id, status=TASK_STATES['READY_FOR_UPLOAD'])
                        task_logger.info("任务处理完成，标记为准备上传")
                    else:
                        # 只有成功上传到目标平台的视频才会被标记为"已完成"
                        update_task(task_id, status=TASK_STATES['COMPLETED'])
                        task_logger.info("任务处理并上传完成")
            else:
                task_logger.warning("任务对象为 None，无法确定最终状态")
                update_task(task_id, status=TASK_STATES['READY_FOR_UPLOAD'])
                task_logger.info("任务处理完成，标记为准备上传")
        except TaskCancelledError:
            task_logger.info("任务已取消，停止后续处理")
            update_task(task_id, status=TASK_STATES['FAILED'], error_message="任务已取消")
        except Exception as e:
            task_logger.error(f"任务处理过程中发生错误: {str(e)}")
            import traceback
            task_logger.error(traceback.format_exc())
            update_task(task_id, status=TASK_STATES['FAILED'], error_message=str(e))
        finally:
            clear_task_cancel(task_id, clear_flag=True)
            # 释放并发配额
            if slot_acquired:
                try:
                    active_task_semaphore.release()
                    task_logger.info("已释放任务并发配额")
                except Exception:
                    pass
            
            # 主动清理内存以降低系统资源占用
            try:
                gc.collect()
                task_logger.debug("已执行垃圾回收以优化内存使用")
            except Exception:
                pass
                
            # 任务完成后，检查是否有其他pending任务需要启动
            task_logger.info("任务处理完成，检查是否有其他pending任务...")
            # 延迟1秒后检查，确保数据库状态更新完成
            import threading
            import time
            
            def delayed_check():
                time.sleep(1)  # 等待1秒确保状态已更新
                self._check_and_start_next_pending_task()
            
            threading.Thread(target=delayed_check, daemon=True).start()
    
    def _check_and_start_next_pending_task(self):
        """检查并启动下一个pending任务"""
        try:
            self._refresh_runtime_limits(force=False)
            # 获取所有pending任务
            pending_tasks = get_tasks_by_status(TASK_STATES['PENDING'])
            active_task_ids = _get_active_task_ids()
            pending_tasks = [task for task in pending_tasks if task.get('id') not in active_task_ids]
            
            if not pending_tasks:
                if active_task_ids:
                    logger.info(f"当前没有可启动的pending任务（{len(active_task_ids)} 个任务线程处于活动状态）")
                else:
                    logger.info("没有pending任务需要启动")
                return
            
            # 检查当前是否有正在运行的任务
            processing_states = [
                'fetching_info',
                'info_fetched', 
                TASK_STATES['TRANSLATING'], 
                TASK_STATES['TAGGING'],
                TASK_STATES['PARTITIONING'],
                TASK_STATES['MODERATING'],
                TASK_STATES['DOWNLOADING'], 
                TASK_STATES['DOWNLOADED'],
                TASK_STATES['ASR_TRANSCRIBING'],
                TASK_STATES['TRANSLATING_SUBTITLE'],
                TASK_STATES['ENCODING_VIDEO'],
                TASK_STATES['UPLOADING']
            ]
            
            running_tasks = []
            for state in processing_states:
                running_tasks.extend(get_tasks_by_status(state))
            running_task_ids = {task.get('id') for task in running_tasks if task.get('id')}
            effective_running_count = len(running_task_ids.union(active_task_ids))
            
            # 如果有任务正在运行且并发限制为1，则不启动新任务
            # 兼容字符串配置，安全转换为整数
            max_concurrent = _as_int(self.config.get('MAX_CONCURRENT_TASKS', 2), 2, minimum=1)
            
            # 内存感知并发控制：如果内存使用过高，降低并发数
            if _should_reduce_concurrency():
                max_concurrent = max(1, max_concurrent // 2)
                logger.info(f"检测到高内存使用，降低并发数至 {max_concurrent}")
                
            if effective_running_count >= max_concurrent:
                logger.info(
                    f"当前有效运行任务数 {effective_running_count}（DB运行中 {len(running_task_ids)}，活动线程 {len(active_task_ids)}），"
                    f"达到并发限制 {max_concurrent}，暂不启动新任务"
                )
                return
            
            # 按创建时间排序，启动最早的pending任务
            from modules.pipeline.gates import downloads_blocked, task_needs_download
            if downloads_blocked():
                runnable = [task for task in pending_tasks if not task_needs_download(task)]
                held = len(pending_tasks) - len(runnable)
                if held and not runnable:
                    logger.info(
                        "下载闸门关闭，%s 个仍需下载的任务继续等待；编码和上传不受影响",
                        held,
                    )
                    return
                pending_tasks = runnable

            pending_tasks.sort(key=lambda x: x['created_at'])
            next_task = pending_tasks[0]
            
            logger.info(f"发现pending任务，准备启动: {next_task['id'][:8]}... ({next_task.get('youtube_url', 'Unknown URL')[-30:]})")
            
            # 调度下一个任务
            job_id = self.schedule_task(next_task['id'])
            if job_id:
                logger.info(f"下一个pending任务已自动启动: {next_task['id'][:8]}...")
            else:
                logger.error(f"启动下一个pending任务失败: {next_task['id'][:8]}...")
                
        except Exception as e:
            logger.error(f"检查和启动下一个pending任务时出错: {str(e)}")

    def _ensure_asr_subtitle_qc(self, task_id, subtitle_file, task_logger, quality_state, *, force_asr=None):
        """ASR 产物烧录前的质检闸门，返回 ``(allowed, qc_cleared, reason)``。

        非 ASR 产物（平台自带 / 人工提供字幕）直接放行且不标记 ``qc_cleared``
        —— 它们不是幻觉与时间轴错位的高风险来源，也不该被 ASR 的历史标记误拦。
        判定默认只看文件名（见 ``_is_asr_subtitle_artifact``）；``force_asr``
        用于「译文的来源是 ASR 字幕」这类**名字上继承不到**的情形：译文写入
        ``translated_*.srt``，不匹配 ``asr_*`` 前缀，若不显式继承来源就会被判为
        非 ASR 产物而**永远放行**（注释声称「至少让闸门有机会拒绝」，实际恒真）。
        ``quality_state`` 仅用于决定质检的**严格度**，不参与「是否质检」的判定。

        ``reason`` 只在与 ``allowed=False`` 搭配时有意义，供调用方在
        ``subtitle_warning_message`` 中保留**首个**拦截归因（此前外层分支会
        用 ``asr_failed_block_embed`` 覆盖掉这里更精确的原因）。
        """
        is_asr_artifact = _is_asr_subtitle_artifact(task_id, subtitle_file)
        if force_asr is not None:
            is_asr_artifact = bool(force_asr)
        if not is_asr_artifact:
            return True, False, ''
        qc_result = self._run_subtitle_qc(
            task_id,
            subtitle_file,
            task_logger,
            strict=(str(quality_state or '').strip().lower() == 'degraded'),
        )
        if qc_result is True:
            return True, True, ''
        if qc_result is SUBTITLE_QC_DISABLED:
            # 用户主动关闭质检：放行，但不标记 qc_cleared（不该借机改写历史
            # 质检结论，也不该让门控以为「本次已通过质检」）。
            task_logger.info("字幕质检已被配置关闭，ASR 字幕按用户配置放行烧录")
            return True, False, ''
        if _qc_result_allows_embed(qc_result, self.config):
            # 逃生口对「质检没跑成」同样生效。本方法被翻译分支与上传前阶段复用，
            # 而 ``_translate_subtitle`` 内那份 ``escape_hatch_active`` 是函数级
            # 局部变量、覆盖不到这里；若不在本方法内读同一个配置键，同一份
            # asr_*.srt 会因为走哪条分支而得到相反结论（实测两条路径相反）。
            # 明确的质检失败结论（False）仍不受逃生口影响。
            task_logger.info(
                "ASR_FAILURE_BLOCKS_EMBED=False：质检不可用不阻断烧录，按配置放行"
            )
            return True, False, ''
        reason_text = 'qc_unavailable' if qc_result is None else 'subtitle_qc_rejected'
        task_logger.warning(
            "ASR 字幕质检未通过（%s）：跳过烧录，保留字幕文件并继续上传原视频",
            reason_text,
        )
        # 被拒对象是 ASR 来源的字幕（含 force_asr 继承来的译文），必须移出复用范围：
        # 留在 task_dir 里会让下一轮扫描直接复用它，用户重跑永远拿不到新字幕。
        self._quarantine_rejected_subtitle(
            subtitle_file, task_logger, force=bool(force_asr)
        )
        update_task(task_id, subtitle_warning_message=reason_text, upload_progress=None)
        return False, False, reason_text

    def _quarantine_rejected_subtitle(self, subtitle_path, task_logger=None, *, force=False) -> str:
        """把被质检/质量门控拒绝的 ASR 字幕移出扫描范围。

        为什么必须移动：被拒绝的字幕文件留在 task_dir 里会让下一轮
        ``_translate_subtitle`` 的 ``os.listdir`` 扫描命中它，从而**跳过 ASR
        重新生成**、直接复用这条已被判定不合格的字幕 —— 用户重跑任务永远
        拿不到新字幕。改名为 ``.rejected.txt`` 后内容仍保留供人工检查，但不再
        匹配 ``.srt/.vtt`` 过滤条件，因此既不会被复用也不会被上传。

        **默认只处理 ASR 产物**：判据在方法内部（只看文件名），而不是依赖每个调用点
        自己先判定。此前防护只加在 ``_quarantine_if_asr_artifact`` 这层包装上，
        而 ``_ensure_asr_subtitle_qc`` 内部那条路径直接调用本方法，导致平台自带
        字幕 ``video.zh.srt`` 在任务级 ``degraded`` 时被改名成
        ``video.zh.rejected.txt`` —— 破坏用户既有产物，且改名后
        ``subtitle_path_original`` 仍指向已不存在的路径。

        ``force=True`` 仅供「译文由 ASR 源字幕翻译而来」这一处使用：文件名
        （``translated_*.srt``）继承不到 ``asr_`` 前缀，来源判定由调用方给出。
        """
        if not force and not _is_asr_subtitle_artifact('', subtitle_path):
            return ''
        try:
            source = str(subtitle_path or '').strip()
            if not source or not os.path.exists(source):
                return ''
            target = f"{os.path.splitext(source)[0]}.rejected.txt"
            index = 1
            while os.path.exists(target):
                target = f"{os.path.splitext(source)[0]}.rejected.{index}.txt"
                index += 1
            os.replace(source, target)
            if task_logger is not None:
                task_logger.info(
                    "已被拒绝的 ASR 字幕已移出复用范围（保留供人工检查）：%s",
                    os.path.basename(target),
                )
            return target
        except Exception as exc:
            if task_logger is not None:
                try:
                    task_logger.warning(f"移动被拒绝的 ASR 字幕失败（不影响上传）: {exc}")
                except Exception:
                    pass
            return ''

    def _mark_subtitle_qc_unavailable(self, task_id, task_logger=None, detail='', blocks=None):
        """把「质检没跑成」显式落库，返回落库成功与否。

        为什么必须落库：``_run_subtitle_qc`` 返回 ``None``（字幕文件缺失 /
        质检执行异常）时此前不写任何字段，``subtitle_qc_failed`` 仍停留在 0。
        后果是一条跨阶段失效链：本阶段拒绝烧录并隔离了源字幕、只落了
        ``subtitle_warning_message='qc_unavailable'``；下一阶段（上传前
        ``_prepare_subtitle_for_upload``）重新读取门控状态时拿到的是
        ``('ok', 0)`` → 放行 → 命中「既有带字幕视频可复用」直接 return。
        而源字幕已被隔离，``_get_embedded_video_candidate`` 的新鲜度检查
        看不到它，就把**上一轮烧录的旧成片**判为新鲜并复用 —— 用户界面显示
        「质检未通过、已跳过烧录」，上传的成片却带着那条被拒字幕。

        落一个显式不可用标记后，``_is_subtitle_stage_rejected`` 与
        ``_subtitle_block_reasons`` 都能看到它，门控在后续阶段保持一致结论
        （checkpoint 侧同样按门控同口径裁决，见 ``_is_subtitle_stage_rejected``）。

        ``blocks`` 表示本次「质检不可用」是否按门控拦截烧录（默认取逃生口
        ``ASR_FAILURE_BLOCKS_EMBED``）。逃生口打开时它**不**拦截，此时不得落
        ``subtitle_qc_failed=1`` —— 否则同一状态在不同阶段得到相反结论：
        ``_ensure_asr_subtitle_qc`` 按逃生口放行，而上传前阶段读到库里的
        ``qc_failed=1`` 又把烧录拦下、并把字幕阶段永久判为「未完成」，
        用户显式配置的宽松策略失效且每轮重跑都重做 ASR。

        落库的 ``subtitle_qc_reason='qc_unavailable'`` 是门控识别「没跑成」的依据
        （见 ``_subtitle_block_reasons``），因此逃生口打开时**已存在明确失败结论**
        的记录必须保留原来的原因：把无法归因的历史失败改写成 ``qc_unavailable``
        会让门控在逃生口下把它当成「没跑成」放过，等于洗白历史失败。
        """
        if blocks is None:
            blocks = _as_bool(self.config.get('ASR_FAILURE_BLOCKS_EMBED', True))
        try:
            fields = {
                'subtitle_qc_reason': 'qc_unavailable',
                'subtitle_qc_checked_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                'upload_progress': None,
            }
            if blocks:
                fields['subtitle_qc_failed'] = 1
            else:
                # 只更新「没跑成」这一事实，保留既有结论：逃生口只放宽
                # 「质检没跑成」，不得顺手把历史上的明确质检失败结论洗白。
                current = get_task(task_id) or {}
                prior_failed = current.get('subtitle_qc_failed') == 1
                fields['subtitle_qc_failed'] = 1 if prior_failed else 0
                if prior_failed:
                    # 原因保持原样（可能为空）：门控据此继续拦截，不会把它
                    # 误当成「没跑成」。
                    fields.pop('subtitle_qc_reason', None)
            update_task(task_id, **fields)
            return True
        except Exception as exc:
            if task_logger is not None:
                try:
                    task_logger.warning(f"写入质检不可用标记失败（不影响本轮拦截决策）: {exc}")
                except Exception:
                    pass
            return False

    def _persist_qc_unavailable(self, task_id, task_logger=None, detail=''):
        """落库「质检没跑成」标记，并**消费**落库结果。

        ``_mark_subtitle_qc_unavailable`` 的返回值此前被两个调用点直接丢弃：
        写入失败时它只记一条 warning，于是「结论不落库」的失效链（后续阶段
        重新读库拿到 ``qc_failed=0`` 而放行复用旧成片）会在 DB 写失败这一情形下
        原样重现。这里显式消费返回值并留下可排查的告警，不再静默降级。
        """
        persisted = self._mark_subtitle_qc_unavailable(task_id, task_logger, detail=detail)
        if not persisted and task_logger is not None:
            try:
                task_logger.warning(
                    "质检不可用标记未能落库（detail=%s）：后续阶段可能看不到本次"
                    "拦截结论，若复用既有成片请人工确认其字幕是否合格",
                    detail or 'n/a',
                )
            except Exception:
                pass
        return persisted

    def _run_subtitle_qc(self, task_id: str, srt_path: str, task_logger, strict: bool = False):
        """对 ASR 生成字幕执行预检，每次都重新计算结果。

        返回三态：``True`` 通过、``False`` 未通过、``SUBTITLE_QC_DISABLED``
        用户主动关闭质检、``None`` 质检启用但不可用（文件缺失 / 执行异常）。
        调用方必须区分「通过」与「没跑成」—— 此前两种情况都返回 ``True``，
        导致质检形同虚设；而「主动关闭」与「没跑成」混用又会造成关闭质检
        反而更严格的反向回归。
        两条「不可用」分支都会落库显式标记（见 ``_mark_subtitle_qc_unavailable``），
        避免拦截结论不持久化导致后续阶段复用旧成片。
        ``strict=True`` 时对 ASR 来源退化的字幕启用更严格判定。
        """
        enabled_raw = self.config.get('SUBTITLE_QC_ENABLED', True)
        enabled = _as_bool(enabled_raw)
        if not enabled:
            task_logger.info("字幕质检未启用，按配置跳过质检并放行烧录")
            return SUBTITLE_QC_DISABLED

        if not srt_path or not os.path.exists(srt_path):
            try:
                task_logger.warning(f"字幕质检不可用：字幕文件缺失 {srt_path}")
            except Exception:
                pass
            self._persist_qc_unavailable(task_id, task_logger, detail='missing_file')
            return None

        try:
            from modules.subtitle_qc import run_subtitle_qc

            result = run_subtitle_qc(
                srt_path,
                self.config,
                total_duration_s=self._resolve_qc_total_duration(task_id, task_logger),
                strict=bool(strict),
            )
            checked_at = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            update_task(
                task_id,
                subtitle_qc_failed=0 if result.passed else 1,
                subtitle_qc_reason=result.reason,
                subtitle_qc_score=float(result.score),
                subtitle_qc_checked_at=checked_at,
            )

            if result.passed:
                raw_ai = result.raw_ai if isinstance(result.raw_ai, dict) else {}
                task_logger.info(
                    "字幕质检通过: decision=%s, reason=%s, final_score=%.3f, rule_score=%.3f, ai_score=%s, ai_mode=%s, ai_override=%s, sample_items=%s, sample_chars=%s, timeline=%s",
                    result.decision or 'unknown',
                    result.reason,
                    float(result.score),
                    float(result.rule_score),
                    f"{float(result.ai_score):.3f}" if result.ai_score is not None else 'n/a',
                    str(raw_ai.get('ai_mode') or 'n/a'),
                    bool(raw_ai.get('ai_override')),
                    result.sample_items,
                    result.sample_chars,
                    raw_ai.get('timeline_metrics') or 'n/a',
                )
                return True

            raw_ai = result.raw_ai if isinstance(result.raw_ai, dict) else {}
            task_logger.warning(
                "字幕质检失败: decision=%s, reason=%s, final_score=%.3f, rule_score=%.3f, ai_score=%s, ai_mode=%s, ai_override=%s, sample_items=%s, sample_chars=%s, timeline=%s",
                result.decision or 'unknown',
                result.reason,
                float(result.score),
                float(result.rule_score),
                f"{float(result.ai_score):.3f}" if result.ai_score is not None else 'n/a',
                str(raw_ai.get('ai_mode') or 'n/a'),
                bool(raw_ai.get('ai_override')),
                result.sample_items,
                result.sample_chars,
                raw_ai.get('timeline_metrics') or 'n/a',
            )
            return False
        except Exception as e:
            # 质检执行异常不再默认放行：返回 None 表示「不可用」，由调用方拒绝烧录。
            try:
                task_logger.warning(f"字幕质检执行异常，判定为质检不可用，将不烧录字幕: {e}")
            except Exception:
                pass
            self._persist_qc_unavailable(task_id, task_logger, detail=f'exception: {e}')
            return None

    def _resolve_qc_total_duration(self, task_id: str, task_logger=None):
        """取视频总时长用于质检的时间轴维度；取不到时返回 None（跳过该维度）。"""
        try:
            task = get_task(task_id)
            video_path = str(task.get('video_path_local') or '') if task else ''
            if not video_path or not os.path.exists(video_path):
                return None
            # _get_video_duration 要求有效 logger（内部直接调用 .info/.warning）。
            duration = self._get_video_duration(video_path, task_logger or logger)
            duration_value = float(duration or 0.0)
            return duration_value if duration_value > 0 else None
        except Exception:
            return None

    def _mark_subtitle_issue(self, task_id: str, reason: str, score=None):
        """将字幕异常写入任务表，复用前端现有“字幕异常”展示。"""
        update_task(
            task_id,
            subtitle_qc_failed=1,
            subtitle_qc_reason=reason,
            subtitle_qc_score=score,
            subtitle_qc_checked_at=datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        )

    def _generate_tags(self, task_id, task_logger):
        """生成视频标签"""
        from modules.ai_enhancer import generate_acfun_tags
        
        task = get_task(task_id)
        if not task:
            task_logger.error("任务不存在")
            return
        
        task_logger.info("开始生成视频标签")
        update_task(task_id, status=TASK_STATES['TAGGING'])
        
        # 优先使用翻译后的元数据，避免将原始导流文本继续送给标签模型
        from modules.utils import safe_str
        title = safe_str(task.get('video_title_translated') or task.get('video_title_original'))
        description = safe_str(task.get('description_translated') or task.get('description_original'))
        
        # 构建OpenAI配置
        openai_config = {
            'OPENAI_API_KEY': self.config.get('OPENAI_API_KEY', ''),
            'OPENAI_BASE_URL': self.config.get('OPENAI_BASE_URL', ''),
            'OPENAI_MODEL_NAME': self.config.get('OPENAI_MODEL_NAME', 'gpt-3.5-turbo'),
            'OPENAI_THINKING_ENABLED': self.config.get('OPENAI_THINKING_ENABLED', False),
            'OPENAI_REASONING_EFFORT': self.config.get('OPENAI_REASONING_EFFORT', 'auto'),
            'OPENAI_TIMEOUT_SECONDS': self.config.get('OPENAI_TIMEOUT_SECONDS', 600),
            'AI_FAILOVER_TIMEOUT_SECONDS': self.config.get('AI_FAILOVER_TIMEOUT_SECONDS', 8),
            # 兜底端点：显式透传，确保 get_ai_client 能拿到（统一客户端也会从全局配置补齐）
            'FALLBACK_OPENAI_API_KEY': self.config.get('FALLBACK_OPENAI_API_KEY', ''),
            'FALLBACK_OPENAI_BASE_URL': self.config.get('FALLBACK_OPENAI_BASE_URL', ''),
            'FALLBACK_OPENAI_MODEL_NAME': self.config.get('FALLBACK_OPENAI_MODEL_NAME', ''),
            'FIXED_PARTITION_ID': self.config.get('FIXED_PARTITION_ID', ''),
        }
        
        # 预设标签（Issue #139）：启用且非空时，预设排在任务已有标签之前，
        # AI 只补齐剩余名额（落盘上限取 bilibili 的 12，AcFun 上传时再截到 6）。
        preset_tags = parse_preset_tags(self.config.get('PRESET_TAGS', ''))
        preset_effective = is_preset_tags_effective(self.config)

        if not preset_effective:
            # 预设未生效：完全保留既有行为（AI 6 标签 / 失败或禁用写空列表）
            if self.config.get('GENERATE_TAGS', True) and (title or description):
                tags = generate_acfun_tags(
                    title, 
                    description, 
                    openai_config=openai_config,
                    task_id=task_id
                )
                # 限制标签数量不超过6个
                if tags:
                    tags = tags[:6]
                    update_task(task_id, tags_generated=json.dumps(tags, ensure_ascii=False))
                else:
                    task_logger.warning("标签生成失败")
                    update_task(task_id, tags_generated=json.dumps([], ensure_ascii=False))
            else:
                task_logger.warning("标签生成已禁用或缺少必要信息")
                update_task(task_id, tags_generated=json.dumps([], ensure_ascii=False))

            task_logger.info(f"标签生成完成: {task.get('tags_generated', '[]')}")
            return True

        existing_tags = _normalize_tags_list(task.get('tags_generated'))
        ai_tags = []
        if self.config.get('GENERATE_TAGS', True):
            if title or description:
                raw_tags = generate_acfun_tags(
                    title,
                    description,
                    openai_config=openai_config,
                    task_id=task_id,
                    avoid_tags=preset_tags,
                ) or []
                ai_tags = [str(tag).strip() for tag in raw_tags if str(tag or '').strip()]
            else:
                task_logger.warning("缺少标题和简介，跳过 AI 标签补齐（仅使用预设标签）")
        else:
            task_logger.info("自动生成标签已禁用，仅使用预设标签")

        merged_tags = merge_tags(
            preset_tags,
            existing_tags + ai_tags,
            limit=BILIBILI_TAG_LIMIT,
        )
        update_task(task_id, tags_generated=json.dumps(merged_tags, ensure_ascii=False))
        task_logger.info(
            f"预设标签生效：预设 {len(preset_tags)} 个 / AI 补齐 {len(ai_tags)} 个"
            f" -> {json.dumps(merged_tags, ensure_ascii=False)}"
        )
        return True
    
    def _recommend_partition(self, task_id, task_logger):
        """推荐视频分区（平台感知：AcFun/Bilibili）"""
        from modules.ai_enhancer import (
            recommend_acfun_partition,
            recommend_bilibili_partition,
            recommend_partitions_aio,
        )

        task = get_task(task_id)
        if not task:
            task_logger.error("任务不存在")
            return

        task_logger.info("开始推荐视频分区")
        update_task(task_id, status=TASK_STATES['PARTITIONING'])

        title = task.get('video_title_translated', '') or task.get('video_title_original', '')
        description = task.get('description_translated', '') or task.get('description_original', '')
        title_original = task.get('video_title_original', '') or ''
        description_original = task.get('description_original', '') or ''
        title_translated = task.get('video_title_translated', '') or ''
        description_translated = task.get('description_translated', '') or ''
        tags_generated = _normalize_tags_list(task.get('tags_generated'))
        if task.get('tags_generated') and not tags_generated:
            task_logger.warning("解析 AI 标签失败或标签为空，分区推荐将忽略标签上下文")
        upload_target = _get_task_upload_target(task)

        openai_config = {
            'OPENAI_API_KEY': self.config.get('OPENAI_API_KEY', ''),
            'OPENAI_BASE_URL': self.config.get('OPENAI_BASE_URL', ''),
            'OPENAI_MODEL_NAME': self.config.get('OPENAI_MODEL_NAME', 'gpt-3.5-turbo'),
            'OPENAI_THINKING_ENABLED': self.config.get('OPENAI_THINKING_ENABLED', False),
            'OPENAI_REASONING_EFFORT': self.config.get('OPENAI_REASONING_EFFORT', 'auto'),
            'AI_FAILOVER_TIMEOUT_SECONDS': self.config.get('AI_FAILOVER_TIMEOUT_SECONDS', 8),
            'OPENAI_TIMEOUT_SECONDS': self.config.get('OPENAI_TIMEOUT_SECONDS', 600),
            # 兜底端点：显式透传，确保 get_ai_client 能拿到（统一客户端也会从全局配置补齐）
            'FALLBACK_OPENAI_API_KEY': self.config.get('FALLBACK_OPENAI_API_KEY', ''),
            'FALLBACK_OPENAI_BASE_URL': self.config.get('FALLBACK_OPENAI_BASE_URL', ''),
            'FALLBACK_OPENAI_MODEL_NAME': self.config.get('FALLBACK_OPENAI_MODEL_NAME', ''),
            'FIXED_PARTITION_ID': self.config.get('FIXED_PARTITION_ID', ''),
            'FIXED_PARTITION_ID_BILIBILI': self.config.get('FIXED_PARTITION_ID_BILIBILI', ''),
            'RECOMMEND_PARTITION_WITH_COVER': self.config.get('RECOMMEND_PARTITION_WITH_COVER', False),
        }
        cover_path = task.get('cover_path_local', '')

        task_logger.info(f"RECOMMEND_PARTITION设置: {self.config.get('RECOMMEND_PARTITION', False)}")
        from modules.utils import safe_str
        task_logger.info(f"标题长度: {len(safe_str(title))}, 描述长度: {len(safe_str(description))}")
        task_logger.info(f"任务目标平台: {upload_target}")

        if not self.config.get('RECOMMEND_PARTITION', False):
            task_logger.info("分区推荐功能已禁用，跳过推荐")
            return True

        if not (title or description):
            task_logger.warning("缺少标题和描述，无法进行分区推荐")
            return True

        targets_to_recommend = []
        if upload_target == UPLOAD_TARGET_BOTH:
            targets_to_recommend = [UPLOAD_TARGET_ACFUN, UPLOAD_TARGET_BILIBILI]
        elif upload_target == UPLOAD_TARGET_BILIBILI:
            targets_to_recommend = [UPLOAD_TARGET_BILIBILI]
        else:
            targets_to_recommend = [UPLOAD_TARGET_ACFUN]

        platform_results = {}
        zone_data = []
        id_mapping_data = []

        if UPLOAD_TARGET_BILIBILI in targets_to_recommend:
            try:
                from .bilibili_zones import get_zone_list_sub
                zone_data = get_zone_list_sub()
                task_logger.info(f"成功读取bilibili分区数据，长度: {len(zone_data)}")
            except Exception as e:
                task_logger.error(f"读取bilibili分区数据失败: {e}")

        if UPLOAD_TARGET_ACFUN in targets_to_recommend:
            from .utils import get_app_subdir
            id_mapping_path = os.path.join(get_app_subdir('acfunid'), 'id_mapping.json')
            task_logger.info(f"尝试读取 AcFun 分区映射文件: {id_mapping_path}")
            try:
                if not os.path.exists(id_mapping_path):
                    task_logger.error(f"分区映射文件不存在: {id_mapping_path}")
                else:
                    with open(id_mapping_path, 'r', encoding='utf-8') as f:
                        id_mapping_data = json.load(f)
                    task_logger.info(f"成功读取 AcFun 分区映射文件，包含 {len(id_mapping_data)} 个分类")
            except Exception as e:
                task_logger.error(f"读取 AcFun 分区ID映射失败: {str(e)}")
                id_mapping_data = []

        if targets_to_recommend == [UPLOAD_TARGET_ACFUN, UPLOAD_TARGET_BILIBILI]:
            if not id_mapping_data:
                task_logger.warning("AcFun 分区映射数据为空，跳过AcFun推荐")
            if not zone_data:
                task_logger.warning("bilibili分区数据为空，跳过bilibili推荐")
            platform_results = recommend_partitions_aio(
                title,
                description,
                acfun_id_mapping_data=id_mapping_data,
                bilibili_zone_data=zone_data,
                title_original=title_original,
                description_original=description_original,
                title_translated=title_translated,
                description_translated=description_translated,
                tags=tags_generated,
                openai_config=openai_config,
                task_id=task_id,
                cover_path=cover_path,
                include_cover_for_ai=self.config.get('RECOMMEND_PARTITION_WITH_COVER', False),
            )
        else:
            for platform in targets_to_recommend:
                partition_selection = None
                if platform == UPLOAD_TARGET_BILIBILI:
                    if not zone_data:
                        task_logger.warning("bilibili分区数据为空，跳过bilibili推荐")
                        platform_results[platform] = {}
                        continue
                    partition_selection = recommend_bilibili_partition(
                        title,
                        description,
                        zone_data,
                        title_original=title_original,
                        description_original=description_original,
                        title_translated=title_translated,
                        description_translated=description_translated,
                        tags=tags_generated,
                        openai_config=openai_config,
                        task_id=task_id,
                        cover_path=cover_path,
                        include_cover_for_ai=self.config.get('RECOMMEND_PARTITION_WITH_COVER', False),
                    )
                else:
                    if not id_mapping_data:
                        task_logger.warning("AcFun 分区映射数据为空，跳过AcFun推荐")
                        platform_results[platform] = {}
                        continue
                    partition_selection = recommend_acfun_partition(
                        title,
                        description,
                        id_mapping_data,
                        title_original=title_original,
                        description_original=description_original,
                        title_translated=title_translated,
                        description_translated=description_translated,
                        tags=tags_generated,
                        openai_config=openai_config,
                        task_id=task_id,
                        cover_path=cover_path,
                        include_cover_for_ai=self.config.get('RECOMMEND_PARTITION_WITH_COVER', False),
                    )

                platform_results[platform] = partition_selection or {}

        for platform in targets_to_recommend:
            partition_selection = platform_results.get(platform) or {}
            recommended_partition_id = str(partition_selection.get('id') or '').strip()
            content_profile = partition_selection.get('content_profile') or {}
            if content_profile:
                task_logger.info(
                    "%s content_profile: domain=%s, subdomain=%s, format=%s, game_mode=%s, interview=%s, confidence=%s, reason=%s, entities=%s",
                    platform,
                    content_profile.get('domain'),
                    content_profile.get('subdomain'),
                    content_profile.get('content_format'),
                    content_profile.get('game_mode'),
                    content_profile.get('is_interview'),
                    content_profile.get('confidence'),
                    content_profile.get('reason_summary'),
                    content_profile.get('entities'),
                )
            if recommended_partition_id:
                selected_field = _get_partition_field_name(platform, 'selected')
                recommended_field = _get_partition_field_name(platform, 'recommended')

                latest_task = get_task(task_id) or {}
                updates: Dict[str, Any] = {recommended_field: recommended_partition_id}
                if not str(latest_task.get(selected_field) or '').strip():
                    updates[selected_field] = recommended_partition_id

                update_task(task_id, **updates)
                task_logger.info(
                    "%s 获取到推荐分区并已更新任务: source=%s, id=%s, confidence=%s, alternatives=%s, low_confidence=%s, reason=%s",
                    platform,
                    partition_selection.get('source'),
                    recommended_partition_id,
                    partition_selection.get('confidence'),
                    partition_selection.get('alternatives') or [],
                    partition_selection.get('low_confidence'),
                    partition_selection.get('reason_summary') or '',
                )
            else:
                if not openai_config.get('OPENAI_API_KEY'):
                    task_logger.warning(f"{platform} 分区推荐未命中：未配置OpenAI且规则匹配失败")
                else:
                    task_logger.warning(f"{platform} 分区推荐未命中：OpenAI与规则匹配均失败")

        task_logger.info(f"分区推荐流程完成，结果: {platform_results}")
        return True
    
    def _moderate_content(self, task_id, task_logger):
        """内容审核"""
        if not self.config.get('CONTENT_MODERATION_ENABLED', False):
            task_logger.info("内容审核已禁用")
            return

        from modules.content_moderator import AlibabaCloudModerator
        
        task = get_task(task_id)
        if not task:
            task_logger.error("任务不存在")
            return
        
        task_logger.info("开始内容审核")
        update_task(task_id, status=TASK_STATES['MODERATING'])
        
        # 优先使用翻译后的标题和描述，如果没有则使用原始内容
        title = task.get('video_title_translated', '') or task.get('video_title_original', '')
        description = task.get('description_translated', '') or task.get('description_original', '')
        
        # 获取AI生成的标签
        tags_string = ""
        tags_list = [] # 初始化 tags_list
        if task.get('tags_generated'):
            try:
                tags_list = json.loads(task.get('tags_generated', '[]'))
                if tags_list:
                    # 用于附加到描述的字符串可以保持原样，或者根据需要调整
                    # tags_string = "，标签：" + "，".join(tags_list) 
                    pass # 暂时不修改附加到描述的逻辑，主要确保tags_list被正确赋值
            except json.JSONDecodeError:
                task_logger.warning("解析AI生成标签失败，内容审核时将不包含标签。")

        # 预处理内容，过滤掉URL等推广内容
        url_pattern = r'https?://(?:[-\w.]|(?:%[\da-fA-F]{2}))+'
        email_pattern = r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b'
        
        filtered_title = re.sub(url_pattern, '', title)
        filtered_title = re.sub(email_pattern, '', filtered_title)
        
        # 将标签附加到描述文本后进行审核 (这部分可以保留，也可以考虑是否还需要)
        # description_with_tags = description + tags_string 
        # 为了更清晰，我们先只审核原始描述，标签单独审核
        from modules.utils import safe_str
        filtered_description = re.sub(url_pattern, '', safe_str(description))
        filtered_description = re.sub(email_pattern, '', filtered_description)
        filtered_description = re.sub(r'\\n{3,}', '\\n\\n', filtered_description)
        
        task_logger.info("已过滤标题和描述中的URL和邮箱地址")
        task_logger.info(f"用于审核的描述文本: {filtered_description[:200]}...") # 日志记录部分内容

        # 阿里云配置
        aliyun_config = {
            'ALIYUN_ACCESS_KEY_ID': self.config.get('ALIYUN_ACCESS_KEY_ID', ''),
            'ALIYUN_ACCESS_KEY_SECRET': self.config.get('ALIYUN_ACCESS_KEY_SECRET', ''),
            'ALIYUN_CONTENT_MODERATION_REGION': self.config.get('ALIYUN_CONTENT_MODERATION_REGION', 'cn-shanghai')
        }
        
        text_moderation_service = self.config.get('ALIYUN_TEXT_MODERATION_SERVICE') or 'comment_detection_pro'
        task_logger.info(f"使用阿里云文本审核服务类型: {text_moderation_service}")

        moderator = AlibabaCloudModerator(aliyun_config, task_id)
        
        title_result = moderator.moderate_text(filtered_title, service_type=text_moderation_service)
        task_logger.info(f"标题审核结果: {title_result}")
        
        description_result = moderator.moderate_text(filtered_description, service_type=text_moderation_service)
        task_logger.info(f"描述审核结果: {description_result}")

        tags_for_moderation_string = ""
        if tags_list:
            tags_for_moderation_string = "，".join(tags_list) # 将标签列表转换为逗号分隔的字符串进行审核
            task_logger.info(f"用于审核的标签文本: {tags_for_moderation_string[:200]}...")
        
        tags_moderation_result = {"pass": True, "details": [{"label": "skipped", "suggestion": "pass", "reason": "没有生成标签或标签为空"}]}
        if tags_for_moderation_string:
            tags_moderation_result = moderator.moderate_text(tags_for_moderation_string, service_type=text_moderation_service)
            task_logger.info(f"标签审核结果: {tags_moderation_result}")
        else:
            task_logger.info("没有标签需要审核。")
        
        cover_result = {"pass": True, "status": "disabled", "details": [{"label": "skipped", "suggestion": "pass", "reason": "封面审核已禁用"}]}
        if self.config.get('CONTENT_MODERATION_COVER_ENABLED', False):
            image_service = self.config.get('ALIYUN_IMAGE_MODERATION_SERVICE') or 'baselineCheck'
            task_logger.info(f"开始视频封面审核，服务类型: {image_service}")
            try:
                cover_path = self._recover_cover_path(task_id, task.get('cover_path_local', ''), task_logger)
                cover_result = moderator.moderate_image(cover_path, service_type=image_service)
            except Exception as exc:
                task_logger.error(f"封面审核失败: {exc}")
                cover_result = {
                    "pass": False,
                    "status": "error",
                    "details": [{"label": "error", "suggestion": "review", "reason": "封面审核失败，请检查封面文件与审核服务"}],
                }
            task_logger.info(f"封面审核结果: {cover_result}")
        
        moderation_result = {
            "title": title_result,
            "description": description_result,
            "tags": tags_moderation_result, # 添加标签审核结果
            "cover": cover_result,
            "overall_pass": (title_result.get("pass", True)
                             and description_result.get("pass", True)
                             and tags_moderation_result.get("pass", True)
                             and cover_result.get("pass", False))
        }
        
        task_logger.info(f"综合审核结果: overall_pass={moderation_result['overall_pass']}")
        if not moderation_result["overall_pass"]:
            if not title_result.get("pass", True):
                task_logger.warning("标题未通过审核")
                for detail in title_result.get("details", []):
                    task_logger.warning(f"标题问题: {detail.get('label')} - {detail.get('reason')}")
                    
            if not description_result.get("pass", True):
                task_logger.warning("描述未通过审核")
                for detail in description_result.get("details", []):
                    task_logger.warning(f"描述问题: {detail.get('label')} - {detail.get('reason')}")
            
            if not tags_moderation_result.get("pass", True):
                task_logger.warning("标签未通过审核")
                for detail in tags_moderation_result.get("details", []):
                    task_logger.warning(f"标签问题: {detail.get('label')} - {detail.get('reason')}")

            if not cover_result.get("pass", False):
                task_logger.warning("封面未通过审核")
                for detail in cover_result.get("details", []):
                    task_logger.warning(f"封面问题: {detail.get('label')} - {detail.get('reason')}")
        
        update_task(
            task_id,
            moderation_result=json.dumps(moderation_result, ensure_ascii=False)
        )
        
        if moderation_result["overall_pass"]:
            task_logger.info("内容审核通过")
        else:
            task_logger.info("内容审核不通过，需要人工审核")
            update_task(
                task_id,
                status=TASK_STATES['AWAITING_REVIEW'],
                error_category=CONTENT_MODERATION_ERROR_CATEGORY,
            )

    def _get_embedded_video_candidate(self, video_path: str, subtitle_paths=None) -> str:
        """返回可复用的带字幕视频路径。

        复用的前提是产物不能比字幕文件更旧 —— 否则字幕已重新生成，
        旧的 ``_with_subtitle.mp4`` 会把错误字幕继续带到成片里。
        """
        def _is_fresh(candidate: str) -> bool:
            if not candidate or not os.path.exists(candidate):
                return False
            try:
                candidate_mtime = os.path.getmtime(candidate)
            except OSError:
                return False
            for subtitle_path in (subtitle_paths or []):
                if not subtitle_path or not os.path.exists(subtitle_path):
                    continue
                try:
                    if os.path.getmtime(subtitle_path) > candidate_mtime + 1e-6:
                        return False
                except OSError:
                    continue
            return True

        if not video_path:
            return ''
        if os.path.exists(video_path):
            base_name_no_ext = os.path.splitext(os.path.basename(video_path))[0]
            if base_name_no_ext.endswith('_with_subtitle') and _is_fresh(video_path):
                return video_path
        video_dir = os.path.dirname(video_path)
        video_name = os.path.splitext(os.path.basename(video_path))[0]
        candidate = os.path.join(video_dir, f"{video_name}_with_subtitle.mp4")
        return candidate if _is_fresh(candidate) else ''

    def _resolve_existing_subtitle_assets(self, task_id: str, task: dict, task_dir: str):
        def _existing_path(value):
            path_value = str(value or '').strip()
            return path_value if path_value and os.path.exists(path_value) else ''

        subtitle_path_original = _existing_path(task.get('subtitle_path_original'))
        subtitle_path_translated = _existing_path(task.get('subtitle_path_translated'))
        subtitle_language = str(task.get('subtitle_language_detected') or '').strip().lower()

        if not subtitle_path_translated:
            translated_candidate = os.path.join(task_dir, f"translated_{task_id}.srt")
            if os.path.exists(translated_candidate):
                subtitle_path_translated = translated_candidate

        if not subtitle_path_original:
            for ext in ('.srt', '.vtt'):
                asr_candidate = os.path.join(task_dir, f"asr_{task_id}{ext}")
                if os.path.exists(asr_candidate):
                    subtitle_path_original = asr_candidate
                    break

        subtitle_candidates = []
        try:
            for name in os.listdir(task_dir):
                if not isinstance(name, str):
                    continue
                lower = name.lower()
                if lower.endswith('.srt') or lower.endswith('.vtt'):
                    subtitle_candidates.append(os.path.join(task_dir, name))
        except Exception:
            subtitle_candidates = []

        subtitle_candidates = sorted(set(subtitle_candidates))
        for candidate in subtitle_candidates:
            lower_name = os.path.basename(candidate).lower()
            if lower_name.startswith('translated_'):
                if not subtitle_path_translated:
                    subtitle_path_translated = candidate
                continue
            if not subtitle_path_original:
                subtitle_path_original = candidate

        return subtitle_path_original, subtitle_path_translated, subtitle_language

# 任务控制函数
def start_task(task_id, config=None):
    """
    启动任务处理
    
    Args:
        task_id: 任务ID
        config: 配置信息
    
    Returns:
        success: 启动是否成功
    """
    # 获取任务信息
    task = get_task(task_id)
    if not task:
        logger.error(f"任务 {task_id} 不存在")
        return False
    
    # 任务已经在处理中或已完成
    if task['status'] not in [TASK_STATES['PENDING'], TASK_STATES['AWAITING_REVIEW'], TASK_STATES['FAILED']]:
        logger.warning(f"任务 {task_id} 状态为 {task['status']}，不能启动")
        return False
    
    # 放弃的失败任务可能没有 worker 负责在 finally 清理取消事件；重试前仅在任务不活动时清除旧标记。
    if not _is_task_active(task_id):
        clear_task_cancel(task_id, clear_flag=True)

    # 如果没有提供配置，尝试从Flask app获取
    if not config:
        try:
            from flask import current_app
            if 'Y2A_SETTINGS' in current_app.config:
                config = current_app.config['Y2A_SETTINGS']
                logger.info("从Flask应用获取配置")
        except (ImportError, RuntimeError):
            logger.warning("无法从Flask应用获取配置，使用空配置")
            config = {}
    
    # 使用全局任务处理器，确保并发控制生效
    processor = get_global_task_processor(config)
    
    # 调度任务
    job_id = processor.schedule_task(task_id)
    
    return job_id is not None

def force_upload_task(task_id, config=None):
    """
    强制上传任务
    
    Args:
        task_id: 任务ID
        config: 配置信息
    
    Returns:
        success: 操作是否成功
    """
    # 获取任务信息
    task = get_task(task_id)
    if not task:
        logger.error(f"任务 {task_id} 不存在")
        return False
    
    # 允许状态为"等待人工审核"、"已完成"、"等待处理"或"准备上传"的任务进行上传
    allowed_states = [TASK_STATES['AWAITING_REVIEW'], TASK_STATES['COMPLETED'], TASK_STATES['PENDING'], TASK_STATES['READY_FOR_UPLOAD']]
    if task['status'] not in allowed_states:
        logger.warning(f"任务 {task_id} 状态为 {task['status']}，只有以下状态的任务可以上传: {', '.join(allowed_states)}")
        return False
    
    # 如果没有提供配置，尝试从Flask app获取
    if not config:
        try:
            from flask import current_app
            if 'Y2A_SETTINGS' in current_app.config:
                config = current_app.config['Y2A_SETTINGS']
                logger.info("从Flask应用获取配置")
        except (ImportError, RuntimeError):
            logger.warning("无法从Flask应用获取配置，使用空配置")
            config = {}
    
    # 使用全局任务处理器，确保并发控制生效
    processor = get_global_task_processor(config)
    
    # 创建任务日志记录器
    task_logger = setup_task_logger(task_id)
    
    try:
        # 直接执行上传步骤
        processor._upload_to_target(task_id, task_logger, allow_missing_translations=True)

        # 上传流程（强制路径）不会进入 process_task 的 finally，需手动唤醒队列
        try:
            import threading, time
            def delayed_check():
                # 等待一下，确保任务状态已写回数据库
                time.sleep(1)
                processor._check_and_start_next_pending_task()
            threading.Thread(target=delayed_check, daemon=True).start()
            logger.info("强制上传结束，已触发检查下一条pending任务")
        except Exception as _e:
            logger.warning(f"强制上传后触发队列检查失败（忽略）：{_e}")

        return True
    except Exception as e:
        task_logger.error(f"强制上传任务 {task_id} 失败: {str(e)}")
        import traceback
        task_logger.error(traceback.format_exc())
        update_task(task_id, status=TASK_STATES['FAILED'], error_message=f"强制上传失败: {str(e)}")
        return False

# 全局任务处理器实例
_global_task_processor = None

def kick_pending_downloads_if_running():
    """闸门打开后立刻看一眼等待队列。处理器还没起来时什么都不做。"""
    proc = _global_task_processor
    if proc is None:
        return
    try:
        proc._check_and_start_next_pending_task()
    except Exception as exc:
        logger.warning(f"恢复下载后调度等待任务失败: {exc}")


def get_global_task_processor(config=None):
    """
    获取全局任务处理器实例，确保并发控制生效
    
    Args:
        config: 配置信息
        
    Returns:
        TaskProcessor: 全局任务处理器实例
    """
    global _global_task_processor

    with _GLOBAL_TASK_PROCESSOR_LOCK:
        if _global_task_processor is None:
            logger.info("创建全局任务处理器实例")
            _global_task_processor = TaskProcessor(config)
        elif config:
            logger.info("配置已更新，刷新全局任务处理器运行时配置")
            _global_task_processor.refresh_config(config)

        return _global_task_processor


def shutdown_global_task_processor():
    """关闭全局任务处理器"""
    global _global_task_processor
    with _GLOBAL_TASK_PROCESSOR_LOCK:
        processor = _global_task_processor
        if processor:
            try:
                processor.shutdown()
            finally:
                _global_task_processor = None
            logger.info("全局任务处理器已关闭")


def _publish_shared_constants():
    """把不可变常量交回各 mixin 模块，保证身份和切片下标与拆分前一致。"""
    import modules.media.download as download_mod
    import modules.pipeline.upload as upload_mod
    import modules.subtitle.translate as translate_mod
    for module in (download_mod, upload_mod, translate_mod):
        for name in module._SHARED_CONSTANT_NAMES:
            setattr(module, name, globals()[name])


_publish_shared_constants()

# 初始化数据库
init_db() 
