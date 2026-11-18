#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Bilibili / AcFun 上传编排。

上传锁仍是 task_manager.upload_semaphore。方法里原来的 global 读取改为
在初始化和加锁前重新取一次，这样行为和拆分前相同。
"""

import json
import os
import re

from modules.pipeline.facade import Live

from modules.pipeline.cancel import TaskCancelledError

BILIBILI_TAG_LIMIT = Live('BILIBILI_TAG_LIMIT')
CONTENT_MODERATION_ERROR_CATEGORY = Live('CONTENT_MODERATION_ERROR_CATEGORY')
PIPELINE_STAGE_GENERATE_TAGS = Live('PIPELINE_STAGE_GENERATE_TAGS')
PIPELINE_STAGE_MODERATE_CONTENT = Live('PIPELINE_STAGE_MODERATE_CONTENT')
PIPELINE_STAGE_RECOMMEND_PARTITION = Live('PIPELINE_STAGE_RECOMMEND_PARTITION')
PLATFORM_ACFUN = Live('PLATFORM_ACFUN')
PLATFORM_BILIBILI = Live('PLATFORM_BILIBILI')
TASK_STATES = Live('TASK_STATES')
UPLOAD_TARGET_ACFUN = Live('UPLOAD_TARGET_ACFUN')
UPLOAD_TARGET_BILIBILI = Live('UPLOAD_TARGET_BILIBILI')
UPLOAD_TARGET_BOTH = Live('UPLOAD_TARGET_BOTH')
_as_bool = Live('_as_bool')
_candidate_subtitle_paths = Live('_candidate_subtitle_paths')
_get_completed_stages = Live('_get_completed_stages')
_get_effective_metadata_limits = Live('_get_effective_metadata_limits')
_get_missing_required_translation_fields = Live('_get_missing_required_translation_fields')
_get_pending_upload_platforms = Live('_get_pending_upload_platforms')
_get_task_partition_id = Live('_get_task_partition_id')
_get_task_upload_target = Live('_get_task_upload_target')
_get_upload_platforms_for_target = Live('_get_upload_platforms_for_target')
_is_asr_enabled = Live('_is_asr_enabled')
_is_asr_subtitle_artifact = Live('_is_asr_subtitle_artifact')
_mark_stage_done = Live('_mark_stage_done')
_needs_cover_moderation = Live('_needs_cover_moderation')
_normalize_tags_list = Live('_normalize_tags_list')
_qc_result_allows_embed = Live('_qc_result_allows_embed')
_raise_if_cancelled = Live('_raise_if_cancelled')
_safe_json_loads = Live('_safe_json_loads')
_subtitle_embed_allowed = Live('_subtitle_embed_allowed')
delete_task_files = Live('delete_task_files')
DOWNLOADS_DIR = Live('DOWNLOADS_DIR')
kick_pending_downloads_if_running = Live('kick_pending_downloads_if_running')
get_task = Live('get_task')
get_task_cancel_event = Live('get_task_cancel_event')
init_upload_semaphore = Live('init_upload_semaphore')
is_preset_tags_effective = Live('is_preset_tags_effective')
parse_preset_tags = Live('parse_preset_tags')
resolve_cookie_file_path = Live('resolve_cookie_file_path')
resolve_upload_tags = Live('resolve_upload_tags')
update_task = Live('update_task')
validate_cookies = Live('validate_cookies')

_SHARED_CONSTANT_NAMES = ('BILIBILI_TAG_LIMIT', 'CONTENT_MODERATION_ERROR_CATEGORY', 'PIPELINE_STAGE_GENERATE_TAGS', 'PIPELINE_STAGE_MODERATE_CONTENT', 'PIPELINE_STAGE_RECOMMEND_PARTITION', 'PLATFORM_ACFUN', 'PLATFORM_BILIBILI', 'TASK_STATES', 'UPLOAD_TARGET_ACFUN', 'UPLOAD_TARGET_BILIBILI', 'UPLOAD_TARGET_BOTH')

def _upload_semaphore():
    import modules.task_manager as task_manager
    return task_manager.upload_semaphore


class UploadMixin:

    def _prepare_subtitle_for_upload(self, task_id, task_logger):
        """上传前字幕处理（ASR/翻译/嵌入）。返回最新任务对象。"""
        task = get_task(task_id)
        if not task:
            task_logger.error("任务不存在，无法执行上传前字幕处理")
            return None

        video_path = task.get('video_path_local', '')
        if not video_path or not os.path.exists(video_path):
            task_logger.warning("视频文件不存在，无法进行上传前的字幕处理")
            return get_task(task_id)

        try:
            _raise_if_cancelled(task_id, task_logger)
            task_dir = os.path.dirname(video_path)

            # 门控先于旧产物复用：否则质检失败的标记会被已存在的 _with_subtitle.mp4
            # 完全绕过，成片继续沿用旧的错误字幕。
            #
            # 注意这里是「延迟拦截」而不是直接 return：直接返回会连带阻断下面的
            # ASR 重新生成分支，导致用户修好 VAD 配置后重跑依然拿不到字幕。
            # 正确语义是——既有（可能已被判定不合格的）产物不得复用烧录，
            # 但新一轮 ASR 产物仍可在复位标记并重新质检后烧录。
            embed_quality_state = str(task.get('subtitle_quality_state') or '').strip().lower() or 'ok'
            qc_failed = task.get('subtitle_qc_failed') == 1
            embed_blocked = not _subtitle_embed_allowed(
                self.config,
                embed_quality_state,
                qc_failed,
                qc_reason=task.get('subtitle_qc_reason'),
                task_logger=task_logger,
            )
            if embed_blocked:
                task_logger.warning(
                    "上传前烧录被门控拦截（quality_state=%s, qc_failed=%s）：不会复用既有字幕产物",
                    embed_quality_state,
                    qc_failed,
                )

            config_embed_enabled = _as_bool(self.config.get('SUBTITLE_EMBED_IN_VIDEO', True))
            embed_allowed = config_embed_enabled and not embed_blocked

            reusable_embedded_video = self._get_embedded_video_candidate(
                video_path,
                subtitle_paths=_candidate_subtitle_paths(task, task_dir),
            )
            if reusable_embedded_video and not embed_blocked:
                if reusable_embedded_video != video_path:
                    update_task(task_id, video_path_local=reusable_embedded_video)
                task_logger.info("检测到已存在带字幕视频，复用现有转码产物")
                return get_task(task_id)
            if reusable_embedded_video and embed_blocked:
                task_logger.info("检测到既有带字幕视频，但本次被门控拦截，不复用该转码产物")

            should_embed_subtitle = embed_allowed
            translation_enabled = _as_bool(self.config.get('SUBTITLE_TRANSLATION_ENABLED', False))
            subtitle_path_original, subtitle_path_translated, subtitle_language = self._resolve_existing_subtitle_assets(
                task_id, task, task_dir
            )
            if subtitle_path_original and not subtitle_language:
                subtitle_language = self._detect_subtitle_language(subtitle_path_original)

            reusable_updates = {}
            if subtitle_path_original and task.get('subtitle_path_original') != subtitle_path_original:
                reusable_updates['subtitle_path_original'] = subtitle_path_original
            if subtitle_path_translated and task.get('subtitle_path_translated') != subtitle_path_translated:
                reusable_updates['subtitle_path_translated'] = subtitle_path_translated
            if subtitle_language and task.get('subtitle_language_detected') != subtitle_language:
                reusable_updates['subtitle_language_detected'] = subtitle_language
            if reusable_updates:
                update_task(task_id, **reusable_updates)
                task = get_task(task_id) or task

            if subtitle_path_translated and os.path.exists(subtitle_path_translated):
                if not should_embed_subtitle:
                    task_logger.info("检测到已存在翻译字幕且未开启烧录，复用现有字幕产物")
                    return get_task(task_id)
                # 质检对象必须是**源**字幕：``translated_*.srt`` 不匹配
                # ``asr_*`` 前缀，把它传进质检闸门会被判为「非 ASR 产物」而直接
                # 放行 —— 译文由 ASR 源字幕翻译而来，源头不合格就没有烧录价值。
                # 源字幕缺失时退回译文本身，但必须把来源（任务里记录的源字幕路径）
                # 显式继承过去，否则那条退回分支恒为放行（force_asr 的意义所在）。
                recorded_original = str(task.get('subtitle_path_original') or '')
                if subtitle_path_original and os.path.exists(subtitle_path_original):
                    qc_target = subtitle_path_original
                    qc_force_asr = None
                else:
                    qc_target = subtitle_path_translated
                    qc_force_asr = _is_asr_subtitle_artifact(task_id, recorded_original)
                if not self._ensure_asr_subtitle_qc(
                    task_id, qc_target, task_logger, embed_quality_state, force_asr=qc_force_asr
                )[0]:
                    return get_task(task_id)

                embedded_video_path = self._embed_subtitle_in_video(
                    task_id, video_path, subtitle_path_translated, task_logger
                )
                if embedded_video_path:
                    update_task(
                        task_id,
                        video_path_local=embedded_video_path,
                        subtitle_path_original=subtitle_path_original,
                        subtitle_path_translated=subtitle_path_translated,
                        subtitle_language_detected=subtitle_language,
                        subtitle_warning_message=None,
                    )
                    task_logger.info("检测到已存在翻译字幕，仅补做烧录")
                else:
                    task_logger.warning("复用已有翻译字幕烧录失败，保留原视频继续上传")
                    update_task(task_id, subtitle_warning_message='subtitle_embed_failed')
                return get_task(task_id)

            if subtitle_path_original and (str(subtitle_language or '').startswith('zh') or not translation_enabled):
                if not should_embed_subtitle:
                    task_logger.info("检测到已存在字幕且未开启烧录，复用现有字幕产物")
                    return get_task(task_id)
                if not self._ensure_asr_subtitle_qc(
                    task_id, subtitle_path_original, task_logger, embed_quality_state
                )[0]:
                    return get_task(task_id)

                embedded_video_path = self._embed_subtitle_in_video(
                    task_id, video_path, subtitle_path_original, task_logger
                )
                if embedded_video_path:
                    update_task(
                        task_id,
                        video_path_local=embedded_video_path,
                        subtitle_path_original=subtitle_path_original,
                        subtitle_path_translated=None,
                        subtitle_language_detected=subtitle_language or 'zh',
                        subtitle_warning_message=None,
                    )
                    task_logger.info("检测到已存在字幕，仅补做烧录")
                else:
                    task_logger.warning("复用已有字幕烧录失败，保留原视频继续上传")
                    update_task(task_id, subtitle_warning_message='subtitle_embed_failed')
                return get_task(task_id)

            if not translation_enabled and (subtitle_path_original or subtitle_path_translated):
                task_logger.info("检测到已有字幕产物但未开启烧录，跳过上传前重复字幕处理")
                return get_task(task_id)

            subtitle_files = []
            try:
                for name in os.listdir(task_dir):
                    if not isinstance(name, str):
                        continue
                    lower = name.lower()
                    if lower.endswith('.srt') or lower.endswith('.vtt'):
                        subtitle_files.append(os.path.join(task_dir, name))
            except Exception:
                pass

            if _is_asr_enabled(self.config):
                if translation_enabled:
                    task_logger.info("上传前执行字幕处理：启用字幕翻译，先尝试ASR/翻译/嵌入")
                    self._translate_subtitle(task_id, task_logger)
                else:
                    if not subtitle_files:
                        task_logger.info("上传前执行字幕处理：启用ASR但未启用字幕翻译，生成基础字幕文件")
                        try:
                            from modules.speech_recognition import create_speech_recognizer_from_config
                            recognizer = create_speech_recognizer_from_config(self.config, task_id)
                        except Exception as e:
                            recognizer = None
                            task_logger.error(f"创建语音识别器失败: {e}")
                        if recognizer:
                            _t2 = get_task(task_id)
                            prev_status2 = _t2['status'] if _t2 else TASK_STATES['UPLOADING']
                            update_task(task_id, status=TASK_STATES['ASR_TRANSCRIBING'])
                            asr_ext = '.srt' if str(self.config.get('SPEECH_RECOGNITION_OUTPUT_FORMAT', 'srt')).lower() == 'srt' else '.vtt'
                            asr_subtitle_path = os.path.join(task_dir, f"asr_{task_id}{asr_ext}")
                            _raise_if_cancelled(task_id, task_logger)
                            # 新一轮 ASR 即将复位全部旧标记，因此本轮产物可按配置重新
                            # 决定是否烧录（不再受入口时的 embed_blocked 约束）。
                            should_embed_subtitle = config_embed_enabled
                            update_task(
                                task_id,
                                asr_warning_message=None,
                                subtitle_qc_failed=0,
                                subtitle_qc_reason=None,
                                subtitle_qc_score=None,
                                subtitle_qc_checked_at=None,
                                subtitle_warning_message=None,
                                subtitle_quality_state=None,
                            )
                            out_path = recognizer.transcribe_video_to_subtitles(video_path, asr_subtitle_path)
                            update_task(task_id, status=prev_status2)
                            quality_state = (
                                str(getattr(recognizer, 'last_quality_state', '') or 'ok').strip().lower()
                                or 'ok'
                            )
                            if out_path and os.path.exists(out_path):
                                detected_lang = self._detect_subtitle_language(out_path)
                                update_task(
                                    task_id,
                                    asr_warning_message=(getattr(recognizer, 'last_warning_message', '') or None),
                                    subtitle_quality_state=quality_state,
                                    subtitle_path_original=out_path,
                                    subtitle_path_translated=None,
                                    subtitle_language_detected=detected_lang
                                )
                                task_logger.info(f"ASR 生成基础字幕成功: {os.path.basename(out_path)}")

                                if quality_state == 'failed' and _as_bool(
                                    self.config.get('ASR_FAILURE_BLOCKS_EMBED', True)
                                ):
                                    task_logger.error(
                                        "上传前 ASR 质量结局为 failed，拒绝烧录字幕并继续上传原视频"
                                    )
                                    self._quarantine_rejected_subtitle(out_path, task_logger)
                                    update_task(
                                        task_id,
                                        subtitle_warning_message='asr_failed_block_embed',
                                    )
                                elif should_embed_subtitle:
                                    qc_result = self._run_subtitle_qc(
                                        task_id,
                                        out_path,
                                        task_logger,
                                        strict=(quality_state == 'degraded'),
                                    )
                                    # 质检裁决统一走 ``_qc_result_allows_embed``：
                                    # 用户主动关闭质检（SUBTITLE_QC_DISABLED）按配置放行；
                                    # 「质检没跑成」（None）则由逃生口
                                    # ASR_FAILURE_BLOCKS_EMBED 决定（此前这条路径完全不
                                    # 读逃生口，与 README 的承诺相反）。
                                    if _qc_result_allows_embed(qc_result, self.config):
                                        embedded_video_path = self._embed_subtitle_in_video(
                                            task_id, video_path, out_path, task_logger
                                        )
                                        if embedded_video_path:
                                            update_task(
                                                task_id,
                                                video_path_local=embedded_video_path,
                                                subtitle_path_original=out_path,
                                                subtitle_path_translated=None,
                                                subtitle_language_detected=detected_lang,
                                                subtitle_warning_message=None,
                                            )
                                            task_logger.info("ASR 字幕烧录完成")
                                        else:
                                            task_logger.warning("ASR 字幕烧录失败，保留原视频继续上传")
                                            update_task(task_id, subtitle_warning_message='subtitle_embed_failed')
                                    else:
                                        reason_text = 'qc_unavailable' if qc_result is None else 'subtitle_qc_rejected'
                                        task_logger.warning(f"ASR 字幕质检未通过（{reason_text}），跳过上传前烧录")
                                        self._quarantine_rejected_subtitle(out_path, task_logger)
                                        update_task(task_id, subtitle_warning_message=reason_text)
                            else:
                                update_task(
                                    task_id,
                                    asr_warning_message=(getattr(recognizer, 'last_warning_message', '') or None),
                                    subtitle_quality_state='failed',
                                )
                                self._mark_subtitle_issue(task_id, 'asr_no_subtitle')
                                task_logger.warning("ASR 未能生成字幕，继续上传流程")
                    else:
                        task_logger.info("已存在字幕文件，跳过ASR 生成")
            else:
                task_logger.info("未启用语音识别，跳过上传前的字幕处理")
        except TaskCancelledError:
            task_logger.info("上传前字幕处理检测到任务取消请求")
            raise
        except Exception as e:
            task_logger.error(f"上传前字幕处理出现异常: {e}")

        return get_task(task_id)
    
    def _upload_to_target(self, task_id, task_logger, allow_missing_translations=False):
        """按任务平台分发上传实现。"""
        task = get_task(task_id)
        if not task:
            task_logger.error("任务不存在")
            return

        if not self._ensure_required_translations_ready(
            task_id,
            task,
            task_logger,
            allow_missing_translations=allow_missing_translations,
        ):
            return

        if allow_missing_translations:
            task = self._ensure_force_upload_metadata_ready(task_id, task_logger)
            if not task:
                return

        upload_target = _get_task_upload_target(task)
        pending_platforms = _get_pending_upload_platforms(task, upload_target)
        task_logger.info(f"上传分发目标平台: {upload_target}")
        task_logger.info(f"待上传平台: {pending_platforms}")

        if not pending_platforms:
            task_logger.info("目标平台均已有上传结果，跳过重复上传")
            if task.get('status') != TASK_STATES['COMPLETED']:
                update_task(task_id, status=TASK_STATES['COMPLETED'], error_message=None, upload_progress=None)
            return

        cover_check_pending = _needs_cover_moderation(task, self.config)
        if cover_check_pending:
            task_logger.info("上传前补充历史任务的封面审核")
            self._moderate_content(task_id, task_logger)
            task = get_task(task_id)
            if not task:
                return

        cover_check_enabled = (self.config.get('CONTENT_MODERATION_ENABLED', False)
                               and self.config.get('CONTENT_MODERATION_COVER_ENABLED', False))
        if cover_check_pending or (cover_check_enabled and not allow_missing_translations):
            moderation = _safe_json_loads(task.get('moderation_result'), {})
            cover_result = moderation.get('cover') if isinstance(moderation, dict) else None
            if (not isinstance(moderation, dict)
                    or (not allow_missing_translations and moderation.get('overall_pass') is not True)
                    or not isinstance(cover_result, dict) or cover_result.get('pass') is not True
                    or _needs_cover_moderation(task, self.config)):
                task_logger.info("封面审核未完成或内容需要人工处理，暂停上传")
                update_task(task_id, status=TASK_STATES['AWAITING_REVIEW'],
                            error_category=CONTENT_MODERATION_ERROR_CATEGORY)
                return

        if upload_target == UPLOAD_TARGET_BOTH:
            task_logger.info("双平台上传将字幕预处理延后到各平台上传阶段执行，确保视频已下载后再处理字幕")
            subtitle_prepared_in_this_round = False

            # 双平台投稿：按 AcFun -> bilibili 顺序执行，且对已成功的平台幂等跳过
            if UPLOAD_TARGET_ACFUN in pending_platforms:
                self._upload_to_acfun(task_id, task_logger, subtitle_prepared=False)
                subtitle_prepared_in_this_round = True
                task = get_task(task_id)
                if not task or task.get('status') == TASK_STATES['FAILED']:
                    return
            else:
                task_logger.info("检测到已有 AcFun 上传结果，跳过 AcFun 上传")

            if UPLOAD_TARGET_BILIBILI in pending_platforms:
                self._upload_to_bilibili(
                    task_id,
                    task_logger,
                    subtitle_prepared=subtitle_prepared_in_this_round
                )
            else:
                task_logger.info("检测到已有 bilibili 上传结果，跳过 bilibili 上传")
            return

        if upload_target == UPLOAD_TARGET_BILIBILI:
            if UPLOAD_TARGET_BILIBILI in pending_platforms:
                self._upload_to_bilibili(task_id, task_logger)
            else:
                task_logger.info("检测到已有 bilibili 上传结果，跳过 bilibili 上传")
            return

        if UPLOAD_TARGET_ACFUN in pending_platforms:
            self._upload_to_acfun(task_id, task_logger)
        else:
            task_logger.info("检测到已有 AcFun 上传结果，跳过 AcFun 上传")

    def _ensure_force_upload_metadata_ready(self, task_id, task_logger):
        """强制上传前继续未完成的 AI 处理阶段。"""
        task = get_task(task_id)
        if not task:
            task_logger.error("任务不存在，无法继续上传前 AI 处理流程")
            return None

        from modules.utils import safe_str
        completed_stages = _get_completed_stages(task, getattr(self, 'config', None))

        def _has_usable_text(current_task):
            title_text = safe_str(current_task.get('video_title_translated') or current_task.get('video_title_original'))
            description_text = safe_str(current_task.get('description_translated') or current_task.get('description_original'))
            return bool(title_text or description_text)

        tags = _normalize_tags_list(task.get('tags_generated'))
        # 预设标签生效时同样要走标签阶段：即便「自动生成标签」关闭，也要把预设标签
        # 落到任务上（与 run_task 的阶段门控同口径）。这里只在标签为空时补跑，
        # 避免每次强制上传都重新调用标签 AI（已有标签的任务在上传路径仍会合并预设）。
        from modules.upload_templates import settings_for_task
        from modules.task_manager import _profile_needs_partition_stage
        runtime_config = settings_for_task(self.config, task)
        preset_effective = is_preset_tags_effective(runtime_config)
        if (self.config.get('GENERATE_TAGS', True) or preset_effective) and not tags:
            if _has_usable_text(task):
                task_logger.info("强制上传前检测到标签为空，继续执行标签生成阶段")
                self._generate_tags(task_id, task_logger)
                task = get_task(task_id) or task
                completed_stages = _mark_stage_done(task_id, completed_stages, PIPELINE_STAGE_GENERATE_TAGS)
            elif preset_effective:
                preset_only = parse_preset_tags(runtime_config.get('PRESET_TAGS', ''))
                task_logger.warning("强制上传前标签缺失且缺少标题和简介，改为只写入预设标签")
                update_task(
                    task_id,
                    tags_generated=json.dumps(preset_only, ensure_ascii=False),
                    silent=True,
                )
                task = get_task(task_id) or task
                completed_stages = _mark_stage_done(task_id, completed_stages, PIPELINE_STAGE_GENERATE_TAGS)
            else:
                task_logger.warning("强制上传前标签缺失，但缺少标题和简介，无法补生成标签")
                update_task(task_id, tags_generated=json.dumps([], ensure_ascii=False), silent=True)
                task = get_task(task_id) or task
                completed_stages = _mark_stage_done(task_id, completed_stages, PIPELINE_STAGE_GENERATE_TAGS)

        if runtime_config.get('RECOMMEND_PARTITION', False) or _profile_needs_partition_stage(runtime_config):
            task = get_task(task_id) or task
            upload_target = _get_task_upload_target(task)
            missing_partition_platforms = [
                platform
                for platform in _get_upload_platforms_for_target(upload_target)
                if not _get_task_partition_id(task, platform, prefer_selected=True)
            ]
            if missing_partition_platforms:
                if _has_usable_text(task):
                    task_logger.info(
                        f"强制上传前检测到分区缺失 {missing_partition_platforms}，继续执行分区推荐阶段"
                    )
                    self._recommend_partition(task_id, task_logger)
                    task = get_task(task_id) or task
                    completed_stages = _mark_stage_done(task_id, completed_stages, PIPELINE_STAGE_RECOMMEND_PARTITION)
                else:
                    task_logger.warning("强制上传前分区缺失，但缺少标题和简介，无法补推荐分区")

        if self.config.get('CONTENT_MODERATION_ENABLED', False):
            task = get_task(task_id) or task
            if not task.get('moderation_result'):
                task_logger.info("强制上传前检测到内容审核未完成，继续执行内容审核阶段")
                self._moderate_content(task_id, task_logger)
                task = get_task(task_id) or task
                if task.get('status') == TASK_STATES['AWAITING_REVIEW']:
                    task_logger.info("内容审核需要人工处理，暂停强制上传")
                    return None
                completed_stages = _mark_stage_done(task_id, completed_stages, PIPELINE_STAGE_MODERATE_CONTENT)

        return get_task(task_id) or task

    def _upload_to_bilibili(self, task_id, task_logger, subtitle_prepared=False):
        """上传到 Bilibili - 带并发控制"""
        task = get_task(task_id)
        if not task:
            task_logger.error("任务不存在")
            return

        upload_semaphore = _upload_semaphore()
        if upload_semaphore is None:
            task_logger.warning("upload_semaphore 为 None，正在初始化...")
            init_upload_semaphore(1)
            upload_semaphore = _upload_semaphore()
            task_logger.info(f"upload_semaphore 初始化完成，当前值: {upload_semaphore}")
            if upload_semaphore is None:
                task_logger.error("upload_semaphore 初始化失败，无法继续执行任务")
                return

        from modules.bilibili_accounts import account_upload_lock, resolve_task_account
        account = resolve_task_account(task, self.config)
        if self._defer_bilibili_cooldown(task_id, task_logger, account):
            return

        task_logger.info("等待获取上传锁...")
        try:
            assert upload_semaphore is not None, "upload_semaphore 应该已经初始化"
            with upload_semaphore:
                task_logger.info("获得上传锁，开始排队该 bilibili 账号")
                with account_upload_lock(str(account.get("id") or "")):
                    task = get_task(task_id) or task
                    if self._defer_bilibili_cooldown(task_id, task_logger, account):
                        task_logger.info("该账号仍在冷却，已跳过本次投稿")
                        return
                    task_logger.info("获得该账号的上传锁，开始上传到 Bilibili")
                    self._do_upload_to_bilibili(task_id, task_logger, subtitle_prepared=subtitle_prepared)
                task_logger.info("释放上传锁")
        except Exception as e:
            task_logger.error(f"获取或使用上传锁时出错: {e}")
            import traceback
            task_logger.error(traceback.format_exc())
            update_task(
                task_id,
                status=TASK_STATES['FAILED'],
                error_message=f"上传锁异常: {str(e)}"
            )
            return

    def _upload_to_acfun(self, task_id, task_logger, subtitle_prepared=False):
        """上传到AcFun - 带并发控制"""
        from modules.acfun_uploader import AcfunUploader
        
        task = get_task(task_id)
        if not task:
            task_logger.error("任务不存在")
            return
        
        # 使用信号量控制并发上传
        upload_semaphore = _upload_semaphore()
        if upload_semaphore is None:
            task_logger.warning("upload_semaphore 为 None，正在初始化...")
            init_upload_semaphore(1)
            upload_semaphore = _upload_semaphore()
            task_logger.info(f"upload_semaphore 初始化完成，当前值: {upload_semaphore}")
            # 确保初始化成功
            if upload_semaphore is None:
                task_logger.error("upload_semaphore 初始化失败，无法继续执行任务")
                return
        else:
            task_logger.info(f"upload_semaphore 已初始化，当前值: {upload_semaphore}")
        
        task_logger.info("等待获取上传锁...")
        try:
            # 类型断言，告诉 Pylance upload_semaphore 不是 None
            assert upload_semaphore is not None, "upload_semaphore 应该已经初始化"
            with upload_semaphore:
                task_logger.info("获得上传锁，开始上传到AcFun")
                self._do_upload_to_acfun(task_id, task_logger, subtitle_prepared=subtitle_prepared)
                task_logger.info("释放上传锁")
        except Exception as e:
            task_logger.error(f"获取或使用上传锁时出错: {e}")
            import traceback
            task_logger.error(traceback.format_exc())
            # 确保更新任务状态为失败
            update_task(
                task_id,
                status=TASK_STATES['FAILED'],
                error_message=f"上传锁异常: {str(e)}"
            )
            return
    
    def _do_upload_to_acfun(self, task_id, task_logger, subtitle_prepared=False):
        """实际执行上传到AcFun的逻辑"""
        from modules.acfun_uploader import AcfunUploader
        
        task = get_task(task_id)
        if not task:
            task_logger.error("任务不存在")
            return
        
        update_task(task_id, status=TASK_STATES['UPLOADING'])
        
        # 获取任务信息
        video_path = task.get('video_path_local', '') if task else ''
        cover_path = task.get('cover_path_local', '') if task else ''
        title = (task.get('video_title_translated', '') or task.get('video_title_original', '')) if task else ''
        description = (task.get('description_translated', '') or task.get('description_original', '')) if task else ''
        partition_id = _get_task_partition_id(task, UPLOAD_TARGET_ACFUN, prefer_selected=True) if task else ''
        from modules.upload_templates import apply_metadata_templates, settings_for_task
        runtime_config = settings_for_task(self.config, task)
        missing_translation_fields = _get_missing_required_translation_fields(task, runtime_config)
        if missing_translation_fields:
            task_logger.warning(
                f"当前上传路径存在缺失译文字段 {missing_translation_fields}，将回退原文继续上传"
            )
        fixed_acfun_pid = str(runtime_config.get('FIXED_PARTITION_ID', '') or '').strip()
        if fixed_acfun_pid:
            partition_id = fixed_acfun_pid
        
        # 如果没有视频文件，先下载视频
        if not video_path or not os.path.exists(video_path):
            task_logger.info("检测到视频文件缺失，开始下载视频文件...")
            youtube_url = task.get('youtube_url', '') if task else ''
            if not youtube_url:
                task_logger.error("无法获取YouTube URL，无法下载视频")
                update_task(
                    task_id,
                    status=TASK_STATES['FAILED'],
                    error_message="无法获取YouTube URL，无法下载视频"
                )
                return
            
            if self._download_video_file(task_id, youtube_url, task_logger) == 'held':
                return

            # 重新获取任务信息
            task = get_task(task_id)
            video_path = task.get('video_path_local', '') if task else ''
            cover_path = task.get('cover_path_local', '') if task else ''
        
        # 无论封面文件当前是否存在，都先统一做一次恢复/校验：
        # _recover_cover_path() 内部已包含 realpath + commonpath 的 downloads 目录边界检查，
        # 可阻止路径遍历或 symlink 指向 downloads 外部的情况；对于合法且已存在的路径，
        # 其 fast-return 会直接返回已校验过的路径，不改变原有功能。
        cover_path = self._recover_cover_path(task_id, cover_path, task_logger)
        
        if not subtitle_prepared:
            task = self._prepare_subtitle_for_upload(task_id, task_logger) or task
            video_path = task.get('video_path_local', '') if task else video_path

        # 重新设置状态为上传中（字幕翻译可能已在上述步骤执行）
        update_task(task_id, status=TASK_STATES['UPLOADING'])

        # 解析标签（预设标签生效时：预设在前，其余标签紧随，并按 AcFun 上限截断）
        tags = resolve_upload_tags(
            runtime_config,
            task.get('tags_generated') if task else None,
            PLATFORM_ACFUN,
            logger_obj=task_logger,
        )
        
        # 获取元数据
        metadata_path = task.get('metadata_json_path_local', '') if task else ''
        original_url = ''
        original_uploader = ''
        original_upload_date = ''
        
        if metadata_path and os.path.exists(metadata_path):
            try:
                with open(metadata_path, 'r', encoding='utf-8') as f:
                    metadata = json.load(f)
                    original_url = metadata.get('webpage_url', '')
                    original_uploader = metadata.get('uploader', '')
                    original_upload_date = metadata.get('upload_date', '')
            except Exception as e:
                task_logger.error(f"读取视频元数据失败: {str(e)}")

        profile = runtime_config.get("_UPLOAD_PROFILE")
        title, description, repost_notice_text = apply_metadata_templates(
            profile,
            title,
            description,
            original_url or (task.get('youtube_url', '') if task else ''),
            original_uploader,
            original_upload_date,
        )

        # 可选：将YouTube上传者名字放到标签第一位（注意标签数量限制）
        if self.config.get('YOUTUBE_UPLOADER_AS_FIRST_TAG', False):
            from modules.utils import safe_str
            uploader_tag = safe_str(original_uploader).strip()
            if uploader_tag:
                normalized_uploader = uploader_tag.lower()
                cleaned_tags = []
                for tag in tags:
                    tag_value = safe_str(tag).strip()
                    if not tag_value:
                        continue
                    if tag_value.lower() == normalized_uploader:
                        continue
                    cleaned_tags.append(tag_value)
                # 传给 AcFun 的列表必须按 AcFun 上限截断，但**回写不能用这份截断结果**：
                # both 场景下 bilibili 之后会从库里重新读标签（_upload_to_target 固定
                # AcFun → bilibili 顺序），写回 6 个会把平台上限内本可保留的标签一并丢掉
                # （PR #147 复审 P2-2）。因此回写按 bilibili 上限重新解析一次原始标签，
                # 只在本次上传参数上应用 AcFun 上限。
                upload_tags = [uploader_tag] + cleaned_tags
                tags = upload_tags[:6]
                try:
                    persist_base = resolve_upload_tags(
                        runtime_config,
                        task.get('tags_generated') if task else None,
                        PLATFORM_BILIBILI,
                    )
                    persisted_tags = [uploader_tag] + [
                        safe_str(tag).strip()
                        for tag in persist_base
                        if safe_str(tag).strip() and safe_str(tag).strip().lower() != normalized_uploader
                    ]
                    update_task(
                        task_id,
                        tags_generated=json.dumps(persisted_tags[:BILIBILI_TAG_LIMIT], ensure_ascii=False),
                    )
                except Exception:
                    pass
        
        # AcFun配置（仅支持Cookies，推荐使用设置页二维码登录自动生成）
        acfun_cookies_path = resolve_cookie_file_path(
            path_value=self.config.get('ACFUN_COOKIES_PATH', 'cookies/ac_cookies.json'),
            default_relative_path='cookies/ac_cookies.json',
            service_name='AcFun',
            logger_obj=task_logger,
            allow_json_txt_fallback=True
        )
        
        # 获取封面处理模式
        cover_mode = self.config.get('COVER_PROCESSING_MODE', 'crop')
        
        # 检查必要参数
        missing_params = []
        if not video_path or not os.path.exists(video_path):
            missing_params.append("video_path (视频文件)")
        if not cover_path or not os.path.isfile(cover_path):
            missing_params.append("cover_path (封面文件)")
        if not title:
            missing_params.append("title (视频标题)")
        if not partition_id:
            missing_params.append("partition_id (分区ID)")
        
        if missing_params:
            error_msg = f"上传参数不完整，缺少: {', '.join(missing_params)}"
            task_logger.error(error_msg)
            update_task(
                task_id,
                status=TASK_STATES['FAILED'],
                error_message=error_msg
            )
            return
        
        # 检查登录凭据 - 必须提供有效的Cookie文件
        cookie_file_exists = os.path.exists(acfun_cookies_path)

        # 验证cookies文件（如果存在）
        cookies_valid = False
        if cookie_file_exists:
            is_valid, error_msg = validate_cookies(acfun_cookies_path, "AcFun")
            if is_valid:
                cookies_valid = True
                task_logger.info("AcFun Cookies文件验证通过")
            else:
                task_logger.warning(f"AcFun Cookies文件验证失败: {error_msg}")
                task_logger.error("AcFun Cookies无效，请在设置页重新扫码登录或上传可用Cookies")
                update_task(
                    task_id,
                    status=TASK_STATES['FAILED'],
                    error_message=f"AcFun Cookies无效，请重新登录: {error_msg}"
                )
                return

        if not cookies_valid:
            task_logger.error("AcFun登录信息不完整，需要有效的Cookie文件")
            update_task(
                task_id,
                status=TASK_STATES['FAILED'],
                error_message="AcFun登录信息不完整，需要有效的Cookie文件（可在设置页扫码登录）"
            )
            return
        
        # 创建上传器并执行上传
        try:
            uploader = AcfunUploader(cookie_file=acfun_cookies_path)
            
            cancel_event = get_task_cancel_event(task_id)
            # 上传视频
            success, result = uploader.upload_video(
                video_file_path=video_path,
                cover_file_path=cover_path,
                title=title,
                description=description,
                tags=tags,
                partition_id=partition_id,
                original_url=original_url,
                original_uploader=original_uploader,
                original_upload_date=original_upload_date,
                upload_append_repost_notice=bool(runtime_config.get('UPLOAD_APPEND_REPOST_NOTICE', True)),
                repost_notice_text=repost_notice_text,
                task_id=task_id,
                cover_mode=cover_mode,
                cancel_event=cancel_event
            )
            
            if success:
                task_logger.info(f"视频上传成功: {result}")
                update_task(
                    task_id,
                    status=TASK_STATES['COMPLETED'],
                    acfun_upload_response=json.dumps(result, ensure_ascii=False)
                )
                # 单平台 AcFun 才是最后一站。双平台时等 B 站成功后再删。
                self._reclaim_posted_files(
                    task_id,
                    task_logger,
                    last_platform=_get_task_upload_target(task) == UPLOAD_TARGET_ACFUN,
                )
            else:
                task_logger.error(f"视频上传失败: {result}")
                update_task(
                    task_id,
                    status=TASK_STATES['FAILED'],
                    error_message=f"上传失败: {result}"
                )
        except Exception as e:
            task_logger.error(f"上传过程中发生异常: {str(e)}")
            import traceback
            task_logger.error(traceback.format_exc())
            update_task(
                task_id,
                status=TASK_STATES['FAILED'],
                error_message=f"上传异常: {str(e)}"
            )

    def _defer_bilibili_cooldown(self, task_id, task_logger, account) -> bool:
        """冷却中的账号不发起任何 bilibili 请求。"""
        from modules.bilibili_accounts import cooldown_block_message
        message = cooldown_block_message(account, self.config)
        if not message:
            return False
        task_logger.warning(message)
        update_task(
            task_id,
            status=TASK_STATES['READY_FOR_UPLOAD'],
            error_message=message,
            upload_progress=None,
        )
        return True

    def _do_upload_to_bilibili(self, task_id, task_logger, subtitle_prepared=False):
        """实际执行上传到 Bilibili 的逻辑"""
        from modules.bilibili_uploader import BilibiliUploader

        task = get_task(task_id)
        if not task:
            task_logger.error("任务不存在")
            return

        update_task(task_id, status=TASK_STATES['UPLOADING'], upload_progress='0.0%')

        video_path = task.get('video_path_local', '') if task else ''
        cover_path = task.get('cover_path_local', '') if task else ''
        title = (task.get('video_title_translated', '') or task.get('video_title_original', '')) if task else ''
        description = (task.get('description_translated', '') or task.get('description_original', '')) if task else ''
        upload_target = _get_task_upload_target(task)
        effective_limits = _get_effective_metadata_limits(upload_target)
        partition_id = ''
        missing_translation_fields = _get_missing_required_translation_fields(task, self.config)
        if missing_translation_fields:
            task_logger.warning(
                f"当前上传路径存在缺失译文字段 {missing_translation_fields}，将回退原文继续上传"
            )

        if not video_path or not os.path.exists(video_path):
            task_logger.info("检测到视频文件缺失，开始下载视频文件...")
            youtube_url = task.get('youtube_url', '') if task else ''
            if not youtube_url:
                task_logger.error("无法获取YouTube URL，无法下载视频")
                update_task(
                    task_id,
                    status=TASK_STATES['FAILED'],
                    error_message="无法获取YouTube URL，无法下载视频",
                    upload_progress=None
                )
                return

            if self._download_video_file(task_id, youtube_url, task_logger) == 'held':
                return
            task = get_task(task_id)
            video_path = task.get('video_path_local', '') if task else ''
            cover_path = task.get('cover_path_local', '') if task else ''

        # 无论封面文件当前是否存在，都先统一做一次恢复/校验：
        # _recover_cover_path() 内部已包含 realpath + commonpath 的 downloads 目录边界检查，
        # 可阻止路径遍历或 symlink 指向 downloads 外部的情况；对于合法且已存在的路径，
        # 其 fast-return 会直接返回已校验过的路径，不改变原有功能。
        cover_path = self._recover_cover_path(task_id, cover_path, task_logger)

        if not subtitle_prepared:
            task = self._prepare_subtitle_for_upload(task_id, task_logger) or task
            video_path = task.get('video_path_local', '') if task else video_path

        update_task(task_id, status=TASK_STATES['UPLOADING'], upload_progress='0.0%')

        from modules.upload_templates import apply_metadata_templates, settings_for_task
        runtime_config = settings_for_task(self.config, task)

        # 解析标签（预设标签生效时：预设在前，其余标签紧随，并按 bilibili 上限截断）
        tags = resolve_upload_tags(
            runtime_config,
            task.get('tags_generated') if task else None,
            PLATFORM_BILIBILI,
            logger_obj=task_logger,
        )

        metadata_path = task.get('metadata_json_path_local', '') if task else ''
        original_url = task.get('youtube_url', '') if task else ''
        original_uploader = ''
        original_upload_date = ''

        if metadata_path and os.path.exists(metadata_path):
            try:
                with open(metadata_path, 'r', encoding='utf-8') as f:
                    metadata = json.load(f)
                    original_url = metadata.get('webpage_url', original_url)
                    original_uploader = metadata.get('uploader', '')
                    original_upload_date = metadata.get('upload_date', '')
            except Exception as e:
                task_logger.error(f"读取视频元数据失败: {str(e)}")

        # bilibili 转载页会单独展示 source，这里只保留说明文案和正文，避免 URL 重复出现。
        profile = runtime_config.get("_UPLOAD_PROFILE")
        repost_notice_text = ""
        try:
            from modules.bilibili_uploader import format_bilibili_description
            title, description, repost_notice_text = apply_metadata_templates(
                profile,
                title,
                description,
                original_url,
                original_uploader,
                original_upload_date,
            )
            description = format_bilibili_description(
                base_desc=description,
                original_url=original_url,
                original_uploader=original_uploader,
                original_upload_date=original_upload_date,
                append_repost_notice=bool(runtime_config.get('UPLOAD_APPEND_REPOST_NOTICE', True)),
                max_len=effective_limits['description_limit'],
                repost_notice_text=repost_notice_text,
            )
        except Exception as e:
            task_logger.warning(f"构建bilibili投稿简介失败，回退原简介: {e}")

        if self.config.get('YOUTUBE_UPLOADER_AS_FIRST_TAG', False):
            from modules.utils import safe_str
            uploader_tag = safe_str(original_uploader).strip()
            if uploader_tag:
                normalized_uploader = uploader_tag.lower()
                cleaned_tags = []
                for tag in tags:
                    tag_value = safe_str(tag).strip()
                    if not tag_value:
                        continue
                    if tag_value.lower() == normalized_uploader:
                        continue
                    cleaned_tags.append(tag_value)
                tags = [uploader_tag] + cleaned_tags
                if len(tags) > 12:
                    tags = tags[:12]
                try:
                    update_task(task_id, tags_generated=json.dumps(tags, ensure_ascii=False))
                except Exception:
                    pass

        from modules.bilibili_accounts import (
            request_scope_for_account,
            resolve_task_account,
        )
        account = resolve_task_account(task, self.config)
        try:
            request_scope = request_scope_for_account(account)
        except ValueError as exc:
            task_logger.error(str(exc))
            update_task(
                task_id,
                status=TASK_STATES['FAILED'],
                error_message=str(exc),
                upload_progress=None,
            )
            return
        route_label = "独立代理" if request_scope.get("proxy") else "直连"
        task_logger.info(
            "本次 bilibili 投稿使用账号「%s」，网络：%s",
            account.get("name") or account.get("id"),
            route_label,
        )
        # 没有入队账号戳的旧任务继续用配置里的 Cookie 路径，避免改到已经在跑的单账号安装。
        if str(task.get("bilibili_account_id") or "").strip():
            cookie_path_value = account.get("cookie_path") or self.config.get("BILIBILI_COOKIES_PATH", "cookies/bili_cookies.json")
        else:
            cookie_path_value = self.config.get("BILIBILI_COOKIES_PATH", "cookies/bili_cookies.json")
        bilibili_cookies_path = resolve_cookie_file_path(
            path_value=cookie_path_value,
            default_relative_path="cookies/bili_cookies.json",
            service_name="Bilibili",
            logger_obj=task_logger,
            allow_json_txt_fallback=False,
        )

        # 固定 bilibili 分区优先；否则使用任务分区。并校验分区合法性
        task_partition_id = _get_task_partition_id(
            task,
            UPLOAD_TARGET_BILIBILI,
            prefer_selected=True,
        ) if task else ''
        fixed_bili_pid = str(runtime_config.get('FIXED_PARTITION_ID_BILIBILI', '') or '').strip()
        partition_id = fixed_bili_pid or task_partition_id
        try:
            from modules.bilibili_zones import collect_valid_tids
            valid_tids = collect_valid_tids()
            if partition_id and str(partition_id) not in valid_tids:
                task_logger.warning(f"bilibili分区ID无效: {partition_id}")
                partition_id = ''
            if not partition_id and task_partition_id and str(task_partition_id) in valid_tids:
                partition_id = str(task_partition_id)
        except Exception as e:
            task_logger.warning(f"校验 bilibili 分区列表失败，继续使用当前分区ID: {e}")

        missing_params = []
        if not video_path or not os.path.exists(video_path):
            missing_params.append("video_path (视频文件)")
        if not cover_path or not os.path.isfile(cover_path):
            missing_params.append("cover_path (封面文件)")
        if not title:
            missing_params.append("title (视频标题)")
        if not partition_id:
            missing_params.append("partition_id (分区ID)")
        if not bilibili_cookies_path:
            missing_params.append("BILIBILI_COOKIES_PATH")

        if missing_params:
            error_msg = f"上传参数不完整，缺少: {', '.join(missing_params)}"
            task_logger.error(error_msg)
            update_task(
                task_id,
                status=TASK_STATES['FAILED'],
                error_message=error_msg,
                upload_progress=None
            )
            return

        cookie_file_exists = os.path.exists(bilibili_cookies_path)
        if not cookie_file_exists:
            task_logger.error(f"Bilibili Cookies文件不存在: {bilibili_cookies_path}")
            update_task(
                task_id,
                status=TASK_STATES['FAILED'],
                error_message=f"Bilibili Cookies文件不存在: {bilibili_cookies_path}",
                upload_progress=None
            )
            return

        is_valid, error_msg = validate_cookies(bilibili_cookies_path, "Bilibili")
        if not is_valid:
            task_logger.error(f"Bilibili Cookies文件验证失败: {error_msg}")
            update_task(
                task_id,
                status=TASK_STATES['FAILED'],
                error_message=f"Bilibili Cookies无效: {error_msg}",
                upload_progress=None
            )
            return

        try:
            uploader = BilibiliUploader(
                cookie_file=bilibili_cookies_path,
                submit_api=self.config.get('BILIBILI_SUBMIT_API', 'auto'),
                upload_line=self.config.get('BILIBILI_UPLOAD_LINE', 'auto'),
            )
            uploader.request_scope = request_scope
            uploader.account_id = str(account.get("id") or "") or None
            from modules.bilibili_accounts import account_display_name
            uploader.account_name = account_display_name(account)
            last_progress_text = '0.0%'

            def _normalize_progress_text(progress_text):
                text = str(progress_text or '').strip()
                if not text:
                    return '上传中...'
                match = re.search(r'(\d+(?:\.\d+)?)\s*%', text)
                if match:
                    try:
                        percent = float(match.group(1))
                        percent = max(0.0, min(100.0, percent))
                        return f"{percent:.1f}%"
                    except Exception:
                        return '上传中...'
                try:
                    numeric = float(text)
                    if 0.0 <= numeric <= 1.0:
                        numeric *= 100.0
                    numeric = max(0.0, min(100.0, numeric))
                    return f"{numeric:.1f}%"
                except Exception:
                    return '上传中...'

            def _on_progress(progress_text):
                nonlocal last_progress_text
                normalized_text = _normalize_progress_text(progress_text)
                if normalized_text == last_progress_text:
                    return
                last_progress_text = normalized_text
                update_task(task_id, upload_progress=normalized_text, silent=True)

            success, result = uploader.upload_video(
                video_file_path=video_path,
                cover_file_path=cover_path,
                title=title,
                description=description,
                tags=tags,
                partition_id=partition_id,
                youtube_url=original_url,
                task_id=task_id,
                progress_callback=_on_progress,
                title_limit=effective_limits['title_limit'],
                description_limit=effective_limits['description_limit'],
            )
            if success:
                task_logger.info(f"bilibili上传成功: {result}")
                update_task(
                    task_id,
                    status=TASK_STATES['COMPLETED'],
                    bilibili_upload_response=json.dumps(result, ensure_ascii=False),
                    upload_progress=None
                )
                # B 站是单平台的最后一站，也是双平台流程里的最后一站。
                self._reclaim_posted_files(
                    task_id,
                    task_logger,
                    last_platform=_get_task_upload_target(task) != UPLOAD_TARGET_ACFUN,
                )
            else:
                task_logger.error(f"bilibili上传失败: {result}")
                update_task(
                    task_id,
                    status=TASK_STATES['FAILED'],
                    error_message=f"上传失败: {result}",
                    upload_progress=None
                )
        except Exception as e:
            task_logger.error(f"bilibili上传过程中发生异常: {str(e)}")
            import traceback
            task_logger.error(traceback.format_exc())
            update_task(
                task_id,
                status=TASK_STATES['FAILED'],
                error_message=f"上传异常: {str(e)}",
                upload_progress=None
            )

    def _reclaim_posted_files(self, task_id, task_logger, *, last_platform: bool):
        """最后一次投稿成功后，按清理开关或磁盘阈值删除该任务的本地文件。

        只删除这一条任务目录。磁盘回到阈值以上时解除 disk-low，手动暂停和熔断不动。
        """
        from modules.pipeline.gates import (
            disk_is_low,
            disk_threshold_bytes,
            maybe_release_disk_hold,
        )

        threshold = disk_threshold_bytes(self.config)
        root = os.fspath(DOWNLOADS_DIR)
        low = False
        try:
            low = disk_is_low(root, threshold)
        except Exception as exc:
            task_logger.debug(f"检查磁盘阈值失败: {exc}")
        should_delete = bool(last_platform) and (
            bool(self.config.get('DELETE_DOWNLOAD_FILES_AFTER_UPLOAD', False)) or low
        )
        if should_delete:
            reason = "上传后清理已开启" if self.config.get('DELETE_DOWNLOAD_FILES_AFTER_UPLOAD', False) else "磁盘低于阈值"
            try:
                if delete_task_files(task_id):
                    task_logger.info(f"{reason}，已删除本次投稿的本地文件: {task_id}")
                else:
                    task_logger.warning(f"本次投稿的本地文件删除失败，请手动清理: {task_id}")
            except Exception as exc:
                task_logger.warning(f"上传后删除本地文件失败（不影响已完成的投稿）: {exc}")
        try:
            if maybe_release_disk_hold(root, threshold):
                task_logger.info("磁盘空间已回到阈值以上，自动恢复下载")
                kick_pending_downloads_if_running()
        except Exception as exc:
            task_logger.debug(f"解除磁盘暂停失败: {exc}")
