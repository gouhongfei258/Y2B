"""Task list, manual review, and download controls.

Executed by create_app() in the app package namespace. Endpoint names stay
unprefixed so existing url_for() names and request.endpoint checks keep working.
"""
from app.blueprints.blueprint import Blueprint

bp = Blueprint('tasks', 'app.blueprints.tasks')

@bp.route('/tasks')
@login_required
def tasks():
    """任务列表页面"""
    logger.info("访问任务列表页面")
    
    # 获取分页参数
    page = request.args.get('page', 1, type=int)
    per_page = 20  # 每页显示20条记录
    
    # 获取分页数据
    pagination_data = get_tasks_paginated(page=page, per_page=per_page)
    config = load_config()
    try:
        from y2b.config.upload_templates import list_templates
        upload_templates = list_templates()
    except Exception:
        upload_templates = []
    
    return render_template('tasks.html', 
                         tasks=pagination_data['tasks'],
                         pagination=pagination_data,
                         config=config,
                         upload_templates=upload_templates,
                         runtime_gates=_runtime_gate_status())

@bp.route('/downloads/pause', methods=['POST'])
@login_required
def pause_downloads_route():
    from y2b.pipeline.gates import pause_downloads
    pause_downloads()
    return _finish_gate_action(
        '已暂停下载。正在编码或上传的任务会继续，手动暂停不会自动恢复。',
        'info',
    )

@bp.route('/downloads/resume', methods=['POST'])
@login_required
def resume_downloads_route():
    from y2b.pipeline.gates import public_status, resume_downloads
    from y2b.pipeline.task_manager import kick_pending_downloads_if_running
    resume_downloads()
    status = public_status()
    if status.get('downloads_blocked'):
        return _finish_gate_action(
            '已取消手动暂停。下载仍被其他闸门拦住：' + (status.get('download_message') or ''),
            'warning',
        )
    kick_pending_downloads_if_running()
    return _finish_gate_action('已恢复下载。', 'success')

@bp.route('/downloads/bot/resume', methods=['POST'])
@login_required
def bot_circuit_resume_route():
    from y2b.pipeline.gates import public_status, skip_bot_wait
    from y2b.pipeline.task_manager import kick_pending_downloads_if_running
    skip_bot_wait()
    status = public_status()
    if status.get('downloads_blocked'):
        return _finish_gate_action(
            '已跳过本次熔断等待。下载仍被其他闸门拦住：' + (status.get('download_message') or ''),
            'warning',
        )
    kick_pending_downloads_if_running()
    return _finish_gate_action('已跳过本次熔断等待，将立即试探一次下载。退避次数仍保留。', 'success')

@bp.route('/downloads/bot/reset', methods=['POST'])
@login_required
def bot_circuit_reset_route():
    from y2b.pipeline.gates import public_status, reset_bot_circuit
    from y2b.pipeline.task_manager import kick_pending_downloads_if_running
    reset_bot_circuit()
    status = public_status()
    if status.get('downloads_blocked'):
        return _finish_gate_action(
            '已重置 YouTube 熔断计数。下载仍被其他闸门拦住：' + (status.get('download_message') or ''),
            'warning',
        )
    kick_pending_downloads_if_running()
    return _finish_gate_action('已重置 YouTube 熔断，退避次数已清零。', 'success')

@bp.route('/tasks/<task_id>/fragment')
@login_required
def task_fragment(task_id):
    """返回单个任务的桌面/移动端片段 HTML。"""
    task = get_task(task_id)
    if not task:
        return jsonify({'success': False, 'message': '任务不存在'}), 404

    config = load_config()
    return jsonify({
        'success': True,
        **_render_task_fragments(task, config)
    })

@bp.route('/tasks/stream')
@login_required
def tasks_event_stream():
    """Server-Sent Events stream for realtime task updates."""

    def generate():
        listener = register_task_updates_listener()
        try:
            yield 'data: {"type":"welcome"}\n\n'
            while True:
                try:
                    event = listener.get(timeout=10)  # 减少心跳间隔到 10 秒
                    yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                except Empty:
                    yield 'data: {"type":"heartbeat"}\n\n'
        except GeneratorExit:
            pass
        finally:
            unregister_task_updates_listener(listener)

    response = Response(stream_with_context(generate()), mimetype='text/event-stream')
    response.headers['Cache-Control'] = 'no-cache'
    response.headers['X-Accel-Buffering'] = 'no'
    response.headers['Connection'] = 'keep-alive'
    response.headers['Transfer-Encoding'] = 'chunked'
    return response

