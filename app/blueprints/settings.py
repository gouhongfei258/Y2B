"""Settings, maintenance, and account configuration.

Executed by create_app() in the app package namespace. Endpoint names stay
unprefixed so existing url_for() names and request.endpoint checks keep working.
"""
from app.blueprints.blueprint import Blueprint

bp = Blueprint('settings', 'app.blueprints.settings')

@bp.route('/settings', methods=['GET', 'POST'])
@login_required
def settings():
    """设置页面，用于管理配置"""
    if request.method == 'POST':
        config = load_config()
        form_data = request.form.to_dict()
        uploads = _extract_settings_uploads(request.files)
        operation_id = str(form_data.get('save_operation_id') or uuid.uuid4())
        enable_password_protection = str(form_data.get('password_protection_enabled', '')).lower() in ['true', '1', 'on']
        submitted_new_password = str(form_data.get('new_password') or '')
        submitted_confirm_password = str(form_data.get('confirm_password') or '')
        has_effective_password = (
            (submitted_new_password and submitted_new_password == submitted_confirm_password)
            or bool(config.get('password'))
        )

        # 当用户在未开启保护的情况下保存“启用密码保护”时，需要立即把当前会话标记为已登录，
        # 否则前端接下来轮询 /settings/save-progress/... 会被 login_required 重定向到登录页，
        # 导致保存进度卡住或请求解析失败。
        if enable_password_protection and has_effective_password:
            session['logged_in'] = True
            session.permanent = True
        # 关闭密码保护时不立即清除会话，避免中断当前保存流程的进度轮询。
        # 会话将在用户主动退出或session过期时自然失效。

        if _is_ajax_request():
            _update_settings_save_progress(
                operation_id,
                stage='saving_config',
                message='正在准备保存设置',
                detail='保存任务已创建，正在后台执行。',
                percent=None,
                done=False,
                level='info',
                success=None,
                messages=[]
            )
            save_thread = threading.Thread(
                target=_run_settings_save_operation,
                args=(operation_id, form_data, uploads),
                daemon=True,
                name=f'settings-save-{operation_id[:8]}'
            )
            save_thread.start()
            return jsonify({
                'success': True,
                'messages': [],
                'operation_id': operation_id
            })

        result = _perform_settings_save(form_data, uploads)
        for item in result.get('messages', []):
            flash(item.get('text', ''), item.get('category', 'info'))
        if not result.get('success', False):
            flash('配置保存失败，原配置未被替换。', 'danger')
        return redirect(url_for('settings'))
    
    # GET请求，显示设置页面
    config = load_config()
    acfun_partition_mapping = _load_acfun_partition_mapping()
    bilibili_partition_mapping = _build_bilibili_partition_mapping()
    try:
        from y2b.ai.prompt_manager import get_builtin_prompt_previews
        builtin_prompts = get_builtin_prompt_previews()
    except Exception as exc:
        logger.debug("获取内置 Prompt 预览失败，将不显示预览: %s", exc)
        builtin_prompts = {}
    try:
        from y2b.bilibili.bilibili_accounts import list_accounts, public_account
        bilibili_accounts = [public_account(item) for item in list_accounts(config)]
    except Exception as exc:
        logger.warning("读取 bilibili 账号列表失败: %s", exc)
        bilibili_accounts = []
    return render_template(
        'settings.html',
        config=config,
        tgbot_token_state=_tgbot_api_token_state(config),
        tgbot_token_csrf_token=_ensure_tgbot_token_csrf_token(),
        whisper_languages=WHISPER_LANGUAGE_LIST,
        acfun_partition_mapping=acfun_partition_mapping,
        bilibili_partition_mapping=bilibili_partition_mapping,
        builtin_prompts=builtin_prompts,
        bilibili_accounts=bilibili_accounts,
    )

