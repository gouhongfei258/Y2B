"""YouTube channel monitor.

Executed by create_app() in the app package namespace. Endpoint names stay
unprefixed so existing url_for() names and request.endpoint checks keep working.
"""
from app.blueprints.blueprint import Blueprint

bp = Blueprint('monitor', 'app.blueprints.monitor')

@bp.route('/youtube_monitor')
@login_required
def youtube_monitor_index():
    """YouTube监控主页"""
    configs = youtube_monitor.get_monitor_configs()
    history = youtube_monitor.get_monitor_history(limit=50)
    return render_template('youtube_monitor.html', configs=configs, history=history)

@bp.route('/youtube_monitor/config', methods=['GET', 'POST'])
@login_required
def youtube_monitor_config():
    """监控配置页面"""
    if request.method == 'POST':
        try:
            # 安全的整数转换函数
            def safe_int(value, default=0):
                if not value or value.strip() == '':
                    return default
                try:
                    return int(value)
                except (ValueError, TypeError):
                    return default
            
            # 获取监控类型和模式
            monitor_type = request.form.get('monitor_type', 'youtube_search')
            channel_mode = request.form.get('channel_mode', 'latest')
            
            config_data = {
                'name': request.form.get('name', '').strip(),
                'enabled': 'enabled' in request.form,
                'monitor_type': monitor_type,
                'channel_mode': channel_mode,
                'region_code': request.form.get('region_code', 'US'),
                'category_id': request.form.get('category_id', '0'),
                'time_period': safe_int(request.form.get('time_period'), 7),
                'max_results': safe_int(request.form.get('max_results'), 10),
                'min_view_count': safe_int(request.form.get('min_view_count'), 0),
                'min_like_count': safe_int(request.form.get('min_like_count'), 0),
                'min_comment_count': safe_int(request.form.get('min_comment_count'), 0),
                'keywords': request.form.get('keywords', ''),
                'exclude_keywords': request.form.get('exclude_keywords', ''),
                'title_keywords': (request.form.get('title_keywords') or '').strip(),
                'title_match_mode': _normalize_title_match_mode(request.form.get('title_match_mode')),
                'channel_ids': request.form.get('channel_ids', ''),
                'channel_keywords': request.form.get('channel_keywords', ''),
                'exclude_channel_ids': request.form.get('exclude_channel_ids', ''),
                'min_duration': safe_int(request.form.get('min_duration'), 0),
                'max_duration': safe_int(request.form.get('max_duration'), 0),
                'schedule_type': request.form.get('schedule_type', 'manual'),
                'schedule_interval': safe_int(request.form.get('schedule_interval'), 120),
                'order_by': request.form.get('order_by', 'viewCount'),
                'start_date': request.form.get('start_date', ''),
                'end_date': request.form.get('end_date', ''),
                'latest_days': safe_int(request.form.get('latest_days'), 7),
                'latest_max_results': safe_int(request.form.get('latest_max_results'), 20),
                'rate_limit_requests': safe_int(request.form.get('rate_limit_requests'), 20),
                'rate_limit_window': safe_int(request.form.get('rate_limit_window'), 60),
                'auto_add_to_tasks': 'auto_add_to_tasks' in request.form,
                'video_types': ','.join(request.form.getlist('video_types') or ['video','short','live']),
                'upload_template_id': (request.form.get('upload_template_id') or '').strip(),
            }
            
            # 验证必填项
            if not config_data['name']:
                flash('配置名称不能为空', 'danger')
                return render_template('youtube_monitor_config.html', **_monitor_form_context())
            
            config_id = youtube_monitor.create_monitor_config(config_data)
            flash(f'监控配置 "{config_data["name"]}" 创建成功！', 'success')
            return redirect(url_for('youtube_monitor_index'))
            
        except Exception as e:
            flash(f'创建监控配置失败: {str(e)}', 'danger')
    
    return render_template('youtube_monitor_config.html', **_monitor_form_context())

