#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""视频信息采集、下载和封面恢复。

方法体与拆分前一致。协作对象经 Live 回查 task_manager，取消异常使用
同一 TaskCancelledError 类型。
"""

import json
import os
import subprocess

from modules.pipeline.facade import Live

from modules.pipeline.cancel import TaskCancelledError

DOWNLOADS_DIR = Live('DOWNLOADS_DIR')
TASK_STATES = Live('TASK_STATES')
_raise_if_cancelled = Live('_raise_if_cancelled')
get_task = Live('get_task')
get_task_cancel_event = Live('get_task_cancel_event')
is_task_cancelled = Live('is_task_cancelled')
resolve_youtube_cookies_path = Live('resolve_youtube_cookies_path')
update_task = Live('update_task')
validate_cookies = Live('validate_cookies')

_SHARED_CONSTANT_NAMES = ('TASK_STATES',)

class VideoDownloadMixin:
    
    def _fetch_video_info(self, task_id, youtube_url, task_logger):
        """只采集视频元数据和封面，不下载视频文件"""
        from modules.youtube_handler import download_video_data
        task_logger.info(f"采集视频信息: {youtube_url}")
        update_task(task_id, status='fetching_info')

        _raise_if_cancelled(task_id, task_logger)
        
        cookies_path = resolve_youtube_cookies_path(self.config, task_logger)
        
        # 验证cookies文件
        if cookies_path:
            is_valid, error_msg = validate_cookies(cookies_path, "YouTube")
            if not is_valid:
                task_logger.error(f"YouTube Cookies验证失败: {error_msg}")
                # 尝试不使用cookies继续
                task_logger.info("尝试不使用cookies继续采集信息...")
                cookies_path = None
                
        # 只采集信息
        try:
            cancel_event = get_task_cancel_event(task_id)
            success, result = download_video_data(
                youtube_url,
                task_id,
                cookies_path,
                skip_download=True,
                cancel_event=cancel_event
            )
        except Exception as e:
            if is_task_cancelled(task_id):
                task_logger.info("采集信息过程中检测到任务取消请求")
                raise TaskCancelledError("任务已取消")
            task_logger.error(f"采集视频信息时发生异常: {str(e)}")
            update_task(task_id, status=TASK_STATES['FAILED'], error_message=f"采集信息异常: {str(e)}")
            return
            
        if success:
            task_logger.info("视频信息采集成功")
            metadata_path = result.get('metadata_path')
            video_title = ""
            video_description = ""
            if metadata_path and os.path.exists(metadata_path):
                try:
                    with open(metadata_path, 'r', encoding='utf-8') as f:
                        metadata = json.load(f)
                        video_title = metadata.get('title', '')
                        video_description = metadata.get('description', '')
                except Exception as e:
                    task_logger.error(f"读取视频元数据失败: {str(e)}")
            update_task(
                task_id,
                status='info_fetched',
                video_title_original=video_title,
                description_original=video_description,
                cover_path_local=result.get('cover_path', ''),
                metadata_json_path_local=metadata_path
            )
        else:
            task_logger.error(f"视频信息采集失败: {result}")
            update_task(task_id, status=TASK_STATES['FAILED'], error_message=f"采集信息失败: {result}")

    def _download_video_file(self, task_id, youtube_url, task_logger):
        """审核通过后下载视频文件"""
        from modules.youtube_handler import download_video_data
        task_logger.info(f"审核通过，开始下载视频文件: {youtube_url}")
        update_task(task_id, status=TASK_STATES['DOWNLOADING'])

        _raise_if_cancelled(task_id, task_logger)
        
        cookies_path = resolve_youtube_cookies_path(self.config, task_logger)
        
        # 验证cookies文件
        if cookies_path:
            is_valid, error_msg = validate_cookies(cookies_path, "YouTube")
            if not is_valid:
                task_logger.error(f"YouTube Cookies验证失败: {error_msg}")
                # 尝试不使用cookies继续
                task_logger.info("尝试不使用cookies继续下载...")
                cookies_path = None
                
        # 定义进度回调函数
        def progress_callback(progress_info):
            percent = progress_info.get('percent', 0)
            file_size = progress_info.get('file_size', '')
            speed = progress_info.get('speed', '')
            eta = progress_info.get('eta', '')
            
            # 只显示百分比，简洁明了
            progress_msg = f"{percent:.1f}%"
            
            # 详细信息记录到日志
            detailed_msg = progress_msg
            if file_size:
                detailed_msg += f" / {file_size}"
            if speed:
                detailed_msg += f" @ {speed}"
            if eta:
                detailed_msg += f" ETA {eta}"
            
            # 不再把每次下载进度记录为 INFO 到文件，以减少日志噪声；保留网页进度显示
            task_logger.debug(f"下载进度: {detailed_msg}")
            # 更新任务的上传进度字段用于显示（只显示百分比）
            update_task(task_id, upload_progress=progress_msg, silent=True)
        
        # 只下载视频文件
        try:
            cancel_event = get_task_cancel_event(task_id)
            success, result = download_video_data(
                youtube_url,
                task_id,
                cookies_path,
                only_video=True,
                progress_callback=progress_callback,
                cancel_event=cancel_event
            )
        except Exception as e:
            if is_task_cancelled(task_id):
                task_logger.info("下载过程中检测到任务取消请求")
                raise TaskCancelledError("任务已取消")
            task_logger.error(f"下载视频文件时发生异常: {str(e)}")
            update_task(task_id, status=TASK_STATES['FAILED'], error_message=f"下载异常: {str(e)}")
            return
        if is_task_cancelled(task_id):
            task_logger.info("下载完成前检测到任务取消请求")
            raise TaskCancelledError("任务已取消")
        if success:
            task_logger.info("视频文件下载成功")
            
            # 获取当前任务信息
            task = get_task(task_id)
            
            update_data = {
                'status': TASK_STATES['DOWNLOADED'],
                'video_path_local': result.get('video_path', ''),
                'upload_progress': None  # 清除进度显示
            }
            
            # 如果结果中包含元数据和封面信息，保存这些信息
            # 这是因为我们修改了download_video_data函数，使其在only_video=True时也能返回之前保存的元数据和封面
            if task is not None:
                if result.get('metadata_path') and not task.get('metadata_json_path_local'):
                    update_data['metadata_json_path_local'] = result.get('metadata_path')
                    
                if result.get('cover_path') and not task.get('cover_path_local'):
                    update_data['cover_path_local'] = result.get('cover_path')
            else:
                task_logger.warning("任务对象为 None，无法更新元数据和封面信息")
                
            update_task(task_id, **update_data)
        else:
            task_logger.error(f"视频文件下载失败: {result}")
            update_task(task_id, status=TASK_STATES['FAILED'], error_message=f"下载视频失败: {result}")

    def _recover_cover_path(self, task_id, cover_path, task_logger):
        """尝试恢复缺失的封面文件路径。

        当封面路径为空或文件不存在时（例如直播预告被监控捕获后初始下载失败），
        先在任务目录中搜索已有的封面文件，若仍未找到则从 YouTube 重新采集封面。

        Returns:
            恢复后的封面文件路径，或空字符串（恢复失败时）。
        """
        # 目录边界校验：防止符号链接/路径遍历将 cover_path_local 指向 downloads 目录外
        downloads_dir_real = os.path.realpath(DOWNLOADS_DIR)

        def _is_within_downloads(path):
            path_real = os.path.realpath(path)
            try:
                return os.path.commonpath([downloads_dir_real, path_real]) == downloads_dir_real, path_real
            except ValueError:
                return False, path_real

        if cover_path:
            cover_ok, cover_real = _is_within_downloads(cover_path)
            if cover_ok and os.path.isfile(cover_real):
                return cover_real

        task_logger.info("检测到封面文件缺失，尝试恢复封面...")

        task_dir_ok, task_dir = _is_within_downloads(os.path.join(DOWNLOADS_DIR, task_id))
        if not task_dir_ok:
            task_logger.warning(f"任务目录越界，跳过本地封面恢复: {task_id}")
            task_dir = ''

        # 1) 尝试在任务目录中搜索已有的封面文件
        if task_dir and os.path.isdir(task_dir):
            cover_candidates = [
                'cover.jpg', 'cover.png', 'cover.webp',
                'thumbnail.jpg', 'thumbnail.png', 'thumbnail.webp',
            ]
            for name in cover_candidates:
                candidate_ok, candidate = _is_within_downloads(os.path.join(task_dir, name))
                if not candidate_ok:
                    task_logger.warning(f"检测到越界封面候选路径，已跳过: {name}")
                    continue
                if os.path.isfile(candidate):
                    task_logger.info(f"在任务目录中找到封面文件: {name}")
                    update_task(task_id, cover_path_local=candidate, silent=True)
                    return candidate
            # 搜索任意图片文件
            try:
                with os.scandir(task_dir) as entries:
                    for entry in entries:
                        entry_ok, entry_path = _is_within_downloads(entry.path)
                        if not entry_ok:
                            task_logger.warning(f"检测到越界图片路径，已跳过: {entry.name}")
                            continue
                        if entry.is_file() and entry.name.lower().endswith(('.jpg', '.jpeg', '.png', '.webp')) and os.path.isfile(entry_path):
                            task_logger.info(f"在任务目录中找到图片文件作为封面: {entry.name}")
                            update_task(task_id, cover_path_local=entry_path, silent=True)
                            return entry_path
            except OSError as e:
                task_logger.warning(f"扫描任务目录封面失败，将尝试从 YouTube 重新采集: {task_dir}, error: {e}")

        # 2) 从 YouTube 重新采集封面（仅下载缩略图，不清空任务目录）
        task = get_task(task_id)
        youtube_url = task.get('youtube_url', '') if task else ''
        if not youtube_url:
            task_logger.warning("无法获取YouTube URL，无法重新采集封面")
            return ''

        task_logger.info(f"从 YouTube 重新采集封面: {youtube_url}")
        try:
            from modules.youtube_handler import _find_yt_dlp_command, build_proxy_url, \
                _append_yt_dlp_network_args, _resolve_safe_cookies_path

            if task_dir:
                os.makedirs(task_dir, exist_ok=True)
            else:
                task_logger.warning("任务目录越界，跳过从 YouTube 重新采集封面")
                return ''

            yt_dlp_cmd = _find_yt_dlp_command(task_logger)
            cmd = yt_dlp_cmd + [
                '-o', os.path.join(task_dir, 'video.%(ext)s'),
                '--write-thumbnail',
                '--skip-download',
                '--no-write-info-json',
                '--no-playlist',
                '--ignore-no-formats-error',
                youtube_url,
            ]

            cookies_path = resolve_youtube_cookies_path(self.config, task_logger)

            if cookies_path:
                cookies_path = _resolve_safe_cookies_path(cookies_path, task_logger)

            config = self.config
            proxy_url = build_proxy_url(config)
            _append_yt_dlp_network_args(cmd, proxy_url=proxy_url, cookies_path=cookies_path)

            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=120,
                encoding='utf-8', errors='replace',
            )
            task_logger.debug(f"yt-dlp 封面采集输出: {proc.stdout}")
            if proc.returncode != 0:
                task_logger.warning(f"yt-dlp 封面采集返回非零状态: {proc.returncode}, stderr: {proc.stderr}")

            # yt-dlp 使用 -o video.%(ext)s 模板，但缩略图扩展名/文件名并不一定严格固定。
            # 重新扫描任务目录，优先匹配 video*.{jpg,jpeg,png,webp}，再回退到目录内其他图片文件。
            if os.path.isdir(task_dir):
                allowed_exts = {'.jpg', '.jpeg', '.png', '.webp'}
                try:
                    dir_entries = sorted(os.listdir(task_dir))
                except OSError as scan_error:
                    task_logger.warning(f"扫描任务目录中的封面文件失败: {scan_error}")
                    dir_entries = []

                preferred_names = []
                fallback_names = []
                for name in dir_entries:
                    candidate_ok, candidate = _is_within_downloads(os.path.join(task_dir, name))
                    if not candidate_ok or not os.path.isfile(candidate):
                        continue
                    _, ext = os.path.splitext(name)
                    if ext.lower() not in allowed_exts:
                        continue
                    if os.path.splitext(name)[0].lower().startswith('video'):
                        preferred_names.append(name)
                    else:
                        fallback_names.append(name)

                for name in preferred_names + fallback_names:
                    candidate_ok, candidate = _is_within_downloads(os.path.join(task_dir, name))
                    if not candidate_ok:
                        continue
                    if os.path.isfile(candidate):
                        task_logger.info(f"找到封面文件 {name}，直接作为封面使用: {candidate}")
                        update_task(task_id, cover_path_local=candidate, silent=True)
                        return candidate

            task_logger.warning("从 YouTube 重新采集封面未获取到文件")
        except Exception as e:
            task_logger.warning(f"重新采集封面时发生异常: {e}")
        return ''