@bp.route('/settings/update_cover_mode', methods=['POST'])
@login_required
def update_cover_mode():
    """Persist the cover processing mode used by subsequent uploads."""
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({
            'success': False,
            'message': '请求体必须是 JSON 对象。',
        }), 400

    mode = payload.get('mode')
    if not isinstance(mode, str) or mode not in ('crop', 'pad'):
        return jsonify({
            'success': False,
            'message': '封面处理模式必须是 crop 或 pad。',
        }), 400

    try:
        updated_config = update_config({'COVER_PROCESSING_MODE': mode})
    except Exception as exc:
        logger.error('保存封面处理模式失败: %s', exc)
        return jsonify({
            'success': False,
            'message': '保存封面处理模式失败，请稍后重试。',
        }), 500

    # Keep the running application and task processor in sync with the
    # persisted value; a restart should not be required for the next upload.
    try:
        configure_app(app, updated_config)
        from y2b.pipeline.task_manager import get_global_task_processor
        get_global_task_processor(updated_config)
    except Exception as exc:
        # Persistence already succeeded.  Log a runtime-refresh failure but
        # still report success so the caller does not incorrectly retry a
        # setting that is already stored on disk.
        logger.warning('封面处理模式已保存，但刷新运行时配置失败: %s', exc)

    return jsonify({
        'success': True,
        'mode': mode,
        'cover_processing_mode': mode,
        'message': '封面处理模式已更新。',
    })

@bp.route('/settings/tgbot-token', methods=['POST'])
@login_required
def settings_tgbot_token():
    payload = request.get_json(silent=True) if request.is_json else request.form
    payload = payload or {}
    csrf_token = payload.get('csrf_token') or request.headers.get('X-CSRF-Token')
    if not _validate_tgbot_token_csrf(csrf_token):
        return jsonify({'success': False, 'message': '安全校验失败，请刷新设置页后重试。'}), 403

    action = str(payload.get('action') or '').strip().lower()
    if action in ('generate', 'reset'):
        token = _generate_tgbot_api_token()
        updated_config = update_config({
            'TG_BOT_API_TOKEN_HASH': _hash_tgbot_api_token(token),
            'TG_BOT_API_TOKEN_CREATED_AT': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'TG_BOT_API_TOKEN_LAST4': token[-4:],
        })
        return jsonify({
            'success': True,
            'message': 'Telegram Bot API Token 已生成，旧 Token 已失效。请立即复制保存。',
            'token': token,
            'state': _tgbot_api_token_state(updated_config),
            'csrf_token': _ensure_tgbot_token_csrf_token(),
        })

    if action == 'revoke':
        updated_config = update_config({
            'TG_BOT_API_TOKEN_HASH': '',
            'TG_BOT_API_TOKEN_CREATED_AT': '',
            'TG_BOT_API_TOKEN_LAST4': '',
        })
        return jsonify({
            'success': True,
            'message': 'Telegram Bot API Token 已撤销。',
            'state': _tgbot_api_token_state(updated_config),
            'csrf_token': _ensure_tgbot_token_csrf_token(),
        })

    return jsonify({
        'success': False,
        'message': '未知的 Token 操作。',
        'csrf_token': _ensure_tgbot_token_csrf_token(),
    }), 400

@bp.route('/settings/save-progress/<operation_id>', methods=['GET'])
@login_required
def settings_save_progress(operation_id):
    progress = _get_settings_save_progress(operation_id)
    if not progress:
        return jsonify({
            'found': False,
            'stage': None,
            'message': '',
            'detail': '',
            'percent': None,
            'downloaded_bytes': None,
            'total_bytes': None,
            'done': True,
            'level': 'error',
            'success': False,
            'messages': []
        })

    return jsonify({
        'found': True,
        'stage': progress.get('stage'),
        'message': progress.get('message'),
        'detail': progress.get('detail'),
        'percent': progress.get('percent'),
        'downloaded_bytes': progress.get('downloaded_bytes'),
        'total_bytes': progress.get('total_bytes'),
        'done': progress.get('done', False),
        'level': progress.get('level', 'info'),
        'success': progress.get('success'),
        'messages': progress.get('messages', [])
    })

@bp.route('/settings/notifications/test', methods=['POST'])
@login_required
def settings_test_notification():
    if request.is_json:
        data = request.get_json(silent=True) or {}
        channel = str(data.get('channel') or '').strip()
    else:
        channel = str(request.form.get('channel') or '').strip()

    if channel not in (CHANNEL_WECOM, CHANNEL_SERVERCHAN, CHANNEL_MESSAGE_PUSHER):
        return jsonify({'success': False, 'message': '不支持的通知渠道'}), 400

    try:
        config = load_config()
        _sync_notification_service(config)
        service = get_global_notification_service(config)
        service.send_test_message(channel)
        return jsonify({
            'success': True,
            'message': f'{CHANNEL_LABELS.get(channel, channel)} 测试消息已发送'
        })
    except ValueError:
        logger.warning("测试通知发送失败，渠道=%s", channel, exc_info=True)
        return jsonify({
            'success': False,
            'message': f'{CHANNEL_LABELS.get(channel, channel)} 配置不完整，请检查后重试'
        }), 400
    except Exception:
        logger.exception("测试通知发送失败，渠道=%s", channel)
        return jsonify({'success': False, 'message': '测试通知发送失败，请稍后重试'}), 500