@bp.route('/manual_review')
@login_required
def manual_review():
    """人工审核列表页面"""
    logger.info("访问人工审核列表页面")
    review_tasks = get_tasks_by_status(TASK_STATES['AWAITING_REVIEW'])
    for task in review_tasks:
        task['can_retry_translation'] = is_metadata_translation_retryable(task)
    
    # 封面图片现在直接从downloads目录提供
    
    return render_template('manual_review.html', tasks=review_tasks)

@bp.route('/tasks/<task_id>/edit', methods=['GET', 'POST'])
@login_required
def edit_task(task_id):
    """任务编辑页面"""
    task = get_task(task_id)
    
    if not task:
        flash('任务不存在', 'danger')
        return redirect(url_for('tasks'))
    
    if request.method == 'POST':
        # 主表单的隐藏字段已改名 default_action（避免与上传按钮的同名 action 冲突）。
        # 多值同名 action 时显式优先匹配 force_upload（而非取 DOM 顺序第一个）。
        action = _resolve_edit_task_action(request.form)

        def finish_edit(message, category, *, endpoint='edit_task', endpoint_kwargs=None, navigate=False, success=None):
            kwargs = {'task_id': task_id} if endpoint == 'edit_task' and endpoint_kwargs is None else endpoint_kwargs
            extra = None
            if _is_ajax_request() and not navigate:
                extra = _edit_task_client_state(get_task(task_id) or task)
            return _respond_page_action(
                message,
                category,
                endpoint=endpoint,
                endpoint_kwargs=kwargs,
                navigate=navigate,
                success=success,
                extra=extra,
            )

        if action == 'replace_cover':
            try:
                cover_file = request.files.get('cover_file')
                _replace_task_cover(task, cover_file)
                return finish_edit('任务封面已更新。', 'success')
            except Exception as e:
                logger.warning(f"替换任务 {task_id} 封面失败: {e}")
                return finish_edit(f'更换封面失败: {e}', 'danger', success=False)

        if action == 'restore_cover':
            try:
                _restore_task_cover(task)
                return finish_edit('已恢复原始封面。', 'success')
            except Exception as e:
                logger.warning(f"恢复任务 {task_id} 原始封面失败: {e}")
                return finish_edit(f'恢复原封面失败: {e}', 'danger', success=False)

        upload_target = str(task.get('upload_target') or 'acfun').lower()
        # 处理表单提交
        video_title = request.form.get('video_title_translated', '')
        description = request.form.get('description_translated', '')
        legacy_partition_id = request.form.get('selected_partition_id', '')
        partition_id_acfun = request.form.get('selected_partition_id_acfun', '')
        partition_id_bilibili = request.form.get('selected_partition_id_bilibili', '')
        tags_json = request.form.get('tags_json', '[]')

        if upload_target == 'both':
            partition_id_acfun = partition_id_acfun or legacy_partition_id
            partition_id_bilibili = partition_id_bilibili or legacy_partition_id
        elif upload_target == 'bilibili':
            partition_id_bilibili = partition_id_bilibili or legacy_partition_id
        else:
            partition_id_acfun = partition_id_acfun or legacy_partition_id
        # 更新任务信息
        update_data = {
            'video_title_translated': video_title,
            'description_translated': description,
            'selected_partition_id_acfun': partition_id_acfun,
            'selected_partition_id_bilibili': partition_id_bilibili,
            'tags_generated': tags_json,
            'error_message': None,
            'error_category': None,
        }

        # 只有在安全状态下才允许设置为可上传状态，避免与正在处理的任务产生竞态条件
        safe_states_to_make_uploadable = [
            TASK_STATES['DOWNLOADED'],        # 已下载，可以上传
            TASK_STATES['MODERATING'],        # 审核中，可以手动干预
            TASK_STATES['AWAITING_REVIEW'],   # 等待人工审核
            TASK_STATES['FAILED'],            # 失败状态，可以重试
            TASK_STATES['UPLOADING']          # 允许重置卡住的上传状态
        ]
        
        if task['status'] in safe_states_to_make_uploadable:
            update_data['status'] = TASK_STATES['READY_FOR_UPLOAD']
        
        try:
            # 确保silent参数是布尔类型
            final_update_data = update_data.copy()
            silent_param = False  # 默认值
            
            if 'silent' in final_update_data:
                if isinstance(final_update_data['silent'], str):
                    silent_param = final_update_data['silent'].lower() in ('true', 'yes', '1', 'on')
                elif isinstance(final_update_data['silent'], bool):
                    silent_param = final_update_data['silent']
                # 从final_update_data中移除silent，避免重复传递
                final_update_data.pop('silent')
            
            update_task(task_id, silent=silent_param, **final_update_data)
        except Exception as e:
            logger.warning(f"update_task调用失败: {e}")
        logger.info(f"任务 {task_id} 信息已更新")
        updated_task = get_task(task_id)
        if action == 'force_upload':
            config = load_config()
            upload_target = str((updated_task or task).get('upload_target') or 'acfun').lower()
            platform_name = '双平台' if upload_target == 'both' else ('bilibili' if upload_target == 'bilibili' else 'AcFun')
            missing_partitions = _missing_upload_partition_labels(updated_task or task, config)
            if missing_partitions:
                return finish_edit(
                    f'请先选择{ "、".join(missing_partitions) }，或开启分区推荐后再继续上传。',
                    'danger',
                    success=False,
                )

            _start_background_force_upload(task_id, config, platform_name)
            return finish_edit(
                f'已保存当前修改，并启动强制上传到{platform_name}，正在后台处理...',
                'info',
                endpoint='manual_review',
                navigate=True,
            )

        if updated_task and updated_task['status'] == TASK_STATES['READY_FOR_UPLOAD']:
            return finish_edit('任务已保存，当前可单独执行上传。', 'success')
        return finish_edit('任务已保存。', 'success')
    
    # GET请求，显示编辑页面
    # 封面图片现在直接从downloads目录提供
    upload_target = str(task.get('upload_target') or 'acfun').lower()
    acfun_id_mapping = _load_acfun_partition_mapping()
    bilibili_id_mapping = _build_bilibili_partition_mapping()
    id_mapping = bilibili_id_mapping if upload_target == 'bilibili' else acfun_id_mapping
    
    # 准备标签字符串
    tags_string = ""
    if task.get('tags_generated'):
        try:
            tags = json.loads(task['tags_generated'])
            tags_string = ", ".join(tags)
        except Exception as e:
            logger.error(f"解析标签JSON失败: {str(e)}")
    
    # 获取当前配置
    config = load_config()
    cover_state = _edit_task_client_state(task)
    can_upload = cover_state['can_upload']
    has_original_cover_backup = cover_state['has_original_cover_backup']
    has_cover_preview = cover_state['has_cover_preview']
    is_custom_cover_active = cover_state['is_custom_cover_active']
    current_cover_filename = cover_state['current_cover_filename']

    return render_template(
        'edit_task.html', 
        task=task, 
        id_mapping=id_mapping, 
        acfun_id_mapping=acfun_id_mapping,
        bilibili_id_mapping=bilibili_id_mapping,
        tags_string=tags_string,
        config=config,
        upload_target=upload_target,
        can_upload=can_upload,
        has_cover_preview=has_cover_preview,
        has_original_cover_backup=has_original_cover_backup,
        is_custom_cover_active=is_custom_cover_active,
        current_cover_filename=current_cover_filename
    )

