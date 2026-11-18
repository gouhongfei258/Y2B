#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""标题、简介和字幕翻译。

质检、烧录和 checkpoint 仍由 TaskProcessor 上的其他方法完成。
"""

import os

from modules.pipeline.facade import Live

from modules.pipeline.cancel import TaskCancelledError

DOWNLOADS_DIR = Live('DOWNLOADS_DIR')
METADATA_TRANSLATION_ERROR_CATEGORY = Live('METADATA_TRANSLATION_ERROR_CATEGORY')
SUBTITLE_QC_DISABLED = Live('SUBTITLE_QC_DISABLED')
TASK_STATES = Live('TASK_STATES')
_as_bool = Live('_as_bool')
_block_reason_to_warning = Live('_block_reason_to_warning')
_build_missing_translation_review_message = Live('_build_missing_translation_review_message')
_convert_vtt_text_to_srt_text = Live('_convert_vtt_text_to_srt_text')
_get_effective_metadata_limits = Live('_get_effective_metadata_limits')
_get_missing_required_translation_fields = Live('_get_missing_required_translation_fields')
_get_task_upload_target = Live('_get_task_upload_target')
_is_asr_enabled = Live('_is_asr_enabled')
_is_asr_subtitle_artifact = Live('_is_asr_subtitle_artifact')
_qc_result_allows_embed = Live('_qc_result_allows_embed')
_subtitle_block_reasons = Live('_subtitle_block_reasons')
_subtitle_embed_allowed = Live('_subtitle_embed_allowed')
get_task = Live('get_task')
get_task_cancel_event = Live('get_task_cancel_event')
is_task_cancelled = Live('is_task_cancelled')
update_task = Live('update_task')

_SHARED_CONSTANT_NAMES = ('METADATA_TRANSLATION_ERROR_CATEGORY', 'SUBTITLE_QC_DISABLED', 'TASK_STATES')

class SubtitleTranslationMixin:

    def _translate_content(self, task_id, task_logger):
        """翻译视频标题和描述"""
        from modules.ai_enhancer import translate_video_metadata
        
        task = get_task(task_id)
        if not task:
            task_logger.error("任务不存在")
            return
        
        task_logger.info("开始翻译视频标题和描述")
        update_task(task_id, status=TASK_STATES['TRANSLATING'])
        upload_target = _get_task_upload_target(task)
        effective_limits = _get_effective_metadata_limits(upload_target)
        
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
            # 可选：允许用户配置固定分区ID，确保一次命中
            'FIXED_PARTITION_ID': self.config.get('FIXED_PARTITION_ID', ''),
        }
        # 传递 Prompt 中心配置（元数据翻译）
        for prompt_key in (
            'METADATA_TRANSLATE_MODE', 'METADATA_TRANSLATE_TEXT',
            'METADATA_DESC_RETRY_MODE', 'METADATA_DESC_RETRY_TEXT',
        ):
            if prompt_key in self.config:
                openai_config[prompt_key] = self.config[prompt_key]
        
        translate_title = bool(self.config.get('TRANSLATE_TITLE', True) and task.get('video_title_original'))
        translate_description = bool(self.config.get('TRANSLATE_DESCRIPTION', True) and task.get('description_original'))

        if not translate_title and not translate_description:
            task_logger.info("标题和描述翻译均已禁用或缺少原文，跳过")
            return True

        translated = translate_video_metadata(
            task.get('video_title_original', ''),
            task.get('description_original', ''),
            target_language="zh-CN",
            openai_config=openai_config,
            task_id=task_id,
            translate_title=translate_title,
            translate_description=translate_description,
            title_limit=effective_limits['title_limit'],
            description_limit=effective_limits['description_limit'],
        )

        updates = {}
        requested_fields = set(translated.get('requested_fields') or [])
        if translate_title and 'title' in requested_fields:
            updates['video_title_translated'] = translated.get('title', '')
        if translate_description and 'description' in requested_fields:
            updates['description_translated'] = translated.get('description', '')

        if translated.get('success'):
            if updates:
                updates['error_message'] = None
                updates['error_category'] = None
                update_task(task_id, **updates)
            task_logger.info("翻译完成")
            return True

        review_message = translated.get('error_message') or _build_missing_translation_review_message(requested_fields)
        updates.update({
            'status': TASK_STATES['AWAITING_REVIEW'],
            'error_message': review_message,
            'error_category': METADATA_TRANSLATION_ERROR_CATEGORY,
        })
        update_task(task_id, **updates)
        task_logger.warning(review_message)
        return False

    def _ensure_required_translations_ready(self, task_id, task, task_logger, allow_missing_translations=False):
        missing_fields = _get_missing_required_translation_fields(task, self.config)
        if not missing_fields:
            return True

        missing_message = _build_missing_translation_review_message(missing_fields)
        if allow_missing_translations:
            task_logger.warning(
                f"{missing_message} 当前为强制上传路径，将继续回退原文执行上传。"
            )
            return True

        task_logger.warning(missing_message)
        update_task(
            task_id,
            status=TASK_STATES['AWAITING_REVIEW'],
            error_message=missing_message,
            error_category=METADATA_TRANSLATION_ERROR_CATEGORY,
            upload_progress=None,
        )
        return False
    
    def _translate_subtitle(self, task_id, task_logger, embed_in_video_override=None):
        """翻译字幕文件"""
        task = get_task(task_id)
        if not task:
            task_logger.error("任务不存在")
            return False

        translation_enabled = _as_bool(self.config.get('SUBTITLE_TRANSLATION_ENABLED', False))
        config_embed_enabled = _as_bool(self.config.get('SUBTITLE_EMBED_IN_VIDEO', True))
        if embed_in_video_override is None:
            should_embed_subtitle = config_embed_enabled
        else:
            should_embed_subtitle = bool(embed_in_video_override)

        # 烧录门控状态。质检失败不再直接短路整个字幕流程，否则任务重跑会被
        # 旧标记永久锁死、永远拿不到新字幕；改为在每次烧录前统一裁决。
        embed_quality_state = str(task.get('subtitle_quality_state') or '').strip().lower() or 'ok'
        qc_failed = task.get('subtitle_qc_failed') == 1
        # 质检结论的归因：「qc_unavailable」表示「没跑成」，门控对它只按逃生口裁决，
        # 与明确的失败结论分开（见 _subtitle_block_reasons）。
        qc_reason = task.get('subtitle_qc_reason')
        qc_cleared = False
        # ASR_FAILURE_BLOCKS_EMBED=False 时置真：本轮 ASR 产物按旧行为放行烧录。
        escape_hatch_active = False
        # 首个烧录拦截归因。质检闸门写入的 qc_unavailable / subtitle_qc_rejected
        # 必须比外层兜底的 asr_failed_block_embed 更精确，不能被覆盖。
        block_reason = None

        def _embed_guard(subtitle_file, *, asr_artifact=True):
            """烧录前统一门控。

            ``subtitle_quality_state`` 描述的是**ASR 产物**的来源可靠度，
            因此只看 ASR 生成的字幕；已有外部字幕（如平台自带 SRT）不受它约束，
            否则一次历史 ASR 失败会永久阻断正常字幕的烧录。
            ``subtitle_qc_failed`` 始终生效 —— 它针对的是具体字幕文件的质检结论。

            被拒时记录**首个**原因，供外层写入精确的 ``subtitle_warning_message``。
            """
            nonlocal block_reason
            state = embed_quality_state if asr_artifact else 'ok'
            allowed = _subtitle_embed_allowed(
                self.config,
                state,
                qc_failed,
                qc_cleared=qc_cleared,
                qc_reason=qc_reason,
                task_logger=task_logger,
            )
            if not allowed and block_reason is None:
                reasons = _subtitle_block_reasons(
                    self.config, state, qc_failed, qc_cleared=qc_cleared, qc_reason=qc_reason
                )
                if reasons:
                    block_reason = reasons[0]
            return allowed

        def _ensure_asr_qc(subtitle_file):
            """确保 ASR 产物在烧录前通过质检。

            此前质检只在「本次 ASR 现场生成」的分支里执行；已有 ``asr_*.srt``
            被复用时直接烧录，完全绕过质检 —— 这是「字幕质量差却照样烧录」的
            另一条真实路径。此处按文件来源补齐：ASR 产物必须过质检，
            外部字幕（平台自带/人工提供）保持原行为，不额外增加 AI 调用。
            """
            nonlocal qc_cleared, qc_failed, block_reason
            if qc_cleared:
                return True
            allowed, cleared, reason = self._ensure_asr_subtitle_qc(
                task_id, subtitle_file, task_logger, embed_quality_state
            )
            if cleared:
                qc_cleared = True
                qc_failed = False
            if not allowed and block_reason is None:
                block_reason = reason or 'subtitle_qc_rejected'
            return allowed

        def _quarantine_if_asr_artifact(subtitle_file):
            """被门控拦截的 ASR 产物移出扫描范围（外部字幕保持原地不动）。

            判定已下沉到 ``_quarantine_rejected_subtitle`` 内部，这里保留一层
            显式意图声明，并让「不满足条件直接跳过」在调用点可读。
            """
            if _is_asr_subtitle_artifact(task_id, subtitle_file):
                self._quarantine_rejected_subtitle(subtitle_file, task_logger)

        def _block_warning(fallback='asr_failed_block_embed'):
            """保留首个拦截归因，不被外层兜底文案覆盖。"""
            return _block_reason_to_warning(block_reason) if block_reason else fallback

        if translation_enabled:
            task_logger.info("开始字幕翻译")
        elif should_embed_subtitle:
            task_logger.info("开始字幕烧录")
        else:
            task_logger.info("开始字幕处理")
        update_task(task_id, status=TASK_STATES['TRANSLATING_SUBTITLE'])
        
        try:
            task_upload_target = _get_task_upload_target(task)
            asr_generated = False
            # 显式初始化：读取 ASR 质量结局的日志路径引用它，避免「未启用 ASR
            # 且已有外部字幕」时出现未绑定变量。
            recognizer = None
            # 查找字幕文件（大小写无关）
            task_dir = os.path.join(DOWNLOADS_DIR, task_id)
            subtitle_files = []
            try:
                for name in os.listdir(task_dir):
                    if not isinstance(name, str):
                        continue
                    lower = name.lower()
                    if lower.endswith('.srt'):
                        subtitle_files.append(os.path.join(task_dir, name))
                    elif lower.endswith('.vtt'):
                        # 自动将 VTT 转换为 SRT
                        vtt_path = os.path.join(task_dir, name)
                        srt_path = self._convert_vtt_to_srt(vtt_path, task_logger)
                        if srt_path and os.path.exists(srt_path):
                            subtitle_files.append(srt_path)
                            task_logger.info(f"已将 VTT 转换为 SRT: {name} -> {os.path.basename(srt_path)}")
                        else:
                            # 转换失败，仍保留 VTT 以防万一
                            subtitle_files.append(vtt_path)
            except Exception:
                pass
            
            if not subtitle_files:
                task_logger.info("未找到字幕文件，尝试语音识别生成字幕（如已启用）")
                # 若启用了语音识别，使用Whisper兼容API从视频生成字幕
                if _is_asr_enabled(self.config):
                    out_path = None
                    try:
                        from modules.speech_recognition import create_speech_recognizer_from_config
                        recognizer = create_speech_recognizer_from_config(self.config, task_id)
                    except Exception as e:
                        task_logger.error(f"创建语音识别器失败: {e}")
                        recognizer = None
                    if recognizer and task is not None:
                        video_path = task.get('video_path_local')
                        if video_path and os.path.exists(video_path):
                            # 确保在任何情况下都有默认的 prev_status，避免未绑定警告
                            prev_status = TASK_STATES['TRANSLATING_SUBTITLE']
                            try:
                                # 显示ASR状态
                                _t = get_task(task_id)
                                prev_status = _t['status'] if _t else prev_status
                                update_task(task_id, status=TASK_STATES['ASR_TRANSCRIBING'])
                                # 输出字幕路径（强制使用 SRT）
                                asr_ext = '.srt'
                                asr_subtitle_path = os.path.join(task_dir, f"asr_{task_id}{asr_ext}")
                                out_path = None
                                # 新一轮 ASR 前复位上一轮的质检/质量结局标记，
                                # 否则旧标记会让任务永久跳过字幕生成。
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
                                qc_failed = False
                                qc_cleared = False
                                embed_quality_state = 'ok'
                                out_path = recognizer.transcribe_video_to_subtitles(video_path, asr_subtitle_path)
                                # 恢复到字幕翻译状态
                                update_task(task_id, status=prev_status)
                            except Exception as e:
                                # 捕获语音识别过程中的所有异常
                                task_logger.error(f"语音识别过程中发生错误: {e}")
                                import traceback
                                task_logger.error(f"详细错误信息:\n{traceback.format_exc()}")
                                # 恢复任务状态
                                update_task(task_id, status=prev_status if 'prev_status' in locals() else TASK_STATES['TRANSLATING_SUBTITLE'])
                                out_path = None
                        if out_path and os.path.exists(out_path):
                            subtitle_files = [out_path]
                            asr_generated = True
                            embed_quality_state = (
                                str(getattr(recognizer, 'last_quality_state', '') or 'ok').strip().lower() or 'ok'
                            )
                            task_logger.info(f"语音识别生成字幕成功: {os.path.basename(out_path)}")
                            update_task(
                                task_id,
                                asr_warning_message=(getattr(recognizer, 'last_warning_message', '') or None),
                                subtitle_quality_state=embed_quality_state,
                            )
                        else:
                            update_task(
                                task_id,
                                asr_warning_message=(getattr(recognizer, 'last_warning_message', '') or None),
                                subtitle_quality_state=(
                                    str(getattr(recognizer, 'last_quality_state', '') or 'failed').strip().lower()
                                    or 'failed'
                                ),
                            )
                            if getattr(recognizer, 'last_error_message', ''):
                                _t = get_task(task_id)
                                prev_error = _t.get('error_message') if _t else None
                                merged_error = (
                                    f"{prev_error}\n{recognizer.last_error_message}" if prev_error else recognizer.last_error_message
                                )
                                update_task(task_id, error_message=merged_error)
                            self._mark_subtitle_issue(task_id, 'asr_no_subtitle')
                            task_logger.warning("语音识别未能生成字幕，跳过字幕流程")
                            return True
                    else:
                        task_logger.warning("语音识别未启用或视频文件缺失，跳过字幕流程")
                        return True
                else:
                    task_logger.info("未启用语音识别，跳过字幕流程")
                    return True

            # ASR/VAD 质量结局与质检门控：
            # failed  → 直接拒绝烧录（来源不可信）；
            # degraded→ 必须通过严格质检（含时间轴维度）才允许烧录；
            # ok      → 走常规质检。
            if asr_generated:
                asr_subtitle_path = subtitle_files[0] if subtitle_files else None
                detected_lang = self._detect_subtitle_language(asr_subtitle_path) if asr_subtitle_path else None

                if embed_quality_state == 'failed':
                    block_embed = _as_bool(self.config.get('ASR_FAILURE_BLOCKS_EMBED', True))
                    log_fn = task_logger.error if block_embed else task_logger.warning
                    log_fn(
                        "ASR/VAD 质量结局为 failed（%s），%s：继续上传%s",
                        '/'.join(str(item) for item in (getattr(recognizer, 'last_degraded_reasons', []) or []))
                        or 'unknown',
                        '拒绝烧录字幕' if block_embed else '按 ASR_FAILURE_BLOCKS_EMBED=False 放行烧录',
                        '原视频' if block_embed else '本次产物',
                    )
                    if block_embed:
                        self._quarantine_rejected_subtitle(asr_subtitle_path, task_logger)
                        update_task(
                            task_id,
                            subtitle_language_detected=detected_lang,
                            subtitle_warning_message='asr_failed_block_embed',
                            upload_progress=None,
                        )
                        return True
                    # 逃生口：DB 中仍保留 failed 结局供展示，但本轮 ASR 来源
                    # 结局不再拦烧录（这就是 ASR_FAILURE_BLOCKS_EMBED=False 的
                    # 旧行为语义）。注意这里**不改写** embed_quality_state：
                    # 质检的严格度与归因仍需要真实的结局，放行交由
                    # ``_subtitle_embed_allowed`` 内部的 asr_blocks 决定。
                    escape_hatch_active = True

                if asr_subtitle_path and os.path.exists(asr_subtitle_path):
                    strict_qc = embed_quality_state == 'degraded'
                    qc_result = self._run_subtitle_qc(
                        task_id, asr_subtitle_path, task_logger, strict=strict_qc
                    )
                    if qc_result is SUBTITLE_QC_DISABLED:
                        # 用户主动关闭质检：没有任何质检结论可依赖，按配置放行，
                        # 同时清掉内存中的历史 qc_failed —— 否则「关掉质检」会比
                        # 开着更严格。``qc_cleared`` 在此表示「本轮质检门控已裁决
                        # 完毕」，不等于「质检结论为通过」；degraded 的严格质检
                        # 要求同样随质检开关关闭（见 ``_subtitle_block_reasons``）。
                        task_logger.info("字幕质检已关闭，跳过本次烧录前质检裁决")
                        qc_cleared = True
                        qc_failed = False
                    else:
                        # 逃生口（ASR_FAILURE_BLOCKS_EMBED=False）生效时，「质检没跑成」
                        # 也不应阻断烧录，但明确的质检失败结论（False）不受它影响。
                        # 判定统一由 ``_qc_result_allows_embed`` 给出，避免各调用点漂移。
                        if not _qc_result_allows_embed(qc_result, self.config):
                            reason_text = 'qc_unavailable' if qc_result is None else 'subtitle_qc_rejected'
                            task_logger.warning(
                                "转录字幕质检未通过（%s, strict=%s, escape_hatch=%s）：跳过字幕翻译/烧录，"
                                "保留字幕文件并继续上传原视频",
                                reason_text,
                                strict_qc,
                                escape_hatch_active,
                            )
                            self._quarantine_rejected_subtitle(asr_subtitle_path, task_logger)
                            update_task(
                                task_id,
                                subtitle_language_detected=detected_lang,
                                subtitle_warning_message=reason_text,
                                upload_progress=None,
                            )
                            return True
                        qc_cleared = True
                        qc_failed = False
            
            # 优化选择策略：若有中文字幕则直接烧录；否则优先选英文字幕进行翻译
            detected_list = []
            for f in subtitle_files:
                lang = self._detect_subtitle_language(f)
                detected_list.append((f, lang))
            zh_candidates = [f for f, lang in detected_list if str(lang).lower().startswith('zh')]
            en_candidates = [f for f, lang in detected_list if str(lang).lower().startswith('en')]
            
            if zh_candidates:
                # 直接使用中文字幕，不进行翻译
                subtitle_file = zh_candidates[0]
                subtitle_lang = 'zh'
                task_logger.info(f"检测到中文字幕，直接烧录，无需翻译: {os.path.basename(subtitle_file)}")

                if should_embed_subtitle and _embed_guard(subtitle_file, asr_artifact=asr_generated) and _ensure_asr_qc(subtitle_file):
                    embedded_video_path = self._embed_subtitle_in_video(
                        task_id, task['video_path_local'], subtitle_file, task_logger
                    )
                    if embedded_video_path:
                        update_task(
                            task_id,
                            video_path_local=embedded_video_path,
                            subtitle_path_original=subtitle_file,
                            subtitle_path_translated=None,
                            subtitle_language_detected=subtitle_lang,
                            subtitle_warning_message=None,
                        )
                        task_logger.info("中文字幕烧录完成")
                        return True
                    else:
                        task_logger.warning("中文字幕烧录失败，保留原视频")
                        update_task(
                            task_id,
                            subtitle_path_original=subtitle_file,
                            subtitle_path_translated=None,
                            subtitle_language_detected=subtitle_lang,
                            subtitle_warning_message='subtitle_embed_failed',
                        )
                        return False
                elif should_embed_subtitle:
                    task_logger.warning("中文字幕被烧录门控拦截，保留原视频并继续上传")
                    _quarantine_if_asr_artifact(subtitle_file)
                    update_task(
                        task_id,
                        subtitle_path_original=subtitle_file,
                        subtitle_path_translated=None,
                        subtitle_language_detected=subtitle_lang,
                        subtitle_warning_message=_block_warning(),
                    )
                    return True
                else:
                    # 不嵌入字幕，只保存信息
                    update_task(
                        task_id,
                        subtitle_path_original=subtitle_file,
                        subtitle_path_translated=None,
                        subtitle_language_detected=subtitle_lang
                    )
                    task_logger.info("已检测到中文字幕，但未开启烧录，跳过翻译")
                    return True
            
            if not translation_enabled:
                # 只烧录模式：不创建翻译器，直接使用最合适的已有/ASR字幕。
                subtitle_file = zh_candidates[0] if zh_candidates else (en_candidates[0] if en_candidates else subtitle_files[0])
                subtitle_lang = self._detect_subtitle_language(subtitle_file)
                task_logger.info(f"字幕翻译未启用，直接使用原字幕进行烧录: {os.path.basename(subtitle_file)}")

                if should_embed_subtitle and _embed_guard(subtitle_file, asr_artifact=asr_generated) and _ensure_asr_qc(subtitle_file):
                    embedded_video_path = self._embed_subtitle_in_video(
                        task_id, task['video_path_local'], subtitle_file, task_logger
                    )
                    if embedded_video_path:
                        update_task(
                            task_id,
                            video_path_local=embedded_video_path,
                            subtitle_path_original=subtitle_file,
                            subtitle_path_translated=None,
                            subtitle_language_detected=subtitle_lang,
                            subtitle_warning_message=None,
                        )
                        task_logger.info("原字幕烧录完成")
                        return True

                    task_logger.warning("原字幕烧录失败，保留原视频")
                    update_task(
                        task_id,
                        subtitle_path_original=subtitle_file,
                        subtitle_path_translated=None,
                        subtitle_language_detected=subtitle_lang,
                        subtitle_warning_message='subtitle_embed_failed',
                    )
                    return False
                elif should_embed_subtitle:
                    task_logger.warning("原字幕被烧录门控拦截，保留原视频并继续上传")
                    _quarantine_if_asr_artifact(subtitle_file)
                    update_task(
                        task_id,
                        subtitle_path_original=subtitle_file,
                        subtitle_path_translated=None,
                        subtitle_language_detected=subtitle_lang,
                        subtitle_warning_message=_block_warning(),
                    )
                    return True

                update_task(
                    task_id,
                    subtitle_path_original=subtitle_file,
                    subtitle_path_translated=None,
                    subtitle_language_detected=subtitle_lang,
                )
                task_logger.info("字幕翻译和烧录均未启用，仅记录字幕文件")
                return True
            
            # 没有中文：优先选择英文，否则退回第一个文件
            subtitle_file = en_candidates[0] if en_candidates else subtitle_files[0]
            task_logger.info(f"找到字幕文件: {os.path.basename(subtitle_file)}")
            subtitle_lang = self._detect_subtitle_language(subtitle_file)
            task_logger.info(f"检测到字幕语言: {subtitle_lang}")
            if en_candidates and subtitle_file in en_candidates:
                task_logger.info("优先使用英文字幕进行翻译")

            # 创建翻译器（此时需要翻译为中文）
            from modules.subtitle_translator import create_translator_from_config
            translator = create_translator_from_config(self.config, task_id)
            if not translator:
                task_logger.error("无法创建字幕翻译器，请检查API配置")
                update_task(
                    task_id,
                    subtitle_path_original=subtitle_file,
                    subtitle_path_translated=None,
                    subtitle_language_detected=subtitle_lang,
                    upload_progress=None,
                    silent=True,
                )
                return False
            
            # 生成翻译后的文件路径
            # 强制使用 .srt 格式
            translated_subtitle_path = os.path.join(
                task_dir, 
                f"translated_{task_id}.srt"
            )
            
            # 定义进度回调函数
            def progress_callback(progress, current, total):
                if is_task_cancelled(task_id):
                    raise TaskCancelledError("任务已取消")
                # 不再将每次字幕翻译进度记录为 INFO 到文件，减少日志噪声
                task_logger.debug(f"字幕翻译进度: {progress:.1f}% ({current}/{total})")
                # 更新任务进度显示到网页
                update_task(task_id, upload_progress=f"{progress:.1f}%", silent=True)
            
            # 执行翻译
            cancel_event = get_task_cancel_event(task_id)
            success = translator.translate_file(
                subtitle_file, 
                translated_subtitle_path,
                progress_callback=progress_callback,
                cancel_event=cancel_event
            )
            
            if success:
                task_logger.info("字幕翻译完成")
                # 清除翻译进度显示
                update_task(task_id, upload_progress=None, silent=True)
                
                # 如果配置了将字幕嵌入视频
                # 双闸门：门控看译文产物，质检看**源**字幕 —— 译文由源字幕翻译
                # 而来，源头不合格就没有烧录价值；此前翻译分支只有单闸门，
                # 复用已有 asr_*.srt 时会整条绕过质检。
                if (
                    should_embed_subtitle
                    and _embed_guard(translated_subtitle_path, asr_artifact=asr_generated)
                    and _ensure_asr_qc(subtitle_file)
                ):
                    embedded_video_path = self._embed_subtitle_in_video(
                        task_id, task['video_path_local'], 
                        translated_subtitle_path, task_logger
                    )
                    if embedded_video_path:
                        # 更新视频路径为嵌入字幕的版本
                        update_task(
                            task_id,
                            video_path_local=embedded_video_path,
                            subtitle_path_original=subtitle_file,
                            subtitle_path_translated=translated_subtitle_path,
                            subtitle_language_detected=subtitle_lang,
                            subtitle_warning_message=None,
                        )
                    else:
                        task_logger.warning("字幕嵌入失败，保留原视频和字幕文件")
                        update_task(
                            task_id,
                            subtitle_path_original=subtitle_file,
                            subtitle_path_translated=translated_subtitle_path,
                            subtitle_language_detected=subtitle_lang,
                            subtitle_warning_message='subtitle_embed_failed',
                        )
                elif should_embed_subtitle:
                    task_logger.warning("翻译字幕被烧录门控拦截，保留原视频和字幕文件")
                    # 拦截的若是 ASR 产物，必须移出扫描范围，否则下轮重跑会复用
                    # 这条不合格字幕、永远拿不到新字幕。
                    _quarantine_if_asr_artifact(subtitle_file)
                    update_task(
                        task_id,
                        subtitle_path_original=subtitle_file,
                        subtitle_path_translated=translated_subtitle_path,
                        subtitle_language_detected=subtitle_lang,
                        subtitle_warning_message=_block_warning(),
                    )
                else:
                    # 不嵌入字幕，只保存字幕文件信息
                    update_task(
                        task_id,
                        subtitle_path_original=subtitle_file,
                        subtitle_path_translated=translated_subtitle_path,
                        subtitle_language_detected=subtitle_lang
                    )
                
                task_logger.info("字幕翻译处理完成")
                return True
            else:
                task_logger.warning("字幕翻译失败，按策略跳过字幕产物并继续后续上传")
                # 清除进度显示
                update_task(
                    task_id,
                    subtitle_path_original=subtitle_file,
                    subtitle_path_translated=None,
                    subtitle_language_detected=subtitle_lang,
                    upload_progress=None,
                    silent=True,
                )
                return False
                
        except Exception as e:
            task_logger.error(f"字幕翻译过程中发生错误: {str(e)}")
            import traceback
            task_logger.error(traceback.format_exc())
            # 清除进度显示
            update_task(
                task_id,
                subtitle_path_translated=None,
                upload_progress=None,
                silent=True,
            )
            return False

    def _convert_vtt_to_srt(self, vtt_path, task_logger):
        """将VTT字幕文件转换为SRT格式（FFmpeg对SRT支持更好）"""
        try:
            import os
            
            # 生成SRT文件路径
            base_path, _ = os.path.splitext(vtt_path)
            srt_path = f"{base_path}.srt"
            
            with open(vtt_path, 'r', encoding='utf-8') as vtt_file:
                vtt_content = vtt_file.read()
            srt_content = _convert_vtt_text_to_srt_text(vtt_content)
            if not srt_content:
                task_logger.error(f"VTT未解析出有效字幕: {vtt_path}")
                return None
            
            # 写入SRT文件
            with open(srt_path, 'w', encoding='utf-8') as srt_file:
                srt_file.write(srt_content)
            
            task_logger.info(f"VTT转换为SRT成功: {srt_path}")
            return srt_path
            
        except Exception as e:
            task_logger.error(f"VTT转SRT转换失败: {str(e)}")
            return None