@bp.route('/settings/cookiecloud/test', methods=['POST'])
@login_required
def settings_test_cookiecloud():
    payload = request.get_json(silent=True) or {}
    effective_config = _merge_cookiecloud_runtime_settings(payload)

    try:
        result = test_cookiecloud_youtube_sync(effective_config)
        message = (
            f"CookieCloud 连接成功，已解析 {result['cookie_count']} 条 YouTube/Google Cookies，"
            f"当前使用 {result['crypto_type_used']} 算法。"
        )
        updated_at = _remember_cookiecloud_sync_result(True, message)
        return jsonify({
            'success': True,
            'message': message,
            'cookie_count': result['cookie_count'],
            'crypto_type_used': result['crypto_type_used'],
            'updated_at': updated_at,
            'status': 'success',
        })
    except CookieCloudError as exc:
        message = _cookiecloud_operation_error_message('test')
        updated_at = _remember_cookiecloud_sync_result(False, message)
        logger.warning('CookieCloud 连接测试失败（%s）: %s', type(exc).__name__, exc)
        return jsonify({
            'success': False,
            'message': message,
            'updated_at': updated_at,
            'status': 'error',
        }), 400
    except Exception:
        message = _cookiecloud_operation_error_message('test', retry_later=True)
        updated_at = _remember_cookiecloud_sync_result(False, message)
        logger.exception('CookieCloud 连接测试失败')
        return jsonify({
            'success': False,
            'message': message,
            'updated_at': updated_at,
            'status': 'error',
        }), 500

@bp.route('/settings/cookiecloud/sync', methods=['POST'])
@login_required
def settings_sync_cookiecloud():
    payload = request.get_json(silent=True) or {}
    effective_config = _merge_cookiecloud_runtime_settings(payload)

    try:
        result = sync_cookiecloud_to_youtube_file(effective_config)
        message = (
            f"CookieCloud 已成功写入 {result['cookie_count']} 条 YouTube/Google Cookies 到 "
            f"{result['output_path_display']}。"
        )
        updated_at = _remember_cookiecloud_sync_result(True, message)
        return jsonify({
            'success': True,
            'message': message,
            'cookie_count': result['cookie_count'],
            'crypto_type_used': result['crypto_type_used'],
            'output_path_display': result['output_path_display'],
            'updated_at': updated_at,
            'status': 'success',
        })
    except CookieCloudError as exc:
        message = _cookiecloud_operation_error_message('sync')
        updated_at = _remember_cookiecloud_sync_result(False, message)
        logger.warning('CookieCloud 立即拉取失败（%s）: %s', type(exc).__name__, exc)
        return jsonify({
            'success': False,
            'message': message,
            'updated_at': updated_at,
            'status': 'error',
        }), 400
    except Exception:
        message = _cookiecloud_operation_error_message('sync', retry_later=True)
        updated_at = _remember_cookiecloud_sync_result(False, message)
        logger.exception('CookieCloud 立即拉取失败')
        return jsonify({
            'success': False,
            'message': message,
            'updated_at': updated_at,
            'status': 'error',
        }), 500

@bp.route('/settings/acfun/qrcode/start', methods=['POST'])
@login_required
def acfun_qrcode_start():
    """发起 AcFun 二维码登录并返回二维码图片。"""
    config = load_config()
    cookie_path = resolve_cookie_file_path(
        path_value=config.get('ACFUN_COOKIES_PATH', 'cookies/ac_cookies.json'),
        default_relative_path='cookies/ac_cookies.json',
        service_name='AcFun',
        logger_obj=logger,
        allow_json_txt_fallback=True
    )

    try:
        session_id, qr_session = _create_acfun_qr_session()
        qr_data = qr_session.generate()
        return jsonify({
            'success': True,
            'session_id': session_id,
            'image_base64': qr_data.get('image_base64', ''),
            'mime_type': qr_data.get('mime_type', 'image/png'),
            'expires_in': _ACFUN_QR_SESSION_TTL_SECONDS,
            'qr_expires_in_ms': qr_data.get('expires_in_ms', 120000),
            'cookie_path': cookie_path,
        })
    except Exception as e:
        logger.error(f"发起 AcFun 二维码登录失败: {e}")
        return jsonify({'success': False, 'message': '二维码登录失败，请稍后重试'}), 500