@bp.route('/tasks/<task_id>/cover')
@login_required
def get_task_cover(task_id):
    """获取任务封面图片"""
    task = get_task(task_id)
    
    if not task:
        # 返回默认图片或404
        return '', 404
    
    try:
        task_dir_real = _get_task_dir_real(task_id)
    except (ValueError, OSError):
        return '', 404

    cover_path = _get_current_cover_path(task, task_dir_real)
    if cover_path and os.path.exists(cover_path):
        mime_type, _ = mimetypes.guess_type(cover_path)
        return send_file(cover_path, mimetype=mime_type)
    
    # 没有找到封面
    return '', 404

@bp.route('/tasks/<task_id>/review')
@login_required
def review_task(task_id):
    """重定向到任务编辑页面"""
    return redirect(url_for('edit_task', task_id=task_id))

@bp.route('/tasks/add_via_extension', methods=['POST', 'OPTIONS'])
@tgbot_upload_token_required
def add_task_via_extension():
    """
    通过浏览器扩展或API添加任务 (JSON格式)
    支持Telegram Bot、浏览器扩展等外部服务调用
    """
    # 处理CORS预检请求
    if request.method == 'OPTIONS':
        return '', 200
    
    try:
        # 优先从JSON获取，兼容form表单
        if request.is_json:
            data = request.get_json()
            youtube_url = data.get('youtube_url') if data else None
            upload_target = data.get('upload_target') if data else None
        else:
            youtube_url = request.form.get('youtube_url')
            upload_target = request.form.get('upload_target')

        if not youtube_url:
            return jsonify({'success': False, 'message': 'YouTube URL不能为空'}), 400
        valid_url, url_error = validate_youtube_submission_url(youtube_url)
        if not valid_url:
            return jsonify({'success': False, 'message': url_error}), 400

        config = load_config()
        if not upload_target:
            upload_target = config.get('UPLOAD_TARGET_DEFAULT', 'acfun')
        else:
            upload_target = str(upload_target).strip().lower()

        if upload_target not in ('acfun', 'bilibili', 'both'):
            return jsonify({'success': False, 'message': '无效的投稿平台参数'}), 400
        
        # 判断是否为播放列表URL
        if 'youtube.com/playlist' in youtube_url or 'youtu.be/playlist' in youtube_url:
            # 提取所有视频URL
            cookies_path = config.get('YOUTUBE_COOKIES_PATH')
            video_urls = extract_video_urls_from_playlist(youtube_url, cookies_path)
            if not video_urls:
                return jsonify({'success': False, 'message': '未能提取到播放列表中的视频'}), 400
            
            added_count = 0
            task_ids = []
            for url in video_urls:
                task_id = add_task(url, upload_target=upload_target)
                if task_id:
                    added_count += 1
                    task_ids.append(task_id)
                    # 自动模式下启动任务
                    if config.get('AUTO_MODE_ENABLED', False):
                        start_task(task_id, config)
            
            return jsonify({
                'success': True,
                'message': f'已批量添加 {added_count} 个视频任务（来自播放列表）',
                'task_ids': task_ids,
                'count': added_count
            }), 200
        else:
            # 单个视频
            task_id = add_task(youtube_url, upload_target=upload_target)
            if task_id:
                if config.get('AUTO_MODE_ENABLED', False):
                    logger.info(f"自动模式已启用，立即开始处理任务 {task_id}")
                    start_task(task_id, config)
                    return jsonify({
                        'success': True,
                        'message': f'任务已添加并开始处理',
                        'task_id': task_id
                    }), 200
                else:
                    return jsonify({
                        'success': True,
                        'message': '任务已添加',
                        'task_id': task_id
                    }), 200
            else:
                return jsonify({'success': False, 'message': '添加任务失败'}), 500
                
    except Exception as e:
        logger.error(f"通过扩展添加任务失败: {str(e)}")
        return jsonify({'success': False, 'message': '服务器内部错误，请稍后重试'}), 500