@bp.route('/youtube_monitor/config/<int:config_id>/edit', methods=['GET', 'POST'])
@login_required
def youtube_monitor_config_edit(config_id):
    """编辑监控配置"""
    config = youtube_monitor.get_monitor_config(config_id)
    if not config:
        flash('监控配置不存在', 'danger')
        return redirect(url_for('youtube_monitor_index'))
    
    if request.method == 'POST':
        try:
            # 安全的整数转换函数
            def safe_int(value, default=0):
                if not value or value.strip() == '':
                    return default
                try:
                    return int(value)
                except (ValueError, TypeError):
                    return default
            
            # 获取监控类型和模式
            monitor_type = request.form.get('monitor_type', 'youtube_search')
            channel_mode = request.form.get('channel_mode', 'latest')
            
            config_data = {
                'name': request.form.get('name', '').strip(),
                'enabled': 'enabled' in request.form,
                'monitor_type': monitor_type,
                'channel_mode': channel_mode,
                'region_code': request.form.get('region_code', 'US'),
                'category_id': request.form.get('category_id', '0'),
                'time_period': safe_int(request.form.get('time_period'), 7),
                'max_results': safe_int(request.form.get('max_results'), 10),
                'min_view_count': safe_int(request.form.get('min_view_count'), 0),
                'min_like_count': safe_int(request.form.get('min_like_count'), 0),
                'min_comment_count': safe_int(request.form.get('min_comment_count'), 0),
                'keywords': request.form.get('keywords', ''),
                'exclude_keywords': request.form.get('exclude_keywords', ''),
                'title_keywords': (request.form.get('title_keywords') or '').strip(),
                'title_match_mode': _normalize_title_match_mode(request.form.get('title_match_mode')),
                'channel_ids': request.form.get('channel_ids', ''),
                'channel_keywords': request.form.get('channel_keywords', ''),
                'exclude_channel_ids': request.form.get('exclude_channel_ids', ''),
                'min_duration': safe_int(request.form.get('min_duration'), 0),
                'max_duration': safe_int(request.form.get('max_duration'), 0),
                'schedule_type': request.form.get('schedule_type', 'manual'),
                'schedule_interval': safe_int(request.form.get('schedule_interval'), 120),
                'order_by': request.form.get('order_by', 'viewCount'),
                'start_date': request.form.get('start_date', ''),
                'end_date': request.form.get('end_date', ''),
                'latest_days': safe_int(request.form.get('latest_days'), 7),
                'latest_max_results': safe_int(request.form.get('latest_max_results'), 20),
                'rate_limit_requests': safe_int(request.form.get('rate_limit_requests'), 20),
                'rate_limit_window': safe_int(request.form.get('rate_limit_window'), 60),
                'auto_add_to_tasks': 'auto_add_to_tasks' in request.form,
                'video_types': ','.join(request.form.getlist('video_types') or ['video','short','live']),
                'upload_template_id': (request.form.get('upload_template_id') or '').strip(),
            }
            
            # 验证必填项
            if not config_data['name']:
                flash('配置名称不能为空', 'danger')
                return render_template('youtube_monitor_config.html', **_monitor_form_context({'config': config, 'is_edit': True}))
            
            youtube_monitor.update_monitor_config(config_id, config_data)
            flash(f'监控配置更新成功！', 'success')
            return redirect(url_for('youtube_monitor_index'))
            
        except Exception as e:
            flash(f'更新监控配置失败: {str(e)}', 'danger')
    
    return render_template('youtube_monitor_config.html', **_monitor_form_context({'config': config, 'is_edit': True}))

@bp.route('/youtube_monitor/config/<int:config_id>/delete', methods=['POST'])
@login_required
def youtube_monitor_config_delete(config_id):
    """删除监控配置"""
    try:
        config = youtube_monitor.get_monitor_config(config_id)
        if config:
            youtube_monitor.delete_monitor_config(config_id)
            return _respond_page_action(
                f'监控配置 "{config["name"]}" 删除成功！',
                'success',
                endpoint='youtube_monitor_index',
                extra={'config_id': config_id, 'removed': True},
            )
        return _respond_page_action('监控配置不存在', 'danger', endpoint='youtube_monitor_index', success=False)
    except Exception as e:
        return _respond_page_action(
            f'删除监控配置失败: {str(e)}',
            'danger',
            endpoint='youtube_monitor_index',
            success=False,
        )

@bp.route('/youtube_monitor/config/<int:config_id>/run', methods=['POST'])
@login_required
def youtube_monitor_run(config_id):
    """立即执行一次监控任务"""
    operation_id, config, error_message = _start_monitor_run_operation(config_id)
    if error_message:
        if _is_ajax_request():
            return jsonify({'success': False, 'message': error_message}), 404
        flash(error_message, 'danger')
        return redirect(url_for('youtube_monitor_index'))

    if not config:
        fallback_message = '监控配置不存在'
        if _is_ajax_request():
            return jsonify({'success': False, 'message': fallback_message}), 404
        flash(fallback_message, 'danger')
        return redirect(url_for('youtube_monitor_index'))

    started_message = f"监控已在后台开始执行：{config['name']}"
    if _is_ajax_request():
        return jsonify({
            'success': True,
            'message': started_message,
            'operation_id': operation_id,
            'config_id': config_id,
        })

    flash(f'{started_message}，请稍后刷新查看结果。', 'info')
    return redirect(url_for('youtube_monitor_history', config_id=config_id))