@bp.route('/settings/acfun/qrcode/status/<session_id>', methods=['GET'])
@login_required
def acfun_qrcode_status(session_id):
    """轮询 AcFun 二维码登录状态。"""
    qr_session = _get_acfun_qr_session(session_id)
    if not qr_session:
        return jsonify({'success': False, 'message': '二维码会话不存在或已过期'}), 404

    config = load_config()
    cookie_path = resolve_cookie_file_path(
        path_value=config.get('ACFUN_COOKIES_PATH', 'cookies/ac_cookies.json'),
        default_relative_path='cookies/ac_cookies.json',
        service_name='AcFun',
        logger_obj=logger,
        allow_json_txt_fallback=True
    )

    try:
        status_data = qr_session.check_status(cookie_file=cookie_path)
        _emit_qr_login_event_once(
            _ACFUN_QR_SESSIONS,
            _ACFUN_QR_SESSION_LOCK,
            session_id,
            'AcFun',
            status_data,
        )
        status = status_data.get('status')
        # done/failed 状态保留到 TTL 自动清理，避免前端再次检查时立刻报“会话过期”
        # 仅 timeout（QR码确实过期）时立即移除
        if status == 'timeout':
            with _ACFUN_QR_SESSION_LOCK:
                _ACFUN_QR_SESSIONS.pop(session_id, None)
        return jsonify({'success': True, **status_data})
    except Exception as e:
        logger.error(f"查询 AcFun 二维码登录状态失败: {e}")
        return jsonify({'success': False, 'message': '查询登录状态失败，请稍后重试'}), 500

@bp.route('/settings/bilibili/qrcode/start', methods=['POST'])
@login_required
def _bilibili_account_from_request(config):
    from y2b.bilibili.bilibili_accounts import get_account, get_default_account, request_scope_for_account
    payload = request.get_json(silent=True) or {}
    account_id = (
        request.form.get('account_id')
        or request.args.get('account_id')
        or payload.get('account_id')
        or ''
    )
    account = get_account(str(account_id).strip(), config) if str(account_id).strip() else None
    if account is None:
        account = get_default_account(config)
    scope = request_scope_for_account(account)
    return account, scope

@bp.route('/settings/bilibili/qrcode/status/<session_id>', methods=['GET'])
@login_required
def bilibili_qrcode_status(session_id):
    """轮询 bilibili 二维码登录状态。"""
    record = _get_bilibili_qr_record(session_id)
    qr_session = record.get('session') if record else None
    if not qr_session:
        return jsonify({'success': False, 'message': '二维码会话不存在或已过期'}), 404

    config = load_config()
    from y2b.bilibili.bilibili_accounts import account_cookie_file, get_account, get_default_account, refresh_account_identity
    account = get_account(record.get('account_id'), config) or get_default_account(config)
    cookie_path = resolve_cookie_file_path(
        path_value=account.get("cookie_path") or config.get("BILIBILI_COOKIES_PATH", "cookies/bili_cookies.json"),
        default_relative_path="cookies/bili_cookies.json",
        service_name="Bilibili",
        logger_obj=logger,
        allow_json_txt_fallback=False,
    )
    if not cookie_path:
        cookie_path = account_cookie_file(account, config)

    try:
        status_data = qr_session.check_status(cookie_file=cookie_path)
        if status_data.get('status') == 'done' and status_data.get('cookies_saved'):
            ok, message = refresh_account_identity(account.get('id'), config)
            status_data['identity_updated'] = ok
            if ok:
                refreshed = get_account(account.get('id'), config) or account
                status_data['uname'] = refreshed.get('uname') or ''
                status_data['mid'] = refreshed.get('mid') or ''
            else:
                status_data['identity_message'] = message
        _emit_qr_login_event_once(
            _BILIBILI_QR_SESSIONS,
            _BILIBILI_QR_SESSION_LOCK,
            session_id,
            'bilibili',
            status_data,
        )
        status = status_data.get('status')
        if status in ('done', 'timeout', 'failed'):
            with _BILIBILI_QR_SESSION_LOCK:
                _BILIBILI_QR_SESSIONS.pop(session_id, None)
        return jsonify({'success': True, **status_data})
    except Exception as e:
        logger.error(f"查询 bilibili 二维码登录状态失败: {e}")
        return jsonify({'success': False, 'message': '查询登录状态失败，请稍后重试'}), 500