@bp.route('/tasks/add', methods=['POST'])
@login_required
def add_task_route():
    """添加新任务，支持播放列表批量添加"""
    youtube_url = request.form.get('youtube_url')
    upload_target = request.form.get('upload_target')
    template_id = request.form.get('template_id') or None
    if template_id == 'default':
        template_id = 'default'

    def finish(message, category, **kwargs):
        return _respond_page_action(message, category, endpoint='tasks', **kwargs)

    if not youtube_url:
        return finish('YouTube URL不能为空', 'danger', success=False)
    valid_url, url_error = validate_youtube_submission_url(youtube_url)
    if not valid_url:
        return finish(url_error, 'danger', success=False)

    config = load_config()
    if not upload_target:
        upload_target = config.get('UPLOAD_TARGET_DEFAULT', 'acfun')

    # 判断是否为播放列表URL
    if 'youtube.com/playlist' in youtube_url or 'youtu.be/playlist' in youtube_url:
        # 提取所有视频URL
        cookies_path = config.get('YOUTUBE_COOKIES_PATH')
        video_urls = extract_video_urls_from_playlist(youtube_url, cookies_path)
        if not video_urls:
            return finish('未能提取到播放列表中的视频', 'danger', success=False)
        added_count = 0
        for url in video_urls:
            task_id = add_task(url, upload_target=upload_target, template_id=template_id)
            if task_id:
                added_count += 1
        return finish(f'已批量添加 {added_count} 个视频任务（来自播放列表）', 'success', extra={'count': added_count})
    else:
        task_id = add_task(youtube_url, upload_target=upload_target, template_id=template_id)
        if task_id:
            if config.get('AUTO_MODE_ENABLED', False):
                logger.info(f"自动模式已启用，立即开始处理任务 {task_id}")
                start_task(task_id, config)
                return finish(
                    f'任务已添加并开始处理: {youtube_url}',
                    'success',
                    extra=_task_mutation_extra(task_id),
                )
            return finish(f'任务已添加: {youtube_url}', 'success', extra=_task_mutation_extra(task_id))
        return finish(f'添加任务失败: {youtube_url}', 'danger', success=False)