@bp.route('/youtube_monitor/run-status/<operation_id>', methods=['GET'])
@login_required
def youtube_monitor_run_status(operation_id):
    """查询后台监控任务的执行状态"""
    progress = _get_monitor_run_progress(operation_id)
    if not progress:
        return jsonify({
            'found': False,
            'config_id': None,
            'message': '',
            'detail': '',
            'done': True,
            'level': 'error',
            'success': False,
        })

    return jsonify({
        'found': True,
        'config_id': progress.get('config_id'),
        'message': progress.get('message', ''),
        'detail': progress.get('detail', ''),
        'done': progress.get('done', False),
        'level': progress.get('level', 'info'),
        'success': progress.get('success'),
    })

@bp.route('/youtube_monitor/history/<int:config_id>')
@login_required
def youtube_monitor_history(config_id):
    """查看指定监控配置的发现历史"""
    config = youtube_monitor.get_monitor_config(config_id)
    if not config:
        flash('监控配置不存在', 'danger')
        return redirect(url_for('youtube_monitor_index'))
    
    history = youtube_monitor.get_monitor_history(config_id, limit=200)
    
    # 计算统计数据
    stats = {
        'total_records': len(history),
        'added_to_tasks': 0,
        'avg_views': 0,
        'avg_likes': 0
    }
    
    if history:
        total_views = 0
        total_likes = 0
        
        for record in history:
            if record.get('added_to_tasks'):
                stats['added_to_tasks'] += 1
            total_views += record.get('view_count', 0)
            total_likes += record.get('like_count', 0)
        
        stats['avg_views'] = int(total_views / len(history))
        stats['avg_likes'] = int(total_likes / len(history))
    
    return render_template('youtube_monitor_history.html', history=history, config=config, stats=stats)

@bp.route('/youtube_monitor/add_to_tasks', methods=['POST'])
@login_required
def youtube_monitor_add_to_tasks():
    """从监控历史中添加视频到任务列表"""
    data = request.get_json(silent=True) or {}
    video_id = data.get('video_id')
    config_id = data.get('config_id')
    if not video_id or not config_id:
        return jsonify({'success': False, 'message': '参数不完整'}), 400
    try:
        config_id_int = int(config_id)
    except (TypeError, ValueError):
        return jsonify({'success': False, 'message': 'config_id 无效'}), 400

    success, message = youtube_monitor.add_video_to_tasks_manually(video_id, config_id_int)

    if success:
        return jsonify({'success': True, 'message': message})
    else:
        return jsonify({'success': False, 'message': message}), 400

@bp.route('/youtube_monitor/history/<int:config_id>/clear', methods=['POST'])
@login_required
def youtube_monitor_clear_history(config_id):
    """清空指定监控任务的历史记录"""
    youtube_monitor.clear_monitor_history(config_id)
    return _respond_page_action(
        '已清除该配置的历史记录',
        'success',
        endpoint='youtube_monitor_index',
        extra={'config_id': config_id, 'cleared': True},
    )

@bp.route('/youtube_monitor/history/clear_all', methods=['POST'])
@login_required
def youtube_monitor_clear_all_history():
    """清空所有历史记录"""
    youtube_monitor.clear_all_monitor_history()
    return _respond_page_action('已清除全部监控历史记录', 'success', endpoint='youtube_monitor_index', extra={'cleared': True})

@bp.route('/youtube_monitor/restore_configs', methods=['POST'])
@login_required
def youtube_monitor_restore_configs():
    """恢复默认监控配置"""
    youtube_monitor.restore_configs_from_files_manually()
    return _respond_page_action('监控配置已从文件恢复', 'success', endpoint='youtube_monitor_index')

@bp.route('/youtube_monitor/config/<int:config_id>/reset_offset', methods=['POST'])
@login_required
def youtube_monitor_reset_offset(config_id):
    """重置频道监控的视频偏移量"""
    youtube_monitor.reset_historical_offset(config_id)
    return _respond_page_action(
        '已重置该监控的历史偏移',
        'success',
        endpoint='youtube_monitor_index',
        extra={'config_id': config_id},
    )

app.register_blueprint(bp)