@bp.route('/settings/bilibili/accounts', methods=['POST'])
@login_required
def bilibili_account_create():
    """新建一个 bilibili 账号，Cookie 文件与其他账号分开。"""
    config = load_config()
    payload = request.get_json(silent=True) or request.form
    name = str(payload.get('name') or '').strip() or '新账号'
    from y2b.bilibili.bilibili_accounts import create_account
    account = create_account(name, config)
    from y2b.bilibili.bilibili_accounts import public_account
    return jsonify({
        'success': True,
        'message': '已添加账号。请单独扫码或上传 Cookie，不会覆盖其他账号。',
        'account': public_account(account),
        'accounts': _bilibili_accounts_payload(config),
    })

@bp.route('/settings/bilibili/accounts/<account_id>', methods=['POST'])
@login_required
def bilibili_account_update(account_id):
    config = load_config()
    payload = request.get_json(silent=True) or request.form
    fields = {
        'name': payload.get('name', ''),
        'proxy_enabled': payload.get('proxy_enabled'),
        'proxy_url': payload.get('proxy_url', ''),
        'proxy_username': payload.get('proxy_username', ''),
    }
    if 'proxy_password' in payload:
        fields['proxy_password'] = payload.get('proxy_password') or ''
    from y2b.bilibili.bilibili_accounts import public_account, update_account
    try:
        account = update_account(account_id, fields, config)
    except ValueError as exc:
        return jsonify({'success': False, 'message': str(exc)}), 400
    if not account:
        return jsonify({'success': False, 'message': '账号不存在'}), 404
    return jsonify({
        'success': True,
        'message': '账号已保存',
        'account': public_account(account),
        'accounts': _bilibili_accounts_payload(config),
    })

@bp.route('/settings/bilibili/accounts/<account_id>/default', methods=['POST'])
@login_required
def bilibili_account_make_default(account_id):
    config = load_config()
    from y2b.bilibili.bilibili_accounts import public_account, set_default_account
    account = set_default_account(account_id, config)
    if not account:
        return jsonify({'success': False, 'message': '账号不存在'}), 404
    return jsonify({
        'success': True,
        'message': f'已将「{account.get("name") or "账号"}」设为默认账号',
        'account': public_account(account),
        'accounts': _bilibili_accounts_payload(load_config()),
    })

@bp.route('/settings/bilibili/accounts/<account_id>/delete', methods=['POST'])
@login_required
def bilibili_account_delete(account_id):
    config = load_config()
    from y2b.bilibili.bilibili_accounts import delete_account
    ok, message = delete_account(account_id, config)
    status = 200 if ok else 400
    return jsonify({
        'success': ok,
        'message': message,
        'accounts': _bilibili_accounts_payload(config),
    }), status