@bp.route('/tasks/<task_id>/start', methods=['POST'])
@login_required
def start_task_route(task_id):
    """开始处理任务"""
    task = get_task(task_id)
    
    if not task:
        return _respond_page_action('任务不存在', 'danger', endpoint='tasks', success=False)
    
    if task['status'] not in [TASK_STATES['PENDING'], TASK_STATES['FAILED']]:
        return _respond_page_action(
            f'当前任务状态为 {task_status_display(task["status"])}，不能启动',
            'warning',
            endpoint='tasks',
            success=False,
            extra={'task_id': task_id},
        )
    
    # 获取当前配置
    config = load_config()
    
    # 启动任务处理
    success = start_task(task_id, config)
    
    if success:
        # 检查是否是自动模式
        if config.get('AUTO_MODE_ENABLED', False):
            message, category = '任务已启动，自动模式将会自动完成下载、处理和上传', 'info'
        else:
            message, category = '任务处理已启动', 'success'
        return _respond_page_action(
            message,
            category,
            endpoint='tasks',
            extra=_task_mutation_extra(task_id),
        )

    return _respond_page_action(
        '启动任务处理失败',
        'danger',
        endpoint='tasks',
        success=False,
        extra={'task_id': task_id},
    )

@bp.route('/tasks/<task_id>/delete', methods=['POST'])
@login_required
def delete_task_route(task_id):
    """删除任务"""
    delete_files = request.form.get('delete_files', 'true').lower() in ('true', 'yes', '1', 'on')
    
    success = delete_task(task_id, delete_files)
    
    if success:
        return _respond_page_action(
            '任务已删除',
            'success',
            endpoint='tasks',
            extra={'task_id': task_id, 'removed': True},
        )
    return _respond_page_action(
        '删除任务失败',
        'danger',
        endpoint='tasks',
        success=False,
        extra={'task_id': task_id},
    )

@bp.route('/tasks/clear_all', methods=['POST'])
@login_required
def clear_all_tasks_route():
    """清空所有任务（可选择同时删除任务文件）"""
    try:
        delete_files = request.form.get('delete_files', 'true').lower() in ['true', '1', 'on']
        success = clear_all_tasks(delete_files=delete_files)
        if success:
            return _respond_page_action('所有任务已清空', 'success', endpoint='tasks', extra={'cleared': True})
        return _respond_page_action('清空任务失败，请查看日志', 'danger', endpoint='tasks', success=False)
    except Exception as e:
        logger.error(f"清空所有任务失败: {e}")
        return _respond_page_action(f'清空任务失败: {e}', 'danger', endpoint='tasks', success=False)

@bp.route('/tasks/retry_failed', methods=['POST'])
@login_required
def retry_failed_tasks_route():
    """重新调度所有失败的任务（从任务管理器调用）"""
    try:
        # 加载最新配置
        cfg = load_config()
        result = retry_failed_tasks(cfg)
        if isinstance(result, dict):
            scheduled = result.get('scheduled', 0)
            total = result.get('total', 0)
            return _respond_page_action(
                f'已重新调度 {scheduled}/{total} 个失败任务',
                'success',
                endpoint='tasks',
                extra={'scheduled': scheduled, 'total': total},
            )
        return _respond_page_action('重新调度失败，请查看日志', 'danger', endpoint='tasks', success=False)
    except Exception as e:
        logger.error(f"重试失败任务失败: {e}")
        return _respond_page_action(f'重试失败任务失败: {e}', 'danger', endpoint='tasks', success=False)