@bp.route('/settings/bilibili/accounts/<account_id>/cookies', methods=['POST'])
@login_required
def bilibili_account_upload_cookies(account_id):
    """只覆盖这一个账号的 Cookie 文件。"""
    config = load_config()
    from y2b.bilibili.bilibili_accounts import account_cookie_file, get_account, public_account, refresh_account_identity
    account = get_account(account_id, config)
    if not account:
        return jsonify({'success': False, 'message': '账号不存在'}), 404
    upload = request.files.get('cookies_file')
    if not upload or not upload.filename:
        return jsonify({'success': False, 'message': '请选择 Cookie 文件'}), 400
    content = upload.read()
    if not content or len(content) > 2 * 1024 * 1024:
        return jsonify({'success': False, 'message': 'Cookie 文件为空或过大'}), 400
    cookie_path = account_cookie_file(account, config)
    directory = os.path.dirname(cookie_path)
    os.makedirs(directory, exist_ok=True)
    temporary = cookie_path + '.uploading'
    try:
        with open(temporary, 'wb') as handle:
            handle.write(content)
        os.replace(temporary, cookie_path)
    except OSError:
        logger.exception("保存 bilibili 账号 Cookie 文件失败")
        return jsonify({'success': False, 'message': 'Cookie 文件保存失败'}), 500
    from y2b.pipeline.task_manager import validate_cookies
    valid, message = validate_cookies(cookie_path, 'Bilibili')
    if not valid:
        return jsonify({'success': False, 'message': f'Cookie 文件未通过校验: {message}'}), 400
    ok, identity_message = refresh_account_identity(account_id, config)
    refreshed = get_account(account_id, config) or account
    return jsonify({
        'success': True,
        'message': 'Cookie 已写入该账号' + ('' if ok else '，昵称稍后再试'),
        'identity_updated': ok,
        'identity_message': identity_message,
        'account': public_account(refreshed),
        'accounts': _bilibili_accounts_payload(config),
    })

@bp.route('/settings/reset', methods=['POST'])
@login_required
def reset_settings():
    """重置设置"""
    try:
        data = request.get_json() or {}
        keys = data.get('keys', [])
        
        if keys:
            # 重置指定项。直接返回新值，页面就地写回控件，不再整页刷新。
            updated = reset_specific_config(keys)
            values = {}
            for key in keys:
                if key in DEFAULT_CONFIG:
                    values[key] = _settings_form_value(updated.get(key, DEFAULT_CONFIG.get(key)))
        else:
            # 如果未指定keys，则不执行任何操作或返回错误
            # 为了防止误操作全重置，这里要求必须指定keys
            return jsonify({'status': 'error', 'success': False, 'message': '未指定要重置的配置项'}), 400

        return jsonify({
            'status': 'success',
            'success': True,
            'message': '当前页面的设置已重置为默认值。',
            'values': values,
        })
    except Exception as e:
        logger.error(f"重置设置失败: {str(e)}")
        return jsonify({'status': 'error', 'message': '重置设置失败，请稍后重试'}), 500

@bp.route('/logs/cleanup', methods=['POST'])
@login_required
def cleanup_logs_route():
    """手动触发日志清理"""
    config = load_config()
    hours = int(request.form.get('hours', config.get('LOG_CLEANUP_HOURS', DEFAULT_CONFIG['LOG_CLEANUP_HOURS'])))
    
    result = cleanup_logs(hours)
    
    if result.get('success'):
        return _respond_page_action(
            f"日志清理成功，删除了{result['files_removed']}个文件，释放了{result['bytes_freed_readable']}空间",
            'success',
            endpoint='settings',
        )
    return _respond_page_action(
        f"日志清理失败: {result.get('error', '未知错误')}",
        'danger',
        endpoint='settings',
        success=False,
    )

@bp.route('/maintenance/clear_logs', methods=['POST'])
@login_required
def clear_logs_route():
    """立即清空特定日志文件"""
    result = clear_specific_logs()
    
    if result.get('success'):
        processed_files_str = "、".join(result['processed_files'])
        return _respond_page_action(
            f"日志清理成功，已处理{result['files_processed']}个文件（{processed_files_str}），释放了{result['bytes_freed_readable']}空间",
            'success',
            endpoint='settings',
        )
    return _respond_page_action(
        f"日志清理失败: {result.get('error', '未知错误')}",
        'danger',
        endpoint='settings',
        success=False,
    )

@bp.route('/maintenance/cleanup_downloads', methods=['POST'])
@login_required
def cleanup_downloads_route():
    """手动触发下载内容清理"""
    config = load_config()
    hours = int(request.form.get('hours', config.get('DOWNLOAD_CLEANUP_HOURS', 72)))
    
    result = cleanup_downloads(hours)
    
    if result.get('success'):
        return _respond_page_action(
            f"下载内容清理成功，删除了{result['dirs_removed']}个目录、{result['files_removed']}个文件，释放了{result['bytes_freed_readable']}空间",
            'success',
            endpoint='settings',
        )
    return _respond_page_action(
        f"下载内容清理失败: {result.get('error', '未知错误')}",
        'danger',
        endpoint='settings',
        success=False,
    )

app.register_blueprint(bp)