@bp.route('/tasks/<task_id>/retry_translation', methods=['POST'])
@login_required
def retry_metadata_translation_route(task_id):
    """重新执行因自动翻译失败而暂停的任务。"""
    task = get_task(task_id)
    if not task:
        return _respond_page_action('任务不存在', 'danger', endpoint='manual_review', success=False, extra={'task_id': task_id})

    if not is_metadata_translation_retryable(task):
        return _respond_page_action(
            '当前任务不是可重试的自动翻译失败任务',
            'warning',
            endpoint='manual_review',
            success=False,
            extra={'task_id': task_id},
        )

    config = load_config()
    retry_block_reason = get_metadata_translation_retry_block_reason(task, config)
    if retry_block_reason:
        return _respond_page_action(
            retry_block_reason,
            'warning',
            endpoint='manual_review',
            success=False,
            extra={'task_id': task_id},
        )

    try:
        if retry_metadata_translation_task(task_id, config):
            return _respond_page_action(
                '已重新启动自动翻译，任务将在后台继续处理',
                'success',
                endpoint='manual_review',
                extra={'task_id': task_id, 'removed': True},
            )
        return _respond_page_action(
            '重新启动自动翻译失败，请稍后重试',
            'danger',
            endpoint='manual_review',
            success=False,
            extra={'task_id': task_id},
        )
    except Exception as exc:
        logger.error(f"重试任务 {task_id} 的自动翻译失败: {exc}")
        return _respond_page_action(
            '重新启动自动翻译失败，请查看日志',
            'danger',
            endpoint='manual_review',
            success=False,
            extra={'task_id': task_id},
        )

@bp.route('/tasks/<task_id>/force_upload', methods=['POST'])
@login_required
def force_upload_task_route(task_id):
    """强制上传任务"""
    task = get_task(task_id)
    
    if not task:
        return _respond_page_action('任务不存在', 'danger', endpoint='manual_review', success=False, extra={'task_id': task_id})
    
    # 获取当前配置
    config = load_config()
    upload_target = str(task.get('upload_target') or 'acfun').lower()
    platform_name = '双平台' if upload_target == 'both' else ('bilibili' if upload_target == 'bilibili' else 'AcFun')
    missing_partitions = _missing_upload_partition_labels(task, config)
    if missing_partitions:
        return _respond_page_action(
            f'请先选择{ "、".join(missing_partitions) }，或开启分区推荐后再继续上传。',
            'danger',
            endpoint='edit_task',
            endpoint_kwargs={'task_id': task_id},
            success=False,
            navigate=True,
        )
    
    # 启动后台强制上传。未分区时才离开当前页去编辑；启动本身留在原页面。
    _start_background_force_upload(task_id, config, platform_name)

    return _respond_page_action(
        f'已启动强制上传到{platform_name}，正在后台处理...',
        'info',
        endpoint='manual_review',
        extra=_task_mutation_extra(task_id),
    )

@bp.route('/tasks/reset_stuck', methods=['POST'])
@login_required
def reset_stuck_tasks_route():
    """重置卡住的任务"""
    from y2b.pipeline.task_manager import reset_stuck_tasks
    
    try:
        reset_count = reset_stuck_tasks()
        if reset_count > 0:
            return _respond_page_action(
                f'已重置 {reset_count} 个卡住的任务',
                'success',
                endpoint='tasks',
                extra={'reset_count': reset_count},
            )
        return _respond_page_action('没有发现卡住的任务', 'info', endpoint='tasks', extra={'reset_count': 0})
    except Exception as e:
        logger.error(f"重置卡住任务失败: {str(e)}")
        return _respond_page_action('重置卡住任务失败', 'danger', endpoint='tasks', success=False)

@bp.route('/tasks/<task_id>/abandon', methods=['POST'])
@login_required
def abandon_task_route(task_id):
    """放弃任务：先取消，再标记失败；活动任务不立即删除文件。"""
    delete_files = request.form.get('delete_files', 'true').lower() in ('true', 'yes', '1', 'on')
    task = get_task(task_id)
    if not task:
        return _respond_page_action('任务不存在', 'danger', endpoint='tasks', success=False, extra={'task_id': task_id})

    request_task_cancel(task_id)
    active = _is_task_active(task_id)
    if not update_task(task_id, status=TASK_STATES['FAILED'], error_message="用户主动放弃任务"):
        return _respond_page_action('放弃任务失败，请稍后重试', 'danger', endpoint='tasks', success=False, extra={'task_id': task_id})

    files_skipped = False
    if delete_files and not active:
        from y2b.pipeline.task_manager import delete_task_files
        delete_task_files(task_id)
    elif delete_files:
        _schedule_cancelled_task_file_cleanup(task_id)
        files_skipped = True

    if files_skipped:
        message, category = '任务已废弃；检测到任务仍在运行，下载文件将在任务停止后清理。', 'warning'
    else:
        message, category = '任务已废弃', 'success'
    extra = _task_mutation_extra(task_id)
    extra['removed'] = True
    return _respond_page_action(message, category, endpoint='tasks', extra=extra)

app.register_blueprint(bp)
