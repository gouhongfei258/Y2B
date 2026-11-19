# Loaded by create_app() into the app package namespace.
# Helpers stay here; route functions live in app/blueprints.

import os
import json
import hashlib
import logging
import mimetypes
import re
import secrets
import shutil
import time
import uuid
import threading

from datetime import datetime, timedelta
from urllib.parse import urlparse
from logging.handlers import RotatingFileHandler
from flask import Flask, render_template, request, jsonify, redirect, url_for, flash, send_file, session, Response, stream_with_context
from functools import wraps
from flask_cors import CORS
from PIL import Image, UnidentifiedImageError
from werkzeug.security import safe_join
from y2b.youtube.youtube_handler import extract_video_urls_from_playlist, normalize_youtube_url
from y2b.utils import get_app_root_dir, get_app_subdir, normalize_reasoning_effort
from y2b.config.config_manager import load_config, update_config, reset_specific_config, DEFAULT_CONFIG, AI_REASONING_EFFORT_KEYS
from y2b.ai.whisper_languages import WHISPER_LANGUAGE_LIST
from y2b.subtitles.subtitle_style import normalize_hex_color
from y2b.pipeline.task_manager import add_task, start_task, get_task, get_tasks_paginated, get_tasks_by_status, update_task, delete_task, force_upload_task, TASK_STATES, clear_all_tasks, retry_failed_tasks, is_metadata_translation_retryable, get_metadata_translation_retry_block_reason, retry_metadata_translation_task, register_task_updates_listener, unregister_task_updates_listener, resolve_cookie_file_path, request_task_cancel, _is_task_active, _TASK_SCHEDULING_LOCK
from y2b.acfun.acfun_auth import AcfunQrLoginSession
from y2b.bilibili.bilibili_auth import BilibiliQrLoginSession
from y2b.bilibili.bilibili_uploader import bilibili_submit_api_badge
from queue import Empty
from y2b.youtube.youtube_monitor import youtube_monitor
from y2b.ai.speech_pipeline_settings import (
    SPEECH_PIPELINE_CHECKBOXES,
    SPEECH_PIPELINE_FLOAT_FIELDS,
    SPEECH_PIPELINE_INT_FIELDS,
)
from y2b.config.tag_presets import (
    is_preset_tags_enabled,
    normalize_preset_tags_config,
)
from y2b.cookies.cookiecloud import (
    CookieCloudError,
    sync_cookiecloud_to_youtube_file,
    test_cookiecloud_youtube_sync,
)
from y2b.notifications import (
    CHANNEL_LABELS,
    CHANNEL_MESSAGE_PUSHER,
    CHANNEL_SERVERCHAN,
    CHANNEL_WECOM,
    EVENT_LOGIN_LOCKED,
    EVENT_LOGIN_SUCCESS,
    EVENT_QR_LOGIN_FAILED,
    EVENT_QR_LOGIN_SUCCESS,
    NotificationEvent,
    emit_notification_event,
    get_global_notification_service,
    iter_enabled_channel_ids,
    validate_channel_config_fields,
)
from apscheduler.schedulers.background import BackgroundScheduler

app.secret_key = os.urandom(24)  # 用于flash消息


def _ensure_csrf_token() -> str:
    token = session.get('csrf_token')
    if not isinstance(token, str) or len(token) < 32:
        token = secrets.token_urlsafe(32)
        session['csrf_token'] = token
    return token


def _request_csrf_token() -> str:
    return str(
        request.headers.get('X-CSRF-Token')
        or request.form.get('csrf_token')
        or ((request.get_json(silent=True) or {}).get('csrf_token') if request.is_json else '')
        or ''
    )


def _origin_is_same_site(origin: str | None) -> bool:
    if not origin:
        return True
    if origin == 'null':
        return False
    try:
        parsed = urlparse(origin)
        request_host = (request.host_url or '').rstrip('/')
        return bool(parsed.scheme and parsed.netloc and f'{parsed.scheme}://{parsed.netloc}'.rstrip('/') == request_host)
    except Exception:
        return False


@app.before_request
def _protect_cookie_state_changes():
    if request.method not in ('POST', 'PUT', 'PATCH', 'DELETE'):
        return None
    # 外部扩展入口使用独立 Bearer Token，不依赖浏览器 Cookie。
    if request.endpoint == 'add_task_via_extension':
        return None
    # 登录请求本身需要 token；未启用认证保护时不存在可利用的认证 Cookie，
    # 保留旧版匿名本地表单兼容性。受保护会话则必须经过 login_required 后再校验。
    if request.endpoint not in ('login', 'logout') and not session.get('logged_in'):
        return None
    origin = request.headers.get('Origin')
    if origin and not _origin_is_same_site(origin):
        return jsonify({'success': False, 'message': '请求来源校验失败。'}), 403
    token = _request_csrf_token()
    expected = session.get('csrf_token')
    if not isinstance(expected, str) or not token or not secrets.compare_digest(expected, token):
        return jsonify({'success': False, 'message': '安全校验失败，请刷新页面后重试。'}), 403
    return None


@app.context_processor
def _inject_csrf_context():
    return {'csrf_token': _ensure_csrf_token()}


@app.context_processor
def inject_app_settings():
    app_settings = app.config.get('Y2A_SETTINGS', {})
    if not isinstance(app_settings, dict):
        app_settings = {}
    return {
        'now': datetime.now(),  # 每次请求动态获取当前时间
        'app_settings': app_settings,
        'show_logout_in_nav': bool(
            app_settings.get('password_protection_enabled') and session.get('logged_in')
        ),
    }

ALLOWED_COVER_EXTENSIONS = {
    '.jpg': 'image/jpeg',
    '.jpeg': 'image/jpeg',
    '.png': 'image/png',
    '.webp': 'image/webp',
}

# bilibili二维码登录会话（内存）
_BILIBILI_QR_SESSIONS = {}
_BILIBILI_QR_SESSION_LOCK = threading.Lock()
_BILIBILI_QR_SESSION_TTL_SECONDS = 300
# AcFun二维码登录会话（内存）
_ACFUN_QR_SESSIONS = {}
_ACFUN_QR_SESSION_LOCK = threading.Lock()
_ACFUN_QR_SESSION_TTL_SECONDS = 420
# 登录安全状态存储
def _get_security_state_path():
    try:
        db_dir = get_app_subdir('db')
    except Exception:
        # 回退到当前目录下的db
        db_dir = os.path.join(get_app_root_dir(), 'db')
    os.makedirs(db_dir, exist_ok=True)
    return os.path.join(db_dir, 'security_state.json')

def _load_security_state():
    path = _get_security_state_path()
    try:
        if os.path.exists(path) and os.path.getsize(path) > 0:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
                # 兼容缺失字段
                if not isinstance(data, dict):
                    data = {}
        else:
            data = {}
    except Exception:
        data = {}
    # 默认值
    return {
        'failed_attempts': int(data.get('failed_attempts', 0) or 0),
        'locked_until': float(data.get('locked_until', 0) or 0.0),
        'last_attempt': float(data.get('last_attempt', 0) or 0.0),
    }

def _save_security_state(state):
    try:
        path = _get_security_state_path()
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        return True
    except Exception:
        return False


def _describe_youtube_api_status(status_code: str) -> str:
    messages = {
        'direct_ready': 'YouTube API 初始化成功，当前为直连模式',
        'proxy_ready': 'YouTube API 初始化成功，独立代理已启用',
        'missing_api_key': 'YouTube API 密钥未配置，请先在设置页完成接入。',
        'init_failed': 'YouTube监控 API 初始化失败，请检查 API 密钥、代理配置与网络连通性。',
    }
    return messages.get(status_code, 'YouTube监控 API 状态未知，请检查设置。')


def _build_startup_config_log_summary(config: dict | None) -> dict:
    normalized = dict(config or {})

    return {
        'feature_flags': {
            'AUTO_MODE_ENABLED': bool(normalized.get('AUTO_MODE_ENABLED', False)),
            'NOTIFY_ENABLED': bool(normalized.get('NOTIFY_ENABLED', False)),
            'password_protection_enabled': bool(normalized.get('password_protection_enabled', False)),
            'CONTENT_MODERATION_ENABLED': bool(normalized.get('CONTENT_MODERATION_ENABLED', False)),
            'YOUTUBE_PROXY_ENABLED': bool(normalized.get('YOUTUBE_PROXY_ENABLED', False)),
            'YOUTUBE_API_PROXY_ENABLED': bool(normalized.get('YOUTUBE_API_PROXY_ENABLED', False)),
            'COOKIECLOUD_ENABLED': bool(normalized.get('COOKIECLOUD_ENABLED', False)),
            'SUBTITLE_TRANSLATION_ENABLED': bool(normalized.get('SUBTITLE_TRANSLATION_ENABLED', False)),
            'SPEECH_RECOGNITION_ENABLED': bool(normalized.get('SPEECH_RECOGNITION_ENABLED', False)),
        },
        'config_keys_total': len(normalized),
    }


def _sync_notification_service(config: dict | None = None):
    effective_config = dict(config or load_config())
    try:
        get_global_notification_service(effective_config)
        logger.info("通知服务配置已同步")
    except Exception as e:
        logger.warning(f"同步通知服务配置失败: {e}")


def _append_notification_config_warnings(messages: list, config: dict | None):
    effective_config = dict(config or {})
    if not effective_config.get('NOTIFY_ENABLED'):
        return
    for channel_id in iter_enabled_channel_ids(effective_config):
        missing_fields = validate_channel_config_fields(channel_id, effective_config)
        if missing_fields:
            readable_fields = '、'.join(missing_fields)
            channel_label = CHANNEL_LABELS.get(channel_id, channel_id)
            _append_settings_message(
                messages,
                'warning',
                f'已启用 {channel_label} 通知，但缺少配置：{readable_fields}。该渠道会暂时跳过发送。'
            )


def _coerce_checkbox_value(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value or '').strip().lower() in ('true', '1', 'on', 'yes', 'y')


def _merge_cookiecloud_runtime_settings(payload: dict | None, base_config: dict | None = None) -> dict:
    effective_config = dict(base_config or load_config())
    incoming = dict(payload) if isinstance(payload, dict) else {}

    bool_fields = {'COOKIECLOUD_ENABLED', 'COOKIECLOUD_ALLOW_PLAINTEXT_EXPORT'}
    text_fields = {
        'COOKIECLOUD_SERVER_URL',
        'COOKIECLOUD_UUID',
        'COOKIECLOUD_PASSWORD',
        'COOKIECLOUD_CRYPTO_TYPE',
        'YOUTUBE_COOKIES_PATH',
    }

    for key in bool_fields:
        if key in incoming:
            effective_config[key] = _coerce_checkbox_value(incoming.get(key))

    for key in text_fields:
        if key in incoming:
            value = str(incoming.get(key) or '').strip()
            if key == 'COOKIECLOUD_PASSWORD' and not value:
                continue
            effective_config[key] = value

    return effective_config


def _cookiecloud_operation_error_message(action: str, retry_later: bool = False) -> str:
    action_key = str(action or '').strip().lower()
    if action_key == 'test':
        return 'CookieCloud 连接测试失败，请稍后重试。' if retry_later else 'CookieCloud 连接测试失败，请检查配置后重试。'
    if action_key == 'sync':
        return 'CookieCloud 立即拉取失败，请稍后重试。' if retry_later else 'CookieCloud 立即拉取失败，请检查配置后重试。'
    return 'CookieCloud 操作失败，请稍后重试。' if retry_later else 'CookieCloud 操作失败，请检查配置后重试。'


def _remember_cookiecloud_sync_result(success: bool, message: str):
    status = 'success' if success else 'error'
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    try:
        update_config({
            'COOKIECLOUD_LAST_SYNC_AT': timestamp,
            'COOKIECLOUD_LAST_SYNC_STATUS': status,
            'COOKIECLOUD_LAST_SYNC_MESSAGE': str(message or '').strip(),
        })
    except Exception as e:
        logger.warning(f'记录 CookieCloud 最近同步状态失败: {e}')
    return timestamp


def _get_request_ip_address() -> str:
    forwarded_for = request.headers.get('X-Forwarded-For', '')
    if forwarded_for:
        return forwarded_for.split(',')[0].strip()
    return str(request.remote_addr or 'unknown').strip() or 'unknown'


def _emit_login_event(event_type: str, payload: dict):
    emit_notification_event(NotificationEvent(event_type=event_type, payload=payload))


def _cleanup_bilibili_qr_sessions():
    now_ts = time.time()
    with _BILIBILI_QR_SESSION_LOCK:
        stale_ids = []
        for sid, item in _BILIBILI_QR_SESSIONS.items():
            created_at = float(item.get('created_at', 0) or 0)
            if now_ts - created_at > _BILIBILI_QR_SESSION_TTL_SECONDS:
                stale_ids.append(sid)
        for sid in stale_ids:
            _BILIBILI_QR_SESSIONS.pop(sid, None)


def _create_bilibili_qr_session(account_id=None, proxy="", trust_env=True):
    _cleanup_bilibili_qr_sessions()
    session_id = str(uuid.uuid4())
    session_obj = BilibiliQrLoginSession(proxy=proxy, trust_env=trust_env)
    with _BILIBILI_QR_SESSION_LOCK:
        _BILIBILI_QR_SESSIONS[session_id] = {
            'created_at': time.time(),
            'session': session_obj,
            'account_id': account_id,
            'success_notified': False,
            'failure_notified': False,
        }
    return session_id, session_obj


def _get_bilibili_qr_record(session_id: str):
    if not session_id:
        return None
    _cleanup_bilibili_qr_sessions()
    with _BILIBILI_QR_SESSION_LOCK:
        item = _BILIBILI_QR_SESSIONS.get(session_id)
    return item


def _get_bilibili_qr_session(session_id: str):
    item = _get_bilibili_qr_record(session_id)
    if not item:
        return None
    return item.get('session')


def _cleanup_acfun_qr_sessions():
    now_ts = time.time()
    with _ACFUN_QR_SESSION_LOCK:
        stale_ids = []
        for sid, item in _ACFUN_QR_SESSIONS.items():
            created_at = float(item.get('created_at', 0) or 0)
            if now_ts - created_at > _ACFUN_QR_SESSION_TTL_SECONDS:
                stale_ids.append(sid)
        for sid in stale_ids:
            _ACFUN_QR_SESSIONS.pop(sid, None)


def _create_acfun_qr_session():
    _cleanup_acfun_qr_sessions()
    session_id = str(uuid.uuid4())
    session_obj = AcfunQrLoginSession()
    with _ACFUN_QR_SESSION_LOCK:
        _ACFUN_QR_SESSIONS[session_id] = {
            'created_at': time.time(),
            'session': session_obj,
            'success_notified': False,
            'failure_notified': False,
        }
    return session_id, session_obj


def _get_acfun_qr_session(session_id: str):
    if not session_id:
        return None
    _cleanup_acfun_qr_sessions()
    with _ACFUN_QR_SESSION_LOCK:
        item = _ACFUN_QR_SESSIONS.get(session_id)
    if not item:
        return None
    return item.get('session')


def _mark_qr_notification_sent(session_store: dict, lock: threading.Lock, session_id: str, success: bool) -> bool:
    flag_name = 'success_notified' if success else 'failure_notified'
    with lock:
        item = session_store.get(session_id)
        if not item or item.get(flag_name):
            return False
        item[flag_name] = True
        return True


def _emit_qr_login_event_once(
    session_store: dict,
    lock: threading.Lock,
    session_id: str,
    platform: str,
    status_data: dict,
):
    status = str((status_data or {}).get('status') or '').strip().lower()
    if status not in ('done', 'failed'):
        return
    is_success = status == 'done'
    if not _mark_qr_notification_sent(session_store, lock, session_id, is_success):
        return
    _emit_login_event(
        EVENT_QR_LOGIN_SUCCESS if is_success else EVENT_QR_LOGIN_FAILED,
        {
            'platform': platform,
            'message': str((status_data or {}).get('message') or ('Cookies 已保存' if is_success else '登录失败')).strip(),
        }
    )


_SETTINGS_SAVE_OPERATIONS = {}
_SETTINGS_SAVE_LOCK = threading.Lock()
_SETTINGS_SAVE_TTL_SECONDS = 600
_MONITOR_RUN_OPERATIONS = {}
_MONITOR_RUN_LOCK = threading.Lock()
_MONITOR_RUN_TTL_SECONDS = 600


def _new_settings_save_state(operation_id: str) -> dict:
    now_ts = time.time()
    return {
        'operation_id': operation_id,
        'stage': 'saving_config',
        'message': '正在准备保存设置',
        'detail': '正在提交保存任务，请稍候。',
        'percent': None,
        'downloaded_bytes': None,
        'total_bytes': None,
        'done': False,
        'level': 'info',
        'success': None,
        'messages': [],
        'created_at': now_ts,
        'updated_at': now_ts,
        'expires_at': None,
    }


def _cleanup_settings_save_operations():
    now_ts = time.time()
    with _SETTINGS_SAVE_LOCK:
        stale_ids = []
        for operation_id, state in _SETTINGS_SAVE_OPERATIONS.items():
            expires_at = state.get('expires_at')
            if expires_at and now_ts >= float(expires_at):
                stale_ids.append(operation_id)
        for operation_id in stale_ids:
            _SETTINGS_SAVE_OPERATIONS.pop(operation_id, None)


def _update_settings_save_progress(operation_id: str, **fields) -> dict:
    _cleanup_settings_save_operations()
    with _SETTINGS_SAVE_LOCK:
        state = dict(_SETTINGS_SAVE_OPERATIONS.get(operation_id) or _new_settings_save_state(operation_id))
        state.update(fields)
        state['updated_at'] = time.time()
        if state.get('done'):
            state['expires_at'] = state['updated_at'] + _SETTINGS_SAVE_TTL_SECONDS
        _SETTINGS_SAVE_OPERATIONS[operation_id] = state
        return dict(state)


def _get_settings_save_progress(operation_id: str):
    _cleanup_settings_save_operations()
    with _SETTINGS_SAVE_LOCK:
        state = _SETTINGS_SAVE_OPERATIONS.get(operation_id)
        return dict(state) if state else None


def _new_monitor_run_state(operation_id: str, config_id: int) -> dict:
    now_ts = time.time()
    return {
        'operation_id': operation_id,
        'config_id': config_id,
        'message': '监控任务已创建',
        'detail': '正在后台执行 YouTube 监控，请稍候。',
        'done': False,
        'level': 'info',
        'success': None,
        'created_at': now_ts,
        'updated_at': now_ts,
        'expires_at': None,
    }


def _cleanup_monitor_run_operations():
    now_ts = time.time()
    with _MONITOR_RUN_LOCK:
        stale_ids = []
        for operation_id, state in _MONITOR_RUN_OPERATIONS.items():
            expires_at = state.get('expires_at')
            if expires_at and now_ts >= float(expires_at):
                stale_ids.append(operation_id)
        for operation_id in stale_ids:
            _MONITOR_RUN_OPERATIONS.pop(operation_id, None)


def _update_monitor_run_progress(operation_id: str, config_id: int, **fields) -> dict:
    _cleanup_monitor_run_operations()
    with _MONITOR_RUN_LOCK:
        state = dict(_MONITOR_RUN_OPERATIONS.get(operation_id) or _new_monitor_run_state(operation_id, config_id))
        state.update(fields)
        state['config_id'] = config_id
        state['updated_at'] = time.time()
        if state.get('done'):
            state['expires_at'] = state['updated_at'] + _MONITOR_RUN_TTL_SECONDS
        _MONITOR_RUN_OPERATIONS[operation_id] = state
        return dict(state)


def _get_monitor_run_progress(operation_id: str):
    _cleanup_monitor_run_operations()
    with _MONITOR_RUN_LOCK:
        state = _MONITOR_RUN_OPERATIONS.get(operation_id)
        return dict(state) if state else None


def _finalize_monitor_run_operation(operation_id: str, config_id: int, success: bool, message: str, detail: str = ''):
    _update_monitor_run_progress(
        operation_id,
        config_id,
        message=message,
        detail=detail or message,
        done=True,
        level='success' if success else 'error',
        success=success,
    )


def _run_monitor_operation(operation_id: str, config_id: int):
    try:
        success, message = youtube_monitor.run_monitor(config_id)
    except Exception as exc:
        logger.exception("后台执行 YouTube 监控失败，配置ID: %s", config_id)
        success = False
        message = f"监控失败: {exc}"

    detail = '监控记录已更新，可刷新页面查看最新结果。' if success else message
    _finalize_monitor_run_operation(operation_id, config_id, success, message, detail)


def _start_monitor_run_operation(config_id: int):
    config = youtube_monitor.get_monitor_config(config_id)
    if not config:
        return None, None, "监控配置不存在"

    operation_id = str(uuid.uuid4())
    _update_monitor_run_progress(
        operation_id,
        config_id,
        message=f"已启动监控任务：{config['name']}",
        detail='正在后台执行 YouTube 监控，请稍候。',
        done=False,
        level='info',
        success=None,
    )

    monitor_thread = threading.Thread(
        target=_run_monitor_operation,
        args=(operation_id, config_id),
        daemon=True,
        name=f'youtube-monitor-run-{config_id}-{operation_id[:8]}'
    )
    monitor_thread.start()
    return operation_id, config, None


def _append_settings_message(messages: list, category: str, text: str):
    clean_text = str(text or '').strip()
    if not clean_text:
        return
    messages.append({'category': category, 'text': clean_text})


def _get_task_dir_real(task_id: str) -> str:
    downloads_dir_real = os.path.realpath(get_app_subdir('downloads'))
    try:
        normalized_task_id = str(uuid.UUID(str(task_id or '').strip()))
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError('非法任务目录') from exc

    safe_task_dir = safe_join(downloads_dir_real, normalized_task_id)
    if not safe_task_dir:
        raise ValueError('非法任务目录')
    task_dir_real = os.path.realpath(safe_task_dir)
    if os.path.commonpath([downloads_dir_real, task_dir_real]) != downloads_dir_real:
        raise ValueError('非法任务目录')
    return task_dir_real


def _safe_join_task_dir(task_dir_real: str, *parts: str) -> str | None:
    try:
        safe_path = safe_join(task_dir_real, *[str(part) for part in parts])
        if not safe_path:
            return None
        file_real = os.path.realpath(safe_path)
        if os.path.commonpath([task_dir_real, file_real]) != task_dir_real:
            return None
        return file_real
    except (ValueError, OSError):
        return None


TITLE_MATCH_MODES = ('any', 'all')


def _normalize_title_match_mode(value) -> str:
    """规范化标题关键词匹配方式，非法值回退 any（Issue #158）。"""
    mode = str(value or '').strip().lower()
    return mode if mode in TITLE_MATCH_MODES else 'any'


def _get_cover_file_info(path: str):
    ext = os.path.splitext(str(path or ''))[1].lower()
    return ext, ALLOWED_COVER_EXTENSIONS.get(ext)


def _validate_cover_upload(file_storage):
    if not file_storage or not getattr(file_storage, 'filename', ''):
        raise ValueError('请选择要上传的封面图片')

    ext, _ = _get_cover_file_info(file_storage.filename)
    if ext not in ALLOWED_COVER_EXTENSIONS:
        raise ValueError('仅支持 JPG、JPEG、PNG、WEBP 格式的封面图片')

    current_pos = file_storage.stream.tell()
    try:
        with Image.open(file_storage.stream) as img:
            img.verify()
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise ValueError(f'上传文件不是有效图片: {exc}') from exc
    finally:
        file_storage.stream.seek(current_pos)

    return ext


def _find_original_cover_backup(task_dir_real: str):
    for ext in ALLOWED_COVER_EXTENSIONS:
        candidate = _safe_join_task_dir(task_dir_real, f'original_cover{ext}')
        if candidate and os.path.exists(candidate):
            return candidate
    return None


def _get_current_cover_path(task: dict, task_dir_real: str):
    cover_path = str(task.get('cover_path_local') or '').strip()
    if cover_path:
        candidate = _safe_join_task_dir(task_dir_real, os.path.basename(cover_path))
        if candidate and os.path.exists(candidate):
            return candidate

    for name in ('cover.jpg', 'cover.png', 'cover.webp', 'thumbnail.jpg', 'thumbnail.png', 'thumbnail.webp'):
        candidate = _safe_join_task_dir(task_dir_real, name)
        if candidate and os.path.exists(candidate):
            return candidate

    if os.path.isdir(task_dir_real):
        with os.scandir(task_dir_real) as entries:
            for entry in entries:
                if not entry.is_file():
                    continue
                if entry.name.lower().endswith(tuple(ALLOWED_COVER_EXTENSIONS.keys())):
                    candidate = _safe_join_task_dir(task_dir_real, entry.name)
                    if candidate and os.path.exists(candidate):
                        return candidate

    return ''


def _replace_task_cover(task: dict, uploaded_file):
    task_id = str(task.get('id') or '').strip()
    if not task_id:
        raise ValueError('任务不存在')

    task_dir_real = _get_task_dir_real(task_id)
    os.makedirs(task_dir_real, exist_ok=True)

    current_cover_path = _get_current_cover_path(task, task_dir_real)

    ext = _validate_cover_upload(uploaded_file)
    original_backup = _find_original_cover_backup(task_dir_real)

    if not original_backup and current_cover_path:
        current_ext, _ = _get_cover_file_info(current_cover_path)
        if current_ext not in ALLOWED_COVER_EXTENSIONS:
            raise ValueError('当前原始封面格式不受支持，无法创建恢复备份')
        original_backup = _safe_join_task_dir(task_dir_real, f'original_cover{current_ext}')
        if not original_backup:
            raise ValueError('无法创建原始封面备份路径')
        shutil.copy2(current_cover_path, original_backup)

    for existing_ext in ALLOWED_COVER_EXTENSIONS:
        custom_candidate = _safe_join_task_dir(task_dir_real, f'custom_cover{existing_ext}')
        if custom_candidate and os.path.exists(custom_candidate):
            os.remove(custom_candidate)

    new_cover_path = _safe_join_task_dir(task_dir_real, f'custom_cover{ext}')
    if not new_cover_path:
        raise ValueError('无法创建封面保存路径')
    uploaded_file.save(new_cover_path)
    update_task(task_id, cover_path_local=new_cover_path, silent=True)
    return new_cover_path


def _restore_task_cover(task: dict):
    task_id = str(task.get('id') or '').strip()
    if not task_id:
        raise ValueError('任务不存在')

    task_dir_real = _get_task_dir_real(task_id)
    if not os.path.isdir(task_dir_real):
        raise ValueError('任务目录不存在，无法恢复原封面')

    original_backup = _find_original_cover_backup(task_dir_real)
    if not original_backup:
        raise ValueError('未找到原始封面备份，无法恢复')

    update_task(task_id, cover_path_local=original_backup, silent=True)
    return original_backup


def _is_ajax_request() -> bool:
    requested_with = request.headers.get('X-Requested-With', '')
    accept_header = request.headers.get('Accept', '')
    return requested_with == 'XMLHttpRequest' or 'application/json' in accept_header


_ACTION_FLASH_CATEGORIES = {'success', 'info', 'warning', 'danger'}


def _settings_form_value(value):
    """把配置值收成表单控件能直接写回的 JSON 值。"""
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, (list, tuple)):
        return ','.join(str(item) for item in value)
    if isinstance(value, str):
        return value
    return str(value)


def _respond_page_action(
    message,
    category='info',
    *,
    endpoint=None,
    endpoint_kwargs=None,
    status=None,
    extra=None,
    success=None,
    navigate=False,
):
    """同页操作用 JSON；普通表单提交仍 flash 后重定向。

    navigate=True 表示这次结果本来就要离开当前页（例如登录后进入首页，
    或编辑页保存并上传后进入人工审核）。此时 AJAX 也会带上 redirect，
    并先写入 flash，让目标页还能看到原来的提示。
    """
    category = category if category in _ACTION_FLASH_CATEGORIES else 'info'
    if success is None:
        success = category != 'danger'
    if status is None:
        status = 200 if success else 400
    target = url_for(endpoint, **(endpoint_kwargs or {})) if endpoint else None
    if _is_ajax_request():
        payload = {
            'success': bool(success),
            'message': message,
            'category': category,
        }
        if extra:
            for key, value in extra.items():
                if key not in ('success', 'message', 'category', 'redirect'):
                    payload[key] = value
        if navigate and target:
            flash(message, category)
            payload['redirect'] = target
        return jsonify(payload), status
    flash(message, category)
    return redirect(target or request.referrer or url_for('index'))


def _task_mutation_extra(task_id, *, removed=False) -> dict:
    extra = {'task_id': task_id, 'removed': bool(removed)}
    if removed or not _is_ajax_request():
        return extra
    try:
        task = get_task(task_id)
        if not task:
            extra['removed'] = True
            return extra
        extra.update(_render_task_fragments(task))
        extra['task_id'] = task_id
        extra['removed'] = False
    except Exception:
        logger.exception("渲染任务片段失败: %s", task_id)
    return extra


def _edit_task_client_state(task: dict) -> dict:
    """编辑页就地更新所需的状态，不重新渲染整页。"""
    task_id = str(task.get('id') or '').strip()
    has_original_cover_backup = False
    has_cover_preview = False
    is_custom_cover_active = False
    current_cover_filename = ''
    try:
        task_dir_real = _get_task_dir_real(task_id)
        has_original_cover_backup = bool(os.path.isdir(task_dir_real) and _find_original_cover_backup(task_dir_real))
        active_cover_path = _get_current_cover_path(task, task_dir_real) if os.path.isdir(task_dir_real) else ''
        has_cover_preview = bool(active_cover_path)
        current_cover_filename = os.path.basename(active_cover_path) if active_cover_path else ''
        is_custom_cover_active = current_cover_filename.startswith('custom_cover.')
    except Exception:
        has_original_cover_backup = False
        has_cover_preview = bool(task.get('cover_path_local'))
        is_custom_cover_active = False
        current_cover_filename = os.path.basename(str(task.get('cover_path_local') or ''))

    if has_original_cover_backup and is_custom_cover_active:
        cover_hint = '已检测到原始封面备份，可恢复。'
    elif has_original_cover_backup:
        cover_hint = '原始封面备份已存在，当前已在使用原始封面。'
    else:
        cover_hint = '上传新封面后会自动保留原始封面，之后可随时恢复。'

    can_upload = task.get('status') in [
        TASK_STATES['COMPLETED'],
        TASK_STATES['PENDING'],
        TASK_STATES['READY_FOR_UPLOAD'],
        TASK_STATES['AWAITING_REVIEW'],
    ]
    cover_url = ''
    if task_id and has_cover_preview:
        try:
            cover_url = url_for('get_task_cover', task_id=task_id)
        except Exception:
            cover_url = ''

    return {
        'task_id': task_id,
        'status': task.get('status'),
        'status_label': task_status_display(task.get('status')),
        'status_color': task_status_color(task.get('status')),
        'can_upload': can_upload,
        'has_cover_preview': has_cover_preview,
        'has_original_cover_backup': has_original_cover_backup,
        'is_custom_cover_active': is_custom_cover_active,
        'current_cover_filename': current_cover_filename,
        'cover_hint': cover_hint,
        'cover_url': cover_url,
        'error_message': task.get('error_message') or '',
    }


def _extract_settings_uploads(files_storage) -> dict:
    uploads = {}
    for field_name in ('youtube_cookies_file', 'acfun_cookies_file', 'bilibili_cookies_file'):
        file_storage = files_storage.get(field_name)
        if not file_storage or not getattr(file_storage, 'filename', ''):
            continue
        uploads[field_name] = {
            'filename': file_storage.filename,
            'content': file_storage.read()
        }
    return uploads


def _persist_settings_uploads(form_data: dict, uploads: dict):
    cookies_dir = get_app_subdir('cookies')
    os.makedirs(cookies_dir, exist_ok=True)

    file_specs = {
        'youtube_cookies_file': ('yt_cookies.txt', 'YOUTUBE_COOKIES_PATH', 'cookies/yt_cookies.txt', 'YouTube'),
        'acfun_cookies_file': ('ac_cookies.json', 'ACFUN_COOKIES_PATH', 'cookies/ac_cookies.json', 'AcFun'),
        'bilibili_cookies_file': ('bili_cookies.json', 'BILIBILI_COOKIES_PATH', 'cookies/bili_cookies.json', 'Bilibili'),
    }

    for field_name, payload in uploads.items():
        spec = file_specs.get(field_name)
        if not spec or not payload.get('filename'):
            continue
        save_name, config_key, relative_path, service_name = spec
        target_path = os.path.join(cookies_dir, save_name)
        with open(target_path, 'wb') as target_file:
            target_file.write(payload.get('content') or b'')
        form_data[config_key] = relative_path
        logger.info(f"{service_name} cookies文件已上传并保存到: {target_path}")


def _build_settings_progress_reporter(operation_id: str | None):
    if not operation_id:
        return None

    def _report(payload: dict):
        _update_settings_save_progress(
            operation_id,
            stage=payload.get('stage', 'saving_config'),
            message=payload.get('message', ''),
            detail=payload.get('detail', ''),
            percent=payload.get('percent'),
            downloaded_bytes=payload.get('downloaded_bytes'),
            total_bytes=payload.get('total_bytes'),
            level=payload.get('level', 'info')
        )

    return _report


# 设置页表单字段白名单。
# 必须与 templates/settings.html 中的控件保持一致：
#   - 复选框若不在表单数据里就会被当作未勾选，所以"UI 有开关但漏登记"会让该开关永远无法关闭；
#     反之"登记了但 UI 没有控件"会让该键在每次保存时被强制写成 off。
#   - 数值字段用于类型归一化，非法值回退到 DEFAULT_CONFIG 中的默认值。
# tests/test_settings_template_layout.py 会断言模板控件与这些集合一致。
SETTINGS_CHECKBOX_FIELDS = list(dict.fromkeys([
    'AUTO_MODE_ENABLED', 'TRANSLATE_TITLE', 'TRANSLATE_DESCRIPTION',
    'UPLOAD_APPEND_REPOST_NOTICE', 'DELETE_DOWNLOAD_FILES_AFTER_UPLOAD',
    'GENERATE_TAGS', 'YOUTUBE_UPLOADER_AS_FIRST_TAG', 'RECOMMEND_PARTITION',
    'PRESET_TAGS_ENABLED',
    'RECOMMEND_PARTITION_WITH_COVER', 'CONTENT_MODERATION_ENABLED',
    'CONTENT_MODERATION_COVER_ENABLED',
    'OPENAI_THINKING_ENABLED', 'SUBTITLE_OPENAI_THINKING_ENABLED', 'SUBTITLE_QC_THINKING_ENABLED',
    'LOG_CLEANUP_ENABLED', 'DOWNLOAD_CLEANUP_ENABLED',
    'SUBTITLE_TRANSLATION_ENABLED', 'SUBTITLE_EMBED_IN_VIDEO',
    'SUBTITLE_KEEP_ORIGINAL', 'YOUTUBE_AUTO_GENERATED_SUBTITLES_ENABLED',
    'YOUTUBE_PROXY_ENABLED', 'YOUTUBE_API_PROXY_ENABLED', 'password_protection_enabled',
    'SPEECH_RECOGNITION_ENABLED',
    'VAD_ENABLED',
    'SUBTITLE_NORMALIZE_PUNCTUATION', 'SUBTITLE_FILTER_FILLER_WORDS',
    'SUBTITLE_TIME_OFFSET_ENABLED', 'SUBTITLE_MIN_CUE_DURATION_ENABLED',
    'SUBTITLE_MERGE_GAP_ENABLED', 'SUBTITLE_MIN_TEXT_LENGTH_ENABLED',
    'SUBTITLE_MAX_LINE_LENGTH_ENABLED', 'SUBTITLE_MAX_LINES_ENABLED',
    'SUBTITLE_QC_ENABLED',
    'SUBTITLE_TEXT_BOLD', 'SUBTITLE_OUTLINE_ENABLED', 'SUBTITLE_SHADOW_ENABLED',
    'SUBTITLE_BACKGROUND_ENABLED', 'VIDEO_HW_QUALITY_BOOST',
    'FFMPEG_AUTO_DOWNLOAD', 'WHISPER_TRANSLATE',
    'VIDEO_CUSTOM_PARAMS_ENABLED',
    'VOXTRAL_DIARIZE', 'VOXTRAL_ENFORCE_MAX_DURATION',
    'NOTIFY_ENABLED',
    'NOTIFY_EVENT_TASK_ADDED',
    'NOTIFY_EVENT_TASK_COMPLETED',
    'NOTIFY_EVENT_TASK_FAILED',
    'NOTIFY_EVENT_LOGIN_SUCCESS',
    'NOTIFY_EVENT_LOGIN_LOCKED',
    'NOTIFY_EVENT_QR_LOGIN_SUCCESS',
    'NOTIFY_EVENT_QR_LOGIN_FAILED',
    'NOTIFY_WECOM_ENABLED',
    'NOTIFY_SERVERCHAN_ENABLED',
    'NOTIFY_MESSAGE_PUSHER_ENABLED',
    'COOKIECLOUD_ENABLED',
    'COOKIECLOUD_ALLOW_PLAINTEXT_EXPORT',
] + SPEECH_PIPELINE_CHECKBOXES))

SETTINGS_INT_FIELDS = list(dict.fromkeys([
    'MAX_CONCURRENT_TASKS', 'MAX_CONCURRENT_UPLOADS',
    'BILIBILI_PREUPLOAD_COOLDOWN_MINUTES',
    'BILIBILI_SUBMIT_COOLDOWN_MINUTES',
    'BILIBILI_RISK_COOLDOWN_MINUTES',
    'LOG_CLEANUP_HOURS', 'LOG_CLEANUP_INTERVAL',
    'DOWNLOAD_CLEANUP_HOURS', 'DOWNLOAD_CLEANUP_INTERVAL',
    'BILIBILI_PREUPLOAD_COOLDOWN_MINUTES', 'BILIBILI_SUBMIT_COOLDOWN_MINUTES',
    'DOWNLOAD_DISK_FREE_THRESHOLD_GB',
    'SUBTITLE_BATCH_SIZE', 'SUBTITLE_MAX_RETRIES',
    'SUBTITLE_RETRY_DELAY', 'SUBTITLE_MAX_WORKERS', 'YOUTUBE_DOWNLOAD_THREADS',
    'YOUTUBE_DOWNLOAD_MAX_HEIGHT',
    'LOGIN_MAX_FAILED_ATTEMPTS', 'LOGIN_LOCKOUT_MINUTES', 'LOGIN_SESSION_TIMEOUT_MINUTES',
    'AI_FAILOVER_TIMEOUT_SECONDS',
    'VAD_SILERO_MIN_SPEECH_MS', 'VAD_SILERO_MIN_SILENCE_MS', 'VAD_SILERO_MAX_SPEECH_S',
    'VAD_SILERO_SPEECH_PAD_MS',
    'SUBTITLE_QC_SAMPLE_MAX_ITEMS', 'SUBTITLE_QC_MAX_CHARS',
    'SUBTITLE_QC_TIMEOUT_SECONDS',
    'SUBTITLE_MIN_TEXT_LENGTH',
    'WHISPER_MAX_WORKERS', 'WHISPER_MAX_RETRIES',
] + list(SPEECH_PIPELINE_INT_FIELDS)))

SETTINGS_FLOAT_FIELDS = list(dict.fromkeys([
    'VAD_SILERO_THRESHOLD',
    'SUBTITLE_TIME_OFFSET_S', 'SUBTITLE_MIN_CUE_DURATION_S', 'SUBTITLE_MERGE_GAP_S',
    'SUBTITLE_QC_THRESHOLD',
    'WHISPER_RETRY_DELAY_S', 'AUDIO_CHUNK_WINDOW_S', 'AUDIO_CHUNK_OVERLAP_S',
    'VAD_MERGE_GAP_S', 'VAD_MIN_SEGMENT_S', 'VAD_MAX_SEGMENT_S_FOR_SPLIT',
    'SUBTITLE_FONT_SIZE_SCALE', 'SUBTITLE_MARGIN_V_SCALE',
    'SUBTITLE_OUTLINE_SCALE', 'SUBTITLE_SHADOW_SCALE',
    'SUBTITLE_BACKGROUND_OPACITY', 'VIDEO_QUALITY_VALUE',
] + list(SPEECH_PIPELINE_FLOAT_FIELDS)))

# 同一个键不得同时出现在整数与浮点白名单里：整数分支先执行，会把 "15.5"
# 判为非法并回退成默认值，随后浮点分支再处理，导致小数精度被静默丢弃。
# 用显式 raise 而非 assert：assert 在 python -O 下会被整体剥离，这条不变量
# 就会悄悄失效。此处也不另写守卫测试——一旦交叉，import 期即抛错，
# 测试文件根本收集不到，那种测试永远不可能失败。错误信息里带冲突键名。
_NUMERIC_WHITELIST_OVERLAP = sorted(set(SETTINGS_INT_FIELDS) & set(SETTINGS_FLOAT_FIELDS))
if _NUMERIC_WHITELIST_OVERLAP:
    raise RuntimeError(f'数值白名单存在交叉：{_NUMERIC_WHITELIST_OVERLAP}')

# 此前 SUBTITLE_MAX_LINE_LENGTH / SUBTITLE_MAX_LINES 在模板中用 hidden 钉死为
# 999 / 1，与 DEFAULT_CONFIG 的 42 / 2 分叉：全新安装（从未保存过设置页）用 42/2，
# 一旦保存过设置页就变成「永不换行的单行字幕」，同一版本出现两种换行格式。
# 现已改为设置页真实控件，钉死表保留为空以维持 _settings_fallback_default 的
# 统一语义（回退值一律取 DEFAULT_CONFIG）。
_PINNED_SETTINGS_DEFAULTS = {}

# 数值范围校验表：(键, 下限, 上限)。表里有两类键，都有防御价值：
# 1. 设置页有数值控件的键 —— 浏览器原生校验只在「值违反控件 min/max」时挡住提交，
#    而多数控件只写了 min 没写 max（分片窗口、最大片段、语音覆盖率等），
#    直接 POST、旧标签页或脚本提交都能绕过，服务端必须再拦一次；
# 2. 设置页没有控件的键（VAD_MIN_SEGMENT_S / WHISPER_* / SUBTITLE_QC_* 等）——
#    它们没有 UI 入口，但 /settings 用 request.form.to_dict() 接收任意键名，
#    键集合不受模板限制，同一个 POST 依然能写坏配置
#    （SUBTITLE_QC_MIN_COVERAGE_RATIO > 1 会让质检覆盖率维度恒失败、字幕永不烧录）。
# 越界或非法值一律回退 DEFAULT_CONFIG，并汇总成一条用户可见的提示，
# 见 _format_settings_revert_message —— 不允许「值被改掉却提示保存成功」。
SETTINGS_RANGE_GUARDS = (
    ('VAD_MAX_SEGMENT_S', 10.0, 120.0),      # 下限与设置页控件 min=10 对齐
    ('AUDIO_CHUNK_WINDOW_S', 5.0, 120.0),    # 下限与设置页控件 min=5 对齐
    ('AUDIO_CHUNK_OVERLAP_S', 0.0, 5.0),
    ('VAD_MIN_SEGMENT_S', 0.0, 5.0),
    ('VAD_MERGE_GAP_S', 0.0, 5.0),
    ('VAD_MAX_SEGMENT_S_FOR_SPLIT', 5.0, 120.0),
    ('VAD_MIN_SPEECH_COVERAGE_RATIO', 0.0, 1.0),
    ('SUBTITLE_MAX_LINE_LENGTH', 10.0, 200.0),
    ('SUBTITLE_MAX_LINES', 1.0, 5.0),
    ('SUBTITLE_MAX_CUE_DURATION_S', 1.0, 30.0),
    ('SUBTITLE_MAX_CPS', 5.0, 60.0),
    ('SUBTITLE_TRANSLATION_MAX_CHARS_PER_BATCH', 200.0, 20000.0),
    ('WHISPER_TEMPERATURE', 0.0, 1.0),
    ('WHISPER_NO_SPEECH_THRESHOLD', 0.0, 1.0),
    # 质检时间轴阈值：modules/subtitle_qc.py 直接采信这三个键，超出物理含义的值
    # （覆盖率比值 > 1、负的空档秒数）会让整条质检线恒失败，必须挡在配置层。
    ('SUBTITLE_QC_MIN_COVERAGE_RATIO', 0.0, 1.0),
    ('SUBTITLE_QC_MAX_GAP_S', 1.0, 3600.0),
    ('SUBTITLE_QC_MAX_CPS', 1.0, 200.0),
    # 烧录字幕外观（倍率型，越界回退默认）
    ('SUBTITLE_FONT_SIZE_SCALE', 0.5, 2.0),
    ('SUBTITLE_MARGIN_V_SCALE', 0.5, 2.0),
    ('SUBTITLE_OUTLINE_SCALE', 0.0, 3.0),
    ('SUBTITLE_SHADOW_SCALE', 0.0, 3.0),
    ('SUBTITLE_BACKGROUND_OPACITY', 0.0, 1.0),
    # 编码质量（manual 模式下的 CRF/CQ/QP）
    ('VIDEO_QUALITY_VALUE', 0.0, 51.0),
    ('BILIBILI_PREUPLOAD_COOLDOWN_MINUTES', 1.0, 1440.0),
    ('BILIBILI_SUBMIT_COOLDOWN_MINUTES', 1.0, 1440.0),
    ('BILIBILI_RISK_COOLDOWN_MINUTES', 1.0, 1440.0),
    ('DOWNLOAD_DISK_FREE_THRESHOLD_GB', 1.0, 10240.0),
)

# 回退提示里的中文名称（取自设置页控件文案），仅用于让用户看懂被改了哪一项。
SETTINGS_GUARD_LABELS = {
    'VAD_MAX_SEGMENT_S': '最大片段（秒）',
    'AUDIO_CHUNK_WINDOW_S': '分片窗口（秒）',
    'AUDIO_CHUNK_OVERLAP_S': '分片重叠（秒）',
    'VAD_MIN_SEGMENT_S': '最短语音段（秒）',
    'VAD_MERGE_GAP_S': '语音段合并间隔（秒）',
    'VAD_MAX_SEGMENT_S_FOR_SPLIT': '切片上限（秒）',
    'VAD_MIN_SPEECH_COVERAGE_RATIO': '最低语音覆盖率',
    'SUBTITLE_MAX_LINE_LENGTH': '每行最大字符数',
    'SUBTITLE_MAX_LINES': '最大行数',
    'SUBTITLE_MAX_CUE_DURATION_S': '单条字幕最长时长（秒）',
    'SUBTITLE_MAX_CPS': '字幕最大每秒字符数',
    'SUBTITLE_TRANSLATION_MAX_CHARS_PER_BATCH': '翻译单批最大字符数',
    'WHISPER_TEMPERATURE': 'Whisper 解码温度',
    'WHISPER_NO_SPEECH_THRESHOLD': 'Whisper 无语音判定阈值',
    'SUBTITLE_QC_MIN_COVERAGE_RATIO': '质检最低覆盖率',
    'SUBTITLE_QC_MAX_GAP_S': '质检最大空档（秒）',
    'SUBTITLE_QC_MAX_CPS': '质检最大每秒字符数',
    'AI_FAILOVER_TIMEOUT_SECONDS': 'AI 故障转移超时（秒）',
    'SUBTITLE_QC_TIMEOUT_SECONDS': '字幕质检接口超时（秒）',
    'SUBTITLE_MAX_LINE_LENGTH_ENABLED': '每行最大字符数开关',
    'SUBTITLE_MAX_LINES_ENABLED': '最大行数开关',
    # 烧录字幕外观（倍率型）
    'SUBTITLE_FONT_SIZE_SCALE': '字幕字号倍率',
    'SUBTITLE_MARGIN_V_SCALE': '字幕垂直边距倍率',
    'SUBTITLE_OUTLINE_SCALE': '字幕描边倍率',
    'SUBTITLE_SHADOW_SCALE': '字幕阴影倍率',
    'SUBTITLE_BACKGROUND_OPACITY': '字幕背景不透明度',
    # 编码质量
    'VIDEO_QUALITY_VALUE': '画质数值（CRF/CQ/QP）',
    'BILIBILI_PREUPLOAD_COOLDOWN_MINUTES': 'Bilibili 406 冷却（分钟）',
    'BILIBILI_SUBMIT_COOLDOWN_MINUTES': 'Bilibili 137022 冷却（分钟）',
    'BILIBILI_RISK_COOLDOWN_MINUTES': 'Bilibili 603 冷却（分钟）',
    'DOWNLOAD_DISK_FREE_THRESHOLD_GB': '下载磁盘阈值（GB）',
}


def _settings_fallback_default(field, fallback=1):
    """非法数值的回退值统一取 DEFAULT_CONFIG，避免与页面展示的默认值不一致。

    回退值一律来自 DEFAULT_CONFIG（_PINNED_SETTINGS_DEFAULTS 当前为空表，
    它一旦非空就会让某些键的回退值与 DEFAULT_CONFIG 分叉，历史上 999/1 的
    字幕换行就是这样写进来的）；DEFAULT_CONFIG 里没有的键才用 fallback 兜底。
    """
    if field in _PINNED_SETTINGS_DEFAULTS:
        return _PINNED_SETTINGS_DEFAULTS[field]
    if field in DEFAULT_CONFIG:
        return DEFAULT_CONFIG[field]
    return fallback


def _format_guard_bound(value) -> str:
    """把 guard 边界格式化成展示文本：整数边界不显示小数点（10 而不是 10.0）。"""
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return str(value)
    if numeric.is_integer():
        return str(int(numeric))
    return f'{numeric:g}'


def _format_guard_range(guard_min, guard_max) -> str:
    """把 guard 区间格式化成「下限–上限」文本。"""
    return f'{_format_guard_bound(guard_min)}–{_format_guard_bound(guard_max)}'


def _guard_bounds(key):
    """取 SETTINGS_RANGE_GUARDS 中某键的 (下限, 上限)；未登记时返回 (None, None)。"""
    for guard_key, guard_min, guard_max in SETTINGS_RANGE_GUARDS:
        if guard_key == key:
            return guard_min, guard_max
    return None, None


def _format_settings_revert_message(reverts: list) -> str:
    """汇总本轮回退项，生成用户可见的提示文本。

    每条包含：中文名（配置键）、提交值、最终生效值、允许范围 / 交叉约束说明。
    """
    parts = []
    for item in reverts:
        key = str(item.get('key') or '')
        label = SETTINGS_GUARD_LABELS.get(key, key)
        prefix = key if label == key else f'{label}（{key}）'
        text = f'{prefix}：提交 {item.get("submitted")} → 生效 {item.get("applied")}'
        if item.get('allowed'):
            text += f'，允许 {item["allowed"]}'
        if item.get('note'):
            text += f'；{item["note"]}'
        parts.append(text)
    if not parts:
        return ''
    return '以下设置项超出允许范围，已回退后再保存：' + '；'.join(parts) + '。'


def _perform_settings_save(form_data: dict, uploads: dict, operation_id: str | None = None) -> dict:
    form_data = dict(form_data or {})
    uploads = uploads or {}
    # 粘贴内容不进配置文件，也不进日志。
    cookie_paste = form_data.pop('YOUTUBE_COOKIES_PASTE', None)
    cookie_paste = str(cookie_paste or '').strip()
    messages = []
    progress_reporter = _build_settings_progress_reporter(operation_id)

    def report(stage: str, message: str, detail: str = '', percent=None, level: str = 'info', downloaded_bytes=None, total_bytes=None):
        if not progress_reporter:
            return
        progress_reporter({
            'stage': stage,
            'message': message,
            'detail': detail,
            'percent': percent,
            'downloaded_bytes': downloaded_bytes,
            'total_bytes': total_bytes,
            'level': level,
        })

    try:
        report('saving_config', '正在保存配置', '正在校验并写入设置。')
        form_data.pop('save_operation_id', None)

        # 本轮回退记录：键 / 提交值 / 生效值 / 允许范围。所有「值被改掉」的分支都要登记，
        # 保存成功后统一追加一条 warning 消息，避免用户只看到「配置已成功保存」。
        reverts: list = []
        revert_index: dict = {}
        guard_keys = {key for key, _, _ in SETTINGS_RANGE_GUARDS}

        def remember_revert(key, submitted, applied, allowed=None, note=''):
            """登记一次回退；同一键只保留首条提交值，生效值取最后一次。"""
            item = revert_index.get(key)
            if item is None:
                item = {
                    'key': key,
                    'submitted': submitted,
                    'applied': applied,
                    'allowed': allowed,
                    'note': note,
                }
                revert_index[key] = item
                reverts.append(item)
            else:
                item['applied'] = applied
                if allowed and not item.get('allowed'):
                    item['allowed'] = allowed
                if note and not item.get('note'):
                    item['note'] = note
            return item['applied']

        new_password = form_data.get('new_password')
        confirm_password = form_data.get('confirm_password')
        if new_password:
            if new_password == confirm_password:
                form_data['password'] = new_password
            else:
                _append_settings_message(messages, 'danger', '新密码两次输入不一致，密码未更新。')

        form_data.pop('new_password', None)
        form_data.pop('confirm_password', None)

        for checkbox in SETTINGS_CHECKBOX_FIELDS:
            if checkbox not in form_data:
                form_data[checkbox] = 'off'

        for field in AI_REASONING_EFFORT_KEYS:
            if field in form_data:
                submitted_effort = form_data[field]
                form_data[field] = normalize_reasoning_effort(submitted_effort)
                if form_data[field] != str(submitted_effort).strip().lower():
                    remember_revert(
                        field, submitted_effort, form_data[field],
                        allowed="auto / minimal / low / medium / high / xhigh",
                    )

        numeric_fields = SETTINGS_INT_FIELDS
        for field in numeric_fields:
            if field in form_data:
                try:
                    original_value = form_data[field]
                    normalized_value = int(original_value)
                    if field == 'LOGIN_SESSION_TIMEOUT_MINUTES':
                        normalized_value = max(1, normalized_value)
                    form_data[field] = str(normalized_value)
                except (ValueError, TypeError) as e:
                    logger.debug(f"整数转换失败 - field: {field}, value: {form_data[field]}, error: {e}")
                    # AI_FAILOVER_TIMEOUT_SECONDS 必须回退到 8：若落到 1，它会处于合法的
                    # 1–60 区间内，后续范围校验不会再纠正，稍慢的连接会被 1s 误判为宕机。
                    _submitted_int = form_data[field]
                    form_data[field] = str(_settings_fallback_default(field))
                    logger.debug(f"整数字段使用默认值 - field: {field}, value: {form_data[field]}")
                    # 非数字提交在归一化阶段就被改掉，范围校验根本看不到它，
                    # 所以对 guard 覆盖的键在这里补一条回退记录。
                    if field in guard_keys:
                        remember_revert(
                            field, _submitted_int, form_data[field],
                            allowed=_format_guard_range(*_guard_bounds(field)),
                            note='不是有效数值',
                        )

        # AI_FAILOVER_TIMEOUT_SECONDS 范围校验：设置页声明 1–60 秒。
        # 越界或非法（手工配置 / 直接 POST 负值）回退默认值，
        # 避免 httpx 在创建客户端 / 请求时抛 timeout range error。
        _fkey = 'AI_FAILOVER_TIMEOUT_SECONDS'
        if _fkey in form_data:
            try:
                _fv = int(str(form_data[_fkey]).strip())
            except (ValueError, TypeError):
                _fv = None
            if _fv is None or _fv < 1 or _fv > 60:
                _fkey_default = _settings_fallback_default(_fkey)
                logger.debug(f"{_fkey} 越界或非法，回退默认 {_fkey_default}: {form_data.get(_fkey)}")
                _submitted_fkey = form_data[_fkey]
                form_data[_fkey] = str(_fkey_default)
                remember_revert(_fkey, _submitted_fkey, form_data[_fkey], allowed='1–60')

        # SUBTITLE_QC_TIMEOUT_SECONDS 范围校验：设置页声明 10–600 秒。
        # 越界或非法（手工配置 / 直接 POST 负值 / 非数字）回退默认值，
        # 避免负值或超限值被透传给 httpx 客户端时抛 timeout range error。
        _qc_timeout_key = 'SUBTITLE_QC_TIMEOUT_SECONDS'
        if _qc_timeout_key in form_data:
            try:
                _qc_tv = int(str(form_data[_qc_timeout_key]).strip())
            except (ValueError, TypeError):
                _qc_tv = None
            if _qc_tv is None or _qc_tv < 10 or _qc_tv > 600:
                _qc_default = _settings_fallback_default(_qc_timeout_key)
                logger.debug(f"{_qc_timeout_key} 越界或非法，回退默认 {_qc_default}: {form_data.get(_qc_timeout_key)}")
                _submitted_qc_timeout = form_data[_qc_timeout_key]
                form_data[_qc_timeout_key] = str(_qc_default)
                remember_revert(
                    _qc_timeout_key, _submitted_qc_timeout, form_data[_qc_timeout_key],
                    allowed='10–600',
                )

        for field in SETTINGS_FLOAT_FIELDS:
            if field in form_data:
                try:
                    original_value = form_data[field]
                    if str(original_value).strip() == '':
                        raise ValueError('empty string')
                    form_data[field] = str(float(original_value))
                except (ValueError, TypeError) as e:
                    logger.debug(f"浮点数转换失败 - field: {field}, value: {form_data[field]}, error: {e}")
                    _submitted_float = form_data[field]
                    form_data[field] = str(float(_settings_fallback_default(field, 0.0)))
                    logger.debug(f"浮点字段使用默认值 - field: {field}, value: {form_data[field]}")
                    if field in guard_keys:
                        remember_revert(
                            field, _submitted_float, form_data[field],
                            allowed=_format_guard_range(*_guard_bounds(field)),
                            note='不是有效数值',
                        )

        # 表单可达数值键的范围校验 + 无控件数值键的防御性校验（表见 SETTINGS_RANGE_GUARDS）。
        # 关于前端：settings-form 没有 novalidate，保存按钮是 <button type="submit">，
        # 浏览器原生约束校验在 submit 事件派发之前执行（所以 event.preventDefault()
        # 并不会跳过它，有 max 的控件确实拦得住越界值；不要因为这段注释曾经写错就删掉
        # 模板里的 min/max）。真正的问题是覆盖面：多数控件只声明 min（分片窗口、最大片段、
        # 语音覆盖率），且原生校验只作用于「有控件的键」——直接 POST / 脚本 / 旧页面提交的
        # 键根本不在模板里，原生校验无从谈起。所以服务端必须独立拦一遍。
        # 越界或非法一律回退 DEFAULT_CONFIG 的默认值，并由 remember_revert 记录，
        # 保存结束后汇总成一条可见的 warning 消息。
        for _guard_key, _guard_min, _guard_max in SETTINGS_RANGE_GUARDS:
            if _guard_key not in form_data:
                continue
            _submitted_guard = form_data[_guard_key]
            try:
                _guard_value = float(str(_submitted_guard).strip())
            except (ValueError, TypeError):
                _guard_value = None
            if _guard_value is None or _guard_value != _guard_value or not (_guard_min <= _guard_value <= _guard_max):
                _guard_default = _settings_fallback_default(_guard_key)
                logger.debug(
                    f"{_guard_key} 越界或非法，回退默认 {_guard_default}: {form_data.get(_guard_key)}"
                )
                form_data[_guard_key] = str(_guard_default)
                remember_revert(
                    _guard_key, _submitted_guard, form_data[_guard_key],
                    allowed=_format_guard_range(_guard_min, _guard_max),
                )

        # 交叉约束：分片重叠必须严格小于分片窗口。modules/speech_recognition.py 的
        # _create_audio_chunks 每片前进 (window - overlap) 秒，overlap >= window 时
        # 步进 <= 0，分片循环永不终止、ASR 线程挂死（模块侧另有兜底，这里从配置层拦住）。
        # 钳到 window - 1.0 而不是 window - ε：留 1 秒余量既保证严格正步进，
        # 也避免步进趋近于 0 时同一段音频被反复切片、ASR 调用量爆炸。
        _window_key = 'AUDIO_CHUNK_WINDOW_S'
        _overlap_key = 'AUDIO_CHUNK_OVERLAP_S'

        _cross_cache: dict = {}

        def _cross_constraint_value(key):
            """取该键本次生效的数值；本次未提交时回读已存配置，取不到返回 None。"""
            raw = form_data.get(key)
            if raw is None:
                if 'config' not in _cross_cache:
                    try:
                        _cross_cache['config'] = load_config() or {}
                    except Exception as exc:  # 读配置失败不应阻断保存
                        logger.debug(f"读取已存配置用于分片交叉校验失败: {exc}")
                        _cross_cache['config'] = {}
                raw = _cross_cache['config'].get(key)
            try:
                return float(str(raw).strip())
            except (ValueError, TypeError):
                return None

        # 终态校验：位置必须在全部范围 guard 之后、update_config 之前，且两侧都取
        # 「本次生效值」而非「本次提交值」。只校验「本次提交了 overlap」这一侧是不够的：
        # 先保存 window=15 / overlap=5（两者都在各自区间内，完全合法），再只提交
        # window=5 —— 旧逻辑因为 overlap 不在 form_data 里而完全不触发交叉校验，
        # 落盘 5/5、步进 0，且不产生任何 warning。模块侧兜底会把 overlap 钳到
        # window-0.01，步进只剩 0.01 秒：60 秒素材被切成 5502 片、2 小时素材 719501 片，
        # 每片都要重抽一次音频再跑一轮 ASR，等同于把任务拖垮。所以这里不论哪一侧
        # 缺失，都回读已存配置补齐后再判一次，保证落盘配置永远满足 overlap < window。
        _window_value = _cross_constraint_value(_window_key)
        _overlap_value = _cross_constraint_value(_overlap_key)
        if (
            _window_value is not None
            and _overlap_value is not None
            and _overlap_value >= _window_value
        ):
            _overlap_limit = max(0.0, _window_value - 1.0)
            # 未提交 overlap 时 submitted 取当前生效值：提示里的「提交值」必须对得上
            # 用户此刻看到的旧值，否则提示会引用一个用户没提交过的数字。
            _submitted_overlap = form_data.get(_overlap_key, _overlap_value)
            form_data[_overlap_key] = str(_overlap_limit)
            remember_revert(
                _overlap_key, _submitted_overlap, form_data[_overlap_key],
                allowed=f'0–{_format_guard_bound(_overlap_limit)}',
                note=f'须小于分片窗口 {_format_guard_bound(_window_value)}',
            )

        # 旧模板把字幕换行上限钉死为 999/1 且强制两个开关为 on（当时控件是 disabled，
        # 用户选不出这个组合），配置侧由 migrate_pinned_subtitle_wrap_config 迁回
        # 42/2/False/False。设置页路径上只有 999 会越界回退，SUBTITLE_MAX_LINES=1 落在
        # 合法区间会被保留，落盘成 42/1/True/True —— 同一个组合两条路径结果不同。
        # 这里对同一组合整组回退 DEFAULT_CONFIG，与迁移落点保持一致：该组合只可能来自
        # 旧模板或异常提交，不是用户真实意图，而 guard 的既有语义就是「非法值回退默认值」。
        if (
            'SUBTITLE_MAX_LINE_LENGTH' in revert_index
            and str(form_data.get('SUBTITLE_MAX_LINES', '')).strip() == '1'
            and str(form_data.get('SUBTITLE_MAX_LINE_LENGTH_ENABLED', '')).strip().lower() in ('on', 'true', '1')
            and str(form_data.get('SUBTITLE_MAX_LINES_ENABLED', '')).strip().lower() in ('on', 'true', '1')
        ):
            # 两个开关回到「未启用」（DEFAULT_CONFIG 中均为 False），行数上限回到默认 2。
            for _wrap_key, _wrap_is_toggle in (
                ('SUBTITLE_MAX_LINES', False),
                ('SUBTITLE_MAX_LINE_LENGTH_ENABLED', True),
                ('SUBTITLE_MAX_LINES_ENABLED', True),
            ):
                _submitted_wrap = form_data.get(_wrap_key)
                form_data[_wrap_key] = (
                    False if _wrap_is_toggle else str(_settings_fallback_default(_wrap_key))
                )
                remember_revert(
                    _wrap_key, _submitted_wrap, form_data[_wrap_key],
                    note='旧模板钉死的字幕换行组合，整组回退为默认值',
                )

        if 'SUBTITLE_FONT_NAME' in form_data:
            form_data['SUBTITLE_FONT_NAME'] = str(form_data['SUBTITLE_FONT_NAME']).strip()

        # 枚举型外观/编码配置：非法值回退默认，避免脏值进入配置文件
        _enum_guards = (
            ('VIDEO_QUALITY_MODE', ('auto', 'manual')),
            ('VIDEO_HW_QUALITY_LEVEL', ('fast', 'balanced', 'quality')),
            ('VIDEO_COLOR_METADATA_MODE', ('auto', 'bt709', 'off')),
            ('BILIBILI_UPLOAD_LINE', ('auto', 'bda2', 'bldsa', 'qn', 'ws', 'txa', 'alia')),
        )
        for _enum_key, _enum_allowed in _enum_guards:
            if _enum_key not in form_data:
                continue
            _enum_value = str(form_data[_enum_key] or '').strip().lower()
            if _enum_value not in _enum_allowed:
                _enum_value = str(_settings_fallback_default(_enum_key)).strip().lower()
                logger.debug(f"{_enum_key} 非法值，回退默认 {_enum_value}")
            form_data[_enum_key] = _enum_value

        # 颜色字段统一成 #RRGGBB，非法值回退默认
        for _color_key in (
            'SUBTITLE_FONT_COLOR',
            'SUBTITLE_OUTLINE_COLOR',
            'SUBTITLE_BACKGROUND_COLOR',
        ):
            if _color_key not in form_data:
                continue
            form_data[_color_key] = normalize_hex_color(
                form_data[_color_key], str(_settings_fallback_default(_color_key))
            )

        # 预设标签（Issue #139）：统一成「每行一个」后落盘，使重复保存幂等；
        # 超量（>12）只保留前 12 个、超长（>20 字）保留原文，两者的后果都由
        # normalize_preset_tags_config 返回用户可见的 warning，不做静默丢弃。
        if 'PRESET_TAGS' in form_data:
            _preset_text, _preset_warnings = normalize_preset_tags_config(form_data.get('PRESET_TAGS'))
            form_data['PRESET_TAGS'] = _preset_text
            for _preset_warning in _preset_warnings:
                _append_settings_message(messages, 'warning', _preset_warning)
            # 勾选启用但解析后为空时，行为等同未启用，必须让用户知道
            if not _preset_text and is_preset_tags_enabled(form_data):
                _append_settings_message(
                    messages, 'warning',
                    '已勾选「使用预设标签」但没有填写任何标签，当前等同于未启用。'
                )

        _persist_settings_uploads(form_data, uploads)
        updated_config = update_config(form_data)
        try:
            from y2b.bilibili.bilibili_accounts import sync_default_cookie_path
            sync_default_cookie_path(updated_config)
        except Exception as exc:
            logger.warning("同步默认 bilibili 账号 Cookie 路径失败: %s", exc)
        if cookie_paste:
            try:
                from y2b.cookies.cookiecloud import resolve_cookie_output_path
                from y2b.youtube.youtube_handler import import_pasted_youtube_cookies
                cookie_path = resolve_cookie_output_path(
                    updated_config.get('YOUTUBE_COOKIES_PATH')
                )
                imported = import_pasted_youtube_cookies(cookie_paste, cookie_path)
                level = 'success' if imported.get('authenticated') else ('warning' if imported.get('ok') else 'danger')
                _append_settings_message(messages, level, imported.get('message') or 'YouTube Cookies 未更新。')
            except Exception:
                logger.warning("写入粘贴的 YouTube Cookies 失败")
                _append_settings_message(messages, 'danger', '写入粘贴的 YouTube Cookies 失败，文件未改动。')

        try:
            from y2b.pipeline.task_manager import get_global_task_processor
            configure_app(app, updated_config)
            get_global_task_processor(updated_config)
            logger.info("配置已更新并同步到任务处理器")
        except Exception as e:
            logger.warning(f"同步任务处理器配置失败: {e}")

        _sync_notification_service(updated_config)
        _append_notification_config_warnings(messages, updated_config)

        try:
            need_ffmpeg = False
            if str(updated_config.get('SPEECH_RECOGNITION_ENABLED', False)).lower() in ['true', '1', 'on']:
                need_ffmpeg = True
            if str(updated_config.get('SUBTITLE_EMBED_IN_VIDEO', False)).lower() in ['true', '1', 'on']:
                need_ffmpeg = True

            if need_ffmpeg:
                from y2b.media.ffmpeg_manager import get_windows_ffmpeg_manual_setup_message
                from y2b.youtube.youtube_handler import get_ffmpeg_path
                report('checking_ffmpeg', '正在检查 FFmpeg', '已启用依赖 FFmpeg 的功能，正在检查本地环境。')
                ff_path = get_ffmpeg_path(
                    logger=logger,
                    force_refresh=True,
                    progress_callback=progress_reporter
                )
                if ff_path and os.path.exists(ff_path):
                    logger.info(f"FFmpeg 已就绪: {ff_path}")
                    report('completed', 'FFmpeg 已就绪', ff_path, percent=100.0, level='success')
                else:
                    warning_msg = get_windows_ffmpeg_manual_setup_message()
                    logger.warning(warning_msg)
                    _append_settings_message(messages, 'warning', warning_msg)
                    report('warning', 'FFmpeg 未就绪', warning_msg, level='warning')
            else:
                report('completed', '配置已保存', '当前设置不需要额外下载 FFmpeg。', percent=100.0, level='success')
        except Exception as e:
            from y2b.media.ffmpeg_manager import get_windows_ffmpeg_manual_setup_message
            warning_msg = f'检查内置 FFmpeg 状态失败，请查看服务日志。{get_windows_ffmpeg_manual_setup_message()}'
            logger.warning("检查内置 FFmpeg 状态失败: %s", e)
            _append_settings_message(messages, 'warning', warning_msg)
            report('warning', 'FFmpeg 检查失败', warning_msg, level='warning')

        api_key = str(updated_config.get('YOUTUBE_API_KEY') or '').strip()
        api_ready, api_status = youtube_monitor.reload_api_client(updated_config)
        if api_key:
            if api_ready:
                youtube_monitor.start_all_schedules()
                if api_status == 'proxy_ready':
                    logger.info("YouTube监控 API 已重建并同步到监控系统，独立代理已启用")
                else:
                    logger.info("YouTube监控 API 已重建并同步到监控系统，当前为直连模式")
            else:
                youtube_monitor.stop_all_schedules()
                if api_status == 'missing_api_key':
                    warning_msg = 'YouTube API 密钥未配置，请先在设置页完成接入。'
                else:
                    warning_msg = 'YouTube监控 API 初始化失败，请检查 API 密钥、代理配置与网络连通性。'
                logger.warning(warning_msg)
                _append_settings_message(messages, 'warning', warning_msg)
        else:
            youtube_monitor.stop_all_schedules()
            logger.info("YouTube API密钥未配置，已跳过监控系统初始化")

        # Issue 101: 非阻断提示——CookieCloud 已启用时，逐项检查服务地址/UUID/密码是否缺失
        if str(updated_config.get('COOKIECLOUD_ENABLED', False)).lower() in ('true', '1', 'on'):
            _cookiecloud_missing = []
            if not str(updated_config.get('COOKIECLOUD_SERVER_URL', '') or '').strip():
                _cookiecloud_missing.append('服务地址')
            if not str(updated_config.get('COOKIECLOUD_UUID', '') or '').strip():
                _cookiecloud_missing.append('UUID')
            if not str(updated_config.get('COOKIECLOUD_PASSWORD', '') or '').strip():
                _cookiecloud_missing.append('密码')
            if _cookiecloud_missing:
                _append_settings_message(
                    messages, 'warning',
                    f'CookieCloud 已启用，但以下必填项为空：{"、".join(_cookiecloud_missing)}。'
                    'CookieCloud 需要服务地址、UUID、密码三者均非空才能正常同步，'
                    '请补全后保存，或暂时关闭 CookieCloud。'
                )

        # 值被回退过就必须让用户看见：放在 success 之前，保存本身的成功语义不变
        # （success=True、仍有「配置已成功保存」），但不再出现「全部成功」的假象。
        _revert_message = _format_settings_revert_message(reverts)
        if _revert_message:
            logger.warning('设置保存时回退了越界值: %s', _revert_message)
            _append_settings_message(messages, 'warning', _revert_message)

        _append_settings_message(messages, 'success', '配置已成功保存')
        final_level = 'warning' if any(msg['category'] in ('warning', 'danger') for msg in messages) else 'success'
        final_stage = 'warning' if final_level == 'warning' else 'completed'
        final_message = '配置已保存，但有提醒需要处理。' if final_level == 'warning' else '配置已成功保存'
        final_detail = next((msg['text'] for msg in messages if msg['category'] in ('warning', 'danger')), '设置已生效。')
        return {
            'success': True,
            'messages': messages,
            'updated_config': updated_config,
            'final_stage': final_stage,
            'final_message': final_message,
            'final_detail': final_detail,
            'final_level': final_level,
        }
    except Exception as e:
        logger.exception("保存设置失败: %s", e)
        public_message = '保存设置失败，请查看服务日志。'
        _append_settings_message(messages, 'danger', public_message)
        return {
            'success': False,
            'messages': messages,
            'updated_config': None,
            'final_stage': 'failed',
            'final_message': '保存设置失败',
            'final_detail': public_message,
            'final_level': 'error',
        }


def _finalize_settings_save_operation(operation_id: str, result: dict):
    current_state = _get_settings_save_progress(operation_id) or _new_settings_save_state(operation_id)
    percent = current_state.get('percent')
    if result.get('success') and percent is None:
        percent = 100.0

    _update_settings_save_progress(
        operation_id,
        stage=result.get('final_stage', 'completed'),
        message=result.get('final_message', ''),
        detail=result.get('final_detail', ''),
        percent=percent,
        done=True,
        level=result.get('final_level', 'success'),
        success=result.get('success'),
        messages=result.get('messages', []),
    )


def _run_settings_save_operation(operation_id: str, form_data: dict, uploads: dict):
    result = _perform_settings_save(form_data, uploads, operation_id=operation_id)
    _finalize_settings_save_operation(operation_id, result)


TG_BOT_API_TOKEN_PREFIX = 'y2a_tgbot_v1_'
TG_BOT_API_TOKEN_HASH_PREFIX = 'pbkdf2_sha256:'
TG_BOT_API_TOKEN_HASH_ITERATIONS = 260000
_TG_BOT_API_TOKEN_RANDOM_RE = re.compile(r'^[A-Za-z0-9_-]{32,}$')
_TG_BOT_UPLOAD_RATE_LIMIT_WINDOW_SECONDS = 60
_TG_BOT_UPLOAD_RATE_LIMIT_MAX_REQUESTS = 60
_TG_BOT_UPLOAD_RATE_LIMIT_MAX_BUCKETS = 10000
_TG_BOT_UPLOAD_RATE_LIMIT_BUCKETS = {}
_TG_BOT_UPLOAD_RATE_LIMIT_LOCK = threading.Lock()


def _generate_tgbot_api_token() -> str:
    return f'{TG_BOT_API_TOKEN_PREFIX}{secrets.token_urlsafe(32)}'


def _is_valid_tgbot_api_token_format(token: str | None) -> bool:
    token = str(token or '').strip()
    if not token.startswith(TG_BOT_API_TOKEN_PREFIX):
        return False
    random_part = token[len(TG_BOT_API_TOKEN_PREFIX):]
    return bool(_TG_BOT_API_TOKEN_RANDOM_RE.fullmatch(random_part))


def _hash_tgbot_api_token(token: str) -> str:
    salt = secrets.token_urlsafe(16)
    digest = hashlib.pbkdf2_hmac(
        'sha256',
        token.encode('utf-8'),
        salt.encode('utf-8'),
        TG_BOT_API_TOKEN_HASH_ITERATIONS,
    ).hex()
    return f'{TG_BOT_API_TOKEN_HASH_PREFIX}{TG_BOT_API_TOKEN_HASH_ITERATIONS}${salt}${digest}'


def _verify_tgbot_api_token_hash(token: str, stored_hash: str) -> bool:
    if not stored_hash.startswith(TG_BOT_API_TOKEN_HASH_PREFIX):
        return False
    payload = stored_hash[len(TG_BOT_API_TOKEN_HASH_PREFIX):]
    try:
        iterations_text, salt, expected_digest = payload.split('$', 2)
        iterations = int(iterations_text)
    except (TypeError, ValueError):
        return False
    if iterations < 1 or not salt or not expected_digest:
        return False
    actual_digest = hashlib.pbkdf2_hmac(
        'sha256',
        token.encode('utf-8'),
        salt.encode('utf-8'),
        iterations,
    ).hex()
    return secrets.compare_digest(expected_digest, actual_digest)


def _extract_bearer_token() -> str:
    auth_header = request.headers.get('Authorization') or ''
    scheme, _, value = auth_header.partition(' ')
    if scheme.lower() != 'bearer' or not value.strip():
        return ''
    return value.strip()


def _verify_tgbot_api_token(token: str, config: dict | None = None) -> bool:
    if not _is_valid_tgbot_api_token_format(token):
        return False
    effective_config = config if isinstance(config, dict) else load_config()
    stored_hash = str(effective_config.get('TG_BOT_API_TOKEN_HASH') or '').strip()
    return _verify_tgbot_api_token_hash(token, stored_hash)


def _tgbot_api_token_state(config: dict | None = None) -> dict:
    effective_config = config if isinstance(config, dict) else load_config()
    token_hash = str(effective_config.get('TG_BOT_API_TOKEN_HASH') or '').strip()
    return {
        'configured': bool(token_hash),
        'created_at': str(effective_config.get('TG_BOT_API_TOKEN_CREATED_AT') or ''),
        'last4': str(effective_config.get('TG_BOT_API_TOKEN_LAST4') or ''),
    }


def _ensure_tgbot_token_csrf_token() -> str:
    token = session.get('tgbot_token_csrf')
    if not isinstance(token, str) or not token:
        token = secrets.token_urlsafe(32)
        session['tgbot_token_csrf'] = token
    return token


def _validate_tgbot_token_csrf(submitted_token: str | None) -> bool:
    expected = session.get('tgbot_token_csrf')
    if not isinstance(expected, str) or not expected:
        return False
    if not secrets.compare_digest(expected, str(submitted_token or '')):
        return False
    session.pop('tgbot_token_csrf', None)
    return True


def _is_tgbot_upload_rate_limited() -> bool:
    client_id = request.remote_addr or 'unknown'
    now = time.time()
    window_start = now - _TG_BOT_UPLOAD_RATE_LIMIT_WINDOW_SECONDS
    with _TG_BOT_UPLOAD_RATE_LIMIT_LOCK:
        for key in list(_TG_BOT_UPLOAD_RATE_LIMIT_BUCKETS.keys()):
            _TG_BOT_UPLOAD_RATE_LIMIT_BUCKETS[key] = [
                timestamp for timestamp in _TG_BOT_UPLOAD_RATE_LIMIT_BUCKETS[key]
                if timestamp >= window_start
            ]
            if not _TG_BOT_UPLOAD_RATE_LIMIT_BUCKETS[key]:
                del _TG_BOT_UPLOAD_RATE_LIMIT_BUCKETS[key]

        if len(_TG_BOT_UPLOAD_RATE_LIMIT_BUCKETS) > _TG_BOT_UPLOAD_RATE_LIMIT_MAX_BUCKETS:
            newest_buckets = sorted(
                _TG_BOT_UPLOAD_RATE_LIMIT_BUCKETS.items(),
                key=lambda item: item[1][-1] if item[1] else 0,
                reverse=True,
            )[:_TG_BOT_UPLOAD_RATE_LIMIT_MAX_BUCKETS]
            _TG_BOT_UPLOAD_RATE_LIMIT_BUCKETS.clear()
            _TG_BOT_UPLOAD_RATE_LIMIT_BUCKETS.update(newest_buckets)

        bucket = _TG_BOT_UPLOAD_RATE_LIMIT_BUCKETS.setdefault(client_id, [])
        if len(bucket) >= _TG_BOT_UPLOAD_RATE_LIMIT_MAX_REQUESTS:
            return True
        bucket.append(now)
        if len(_TG_BOT_UPLOAD_RATE_LIMIT_BUCKETS) > _TG_BOT_UPLOAD_RATE_LIMIT_MAX_BUCKETS:
            newest_buckets = sorted(
                _TG_BOT_UPLOAD_RATE_LIMIT_BUCKETS.items(),
                key=lambda item: item[1][-1] if item[1] else 0,
                reverse=True,
            )[:_TG_BOT_UPLOAD_RATE_LIMIT_MAX_BUCKETS]
            _TG_BOT_UPLOAD_RATE_LIMIT_BUCKETS.clear()
            _TG_BOT_UPLOAD_RATE_LIMIT_BUCKETS.update(newest_buckets)
        return False


def tgbot_upload_token_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if request.method == 'OPTIONS':
            return f(*args, **kwargs)

        if _is_tgbot_upload_rate_limited():
            return jsonify({'success': False, 'message': '请求过于频繁，请稍后再试。'}), 429

        config = load_config()
        if not config.get('TG_BOT_API_TOKEN_HASH'):
            return jsonify({
                'success': False,
                'message': 'Telegram Bot API Token 未配置，请先在设置页生成专用 Token。'
            }), 403

        token = _extract_bearer_token()
        if not _verify_tgbot_api_token(token, config):
            return jsonify({'success': False, 'message': 'Telegram Bot API Token 无效或缺失。'}), 401

        return f(*args, **kwargs)
    return decorated_function


# 登录验证装饰器
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        config = load_config()
        if config.get('password_protection_enabled'):
            if 'logged_in' not in session:
                flash('请先登录以访问此页面。', 'info')
                return redirect(url_for('login', next=request.full_path))
        return f(*args, **kwargs)
    return decorated_function

# 配置CORS，允许来自YouTube的跨域请求
CORS(app, resources={
    r"/tasks/add_via_extension": {
        "origins": [r"https?://www\.youtube\.com", r"https?://youtube\.com"],
        "methods": ["POST", "OPTIONS"],
        "allow_headers": ["Content-Type", "Authorization"]
    }
})

# 确保日志目录存在
log_dir = get_app_subdir('logs')
os.makedirs(log_dir, exist_ok=True)

# 配置日志
log_file = os.path.join(log_dir, 'app.log')
log_formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')

# 文件处理器
file_handler = RotatingFileHandler(log_file, maxBytes=10485760, backupCount=10, encoding='utf-8')
file_handler.setFormatter(log_formatter)
file_handler.setLevel(logging.WARNING)

# 控制台处理器
console_handler = logging.StreamHandler()
console_handler.setFormatter(log_formatter)
console_handler.setLevel(logging.WARNING)
# 确保Windows控制台编码正确
if os.name == 'nt':
    import sys
    import codecs
    # 检查Python版本和reconfigure方法可用性
    python_version = sys.version_info
    
    # 强制设置stdout和stderr为UTF-8编码
    if hasattr(sys.stdout, 'reconfigure'):
        try:
            sys.stdout.reconfigure(encoding='utf-8')  # type: ignore
        except Exception:
            pass
        
    if hasattr(sys.stderr, 'reconfigure'):
        try:
            sys.stderr.reconfigure(encoding='utf-8')  # type: ignore
        except Exception:
            pass
    
    # 设置环境变量
    os.environ["PYTHONIOENCODING"] = "utf-8"
    # 为控制台处理器设置编码
    try:
        console_handler.setStream(codecs.getwriter('utf-8')(sys.stdout.buffer))  # type: ignore
    except Exception:
        pass

# 配置根日志记录器
root_logger = logging.getLogger()
root_logger.setLevel(logging.WARNING)
root_logger.addHandler(file_handler)
root_logger.addHandler(console_handler)

# 强制设置所有日志记录器的默认编码为UTF-8
try:
    logging.getLogger().handlers[0].encoding = 'utf-8'  # type: ignore
except Exception:
    pass

try:
    if len(logging.getLogger().handlers) > 1:
        logging.getLogger().handlers[1].encoding = 'utf-8'  # type: ignore
except Exception:
    pass

# 配置应用日志记录器
logger = logging.getLogger('Y2A-Auto')
logger.setLevel(logging.WARNING)

def init_id_mapping():
    """
    初始化AcFun分区ID映射.
    id_mapping.json 文件现在应该由 acfunid/ 目录直接提供，并包含在Docker镜像中。
    此函数仅记录一条信息，不再执行文件生成或检查逻辑。
    """
    logger.info("AcFun分区ID映射 (id_mapping.json) 应由 'acfunid/' 目录提供。")

# 模板辅助函数
def task_status_display(status):
    """将任务状态代码转换为显示文本"""
    status_map = {
        TASK_STATES['PENDING']: '等待处理',
        TASK_STATES['DOWNLOADING']: '下载中',
        TASK_STATES['DOWNLOADED']: '下载完成',
        TASK_STATES['TRANSLATING_SUBTITLE']: '翻译字幕中',
    TASK_STATES['ASR_TRANSCRIBING']: '语音转写中',
        TASK_STATES['ENCODING_VIDEO']: '转码视频中',
        TASK_STATES['TRANSLATING']: '翻译中',
        TASK_STATES['TAGGING']: '生成标签中',
        TASK_STATES['PARTITIONING']: '推荐分区中',
        TASK_STATES['MODERATING']: '内容审核中',
        TASK_STATES['AWAITING_REVIEW']: '等待人工审核',
        TASK_STATES['READY_FOR_UPLOAD']: '准备上传',
        TASK_STATES['UPLOADING']: '上传中',
        TASK_STATES['COMPLETED']: '已完成',
        TASK_STATES['FAILED']: '失败',
        'fetching_info': '采集信息中',
        'info_fetched': '信息已采集',
    }
    return status_map.get(status, status)

def task_status_color(status):
    """将任务状态代码转换为显示颜色"""
    color_map = {
        TASK_STATES['PENDING']: 'secondary',
        TASK_STATES['DOWNLOADING']: 'info',
        TASK_STATES['DOWNLOADED']: 'info',
        TASK_STATES['TRANSLATING_SUBTITLE']: 'info',
    TASK_STATES['ASR_TRANSCRIBING']: 'info',
        TASK_STATES['ENCODING_VIDEO']: 'info',
        TASK_STATES['TRANSLATING']: 'info',
        TASK_STATES['TAGGING']: 'info',
        TASK_STATES['PARTITIONING']: 'info',
        TASK_STATES['MODERATING']: 'info',
        TASK_STATES['AWAITING_REVIEW']: 'warning',
        TASK_STATES['READY_FOR_UPLOAD']: 'primary',
        TASK_STATES['UPLOADING']: 'primary',
        TASK_STATES['COMPLETED']: 'success',
        TASK_STATES['FAILED']: 'danger'
    }
    return color_map.get(status, 'secondary')

def _get_bilibili_zone_data():
    from y2b.bilibili.bilibili_zones import get_zone_list_sub
    return get_zone_list_sub()


def _load_acfun_partition_mapping():
    id_mapping_path = os.path.join(get_app_subdir('acfunid'), 'id_mapping.json')
    try:
        with open(id_mapping_path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        logger.warning(f"读取AcFun分区映射失败: {e}")
        return []


def _build_bilibili_partition_mapping():
    id_mapping = []
    zone_data = _get_bilibili_zone_data()
    for parent in zone_data:
        if not isinstance(parent, dict):
            continue
        parent_tid = parent.get('tid')
        parent_name = parent.get('name')
        if parent_tid in (None, 0, '0') or not parent_name:
            continue
        id_mapping.append({
            'category': parent_name,
            'partitions': [{
                'id': str(parent_tid),
                'name': parent_name,
                'sub_partitions': [
                    {
                        'id': str(sub.get('tid')),
                        'name': sub.get('name'),
                    }
                    for sub in (parent.get('sub') or [])
                    if isinstance(sub, dict) and sub.get('tid') not in (None, 0, '0') and sub.get('name')
                ]
            }]
        })
    return id_mapping


def get_partition_name(partition_id, upload_target='acfun'):
    """根据分区ID和平台获取分区名称"""
    if not partition_id:
        return None

    target = str(upload_target or 'acfun').strip().lower()
    pid = str(partition_id)

    if target == 'bilibili':
        try:
            zone_data = _get_bilibili_zone_data()
            for parent in zone_data:
                if str(parent.get('tid')) == pid:
                    return parent.get('name')
                for sub in parent.get('sub', []) or []:
                    if str(sub.get('tid')) == pid:
                        return sub.get('name')
        except Exception as e:
            logger.error(f"获取bilibili分区名称时出错: {str(e)}")
        return None

    # 默认 AcFun
    id_mapping_path = os.path.join(get_app_subdir('acfunid'), 'id_mapping.json')
    try:
        with open(id_mapping_path, 'r', encoding='utf-8') as f:
            id_mapping = json.load(f)

        for category in id_mapping:
            for partition in category.get('partitions', []):
                if str(partition.get('id')) == pid:
                    return partition.get('name')

                for sub_partition in partition.get('sub_partitions', []):
                    if str(sub_partition.get('id')) == pid:
                        return sub_partition.get('name')
    except Exception as e:
        logger.error(f"获取AcFun分区名称时出错: {str(e)}")

    return None

def validate_youtube_submission_url(value):
    """校验任务提交 URL，返回 (是否有效, 用户可见原因)。"""
    raw = str(value or '').strip()
    if not raw or len(raw) > 2048:
        return False, 'YouTube URL 无效或过长'
    try:
        parsed = urlparse(raw)
        if parsed.scheme.lower() not in ('http', 'https'):
            return False, '仅支持 http/https 的 YouTube URL'
        if parsed.username or parsed.password or (parsed.port not in (None, 80, 443)):
            return False, 'YouTube URL 包含不允许的凭据或端口'
    except (TypeError, ValueError):
        return False, 'YouTube URL 格式无效'

    if normalize_youtube_url(raw) is None:
        return False, '仅支持有效的 YouTube 视频或播放列表 URL'
    return True, ''


# 兼容调用方使用更短的公共名称。
is_valid_youtube_url = validate_youtube_submission_url


def parse_json(json_str):
    """将JSON字符串解析为Python对象"""
    if not json_str:
        return {}  # 返回空字典
    
    try:
        return json.loads(json_str)
    except Exception as e:
        logger.error(f"解析JSON时出错: {str(e)}")
        return {} # 返回空字典

def parse_youtube_duration(duration_str):
    """解析YouTube ISO 8601时长格式为秒数"""
    import re
    
    if not duration_str:
        return 0
    
    # PT1H30M45S -> 1小时30分45秒
    pattern = r'PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?'
    match = re.match(pattern, duration_str)
    
    if not match:
        return 0
    
    hours = int(match.group(1) or 0)
    minutes = int(match.group(2) or 0)
    seconds = int(match.group(3) or 0)
    
    return hours * 3600 + minutes * 60 + seconds

# 注册模板过滤器
app.jinja_env.filters['parse_youtube_duration'] = parse_youtube_duration

ALIYUN_LABEL_MAP = {
    "pornographic_adult": "疑似色情内容",
    "sexual_terms": "疑似性健康内容",
    "sexual_suggestive": "疑似低俗内容",
    "political_figure": "疑似政治人物",
    "political_entity": "疑似政治实体",
    "political_n": "疑似敏感政治内容",
    "political_p": "疑似涉政禁宣人物",
    "political_a": "涉政专项升级保障",
    "violent_extremist": "疑似极端组织",
    "violent_incidents": "疑似极端主义内容",
    "violent_weapons": "疑似武器弹药",
    "contraband_drug": "疑似毒品相关",
    "contraband_gambling": "疑似赌博相关",
    "contraband_act": "疑似违禁行为",
    "contraband_entity": "疑似违禁工具",
    "inappropriate_discrimination": "疑似偏见歧视内容",
    "inappropriate_ethics": "疑似不良价值观内容",
    "inappropriate_profanity": "疑似攻击辱骂内容",
    "inappropriate_oral": "疑似低俗口头语内容",
    "inappropriate_superstition": "疑似封建迷信内容",
    "inappropriate_nonsense": "疑似无意义灌水内容",
    "pt_to_sites": "疑似站外引流",
    "pt_by_recruitment": "疑似网赚兼职广告",
    "pt_to_contact": "疑似引流广告号",
    "religion_b": "疑似涉及佛教",
    "religion_t": "疑似涉及道教",
    "religion_c": "疑似涉及基督教",
    "religion_i": "疑似涉及伊斯兰教",
    "religion_h": "疑似涉及印度教",
    "customized": "命中自定义词库",
    "nonLabel": "内容正常", # 通常表示无风险
    "normal": "内容正常" # 另一种表示无风险的标签
    # 可以根据需要添加更多映射
}

def get_aliyun_label_chinese(label_value):
    """获取阿里云审核标签的中文含义"""
    return ALIYUN_LABEL_MAP.get(label_value, label_value) # 如果找不到映射，返回原始标签

# 注册模板辅助函数
app.jinja_env.globals.update(
    task_status_display=task_status_display,
    task_status_color=task_status_color,
    get_partition_name=get_partition_name,
    parse_json=parse_json,
    get_aliyun_label_chinese=get_aliyun_label_chinese, # 添加新的辅助函数
    submit_api_badge=bilibili_submit_api_badge, # 任务列表里的投稿接口徽标
)






def _runtime_gate_status() -> dict:
    try:
        from y2b.pipeline.gates import public_status
        return public_status()
    except Exception:
        logger.warning("读取运行时闸门状态失败", exc_info=True)
        return {
            'manual_pause': False,
            'downloads_blocked': False,
            'block_reasons': [],
            'download_message': '',
            'bot': {'active': False, 'retries': 0, 'reason': ''},
            'disk': {'hold': False},
            'bili_406': {'active': False},
            'bili_137022': {'active': False},
        }


def _gate_page_endpoint():
    return 'tasks' if str(request.form.get('next') or '').strip() == 'tasks' else 'index'


def _finish_gate_action(message, category):
    """有脚本时返回 JSON，由同页逻辑提示并刷新；没有脚本时仍整页跳回。"""
    return _respond_page_action(message, category, endpoint=_gate_page_endpoint())










def _render_task_fragments(task: dict, config: dict | None = None) -> dict:
    if config is None:
        config = load_config()

    return {
        'task_id': task.get('id'),
        'row_html': render_template('partials/task_row.html', task=task, config=config),
        'card_html': render_template('partials/task_card.html', task=task, config=config),
    }




def _missing_upload_partition_labels(task, config):
    upload_target = str(task.get('upload_target') or 'acfun').lower()
    recommend_enabled = str(config.get('RECOMMEND_PARTITION', False)).strip().lower() in ('true', '1', 'on', 'yes')
    missing = []

    if upload_target in ('acfun', 'both'):
        fixed_acfun_pid = str(config.get('FIXED_PARTITION_ID', '') or '').strip()
        acfun_partition = str(
            task.get('selected_partition_id_acfun')
            or task.get('recommended_partition_id_acfun')
            or task.get('selected_partition_id')
            or task.get('recommended_partition_id')
            or ''
        ).strip()
        if not fixed_acfun_pid and not acfun_partition and not recommend_enabled:
            missing.append('AcFun 分区')

    if upload_target in ('bilibili', 'both'):
        fixed_bili_pid = str(config.get('FIXED_PARTITION_ID_BILIBILI', '') or '').strip()
        bili_partition = str(
            task.get('selected_partition_id_bilibili')
            or task.get('recommended_partition_id_bilibili')
            or task.get('selected_partition_id')
            or task.get('recommended_partition_id')
            or ''
        ).strip()
        if not fixed_bili_pid and not bili_partition and not recommend_enabled:
            missing.append('bilibili 分区')

    return missing


def _start_background_force_upload(task_id, config, platform_name):
    logger.info(f"开始后台强制上传任务 {task_id} 到{platform_name}")

    def background_force_upload():
        try:
            success = force_upload_task(task_id, config)
            if success:
                logger.info(f"任务 {task_id} 后台强制上传成功")
            else:
                logger.error(f"任务 {task_id} 后台强制上传失败")
        except Exception as e:
            logger.error(f"任务 {task_id} 后台强制上传出错: {str(e)}")
            import traceback
            logger.error(traceback.format_exc())

    upload_thread = threading.Thread(target=background_force_upload, daemon=True)
    upload_thread.start()
    


def _resolve_edit_task_action(form):
    """从多值同名 action 表单中解析实际要执行的动作。

    优先级：force_upload > 其它显式提交的 action > default_action（save_metadata）。
    升级前已打开/被缓存的旧页面会同时提交 action=save_metadata 与 action=force_upload，
    二者按 DOM 顺序提交后 Werkzeug 仍保留两个值；这里显式优先识别 force_upload，
    避免点击「上传到 Bilibili」却只走保存。
    """
    _submitted = [a.strip().lower() for a in form.getlist('action') if a and a.strip()]
    if 'force_upload' in _submitted:
        return 'force_upload'
    if _submitted:
        return _submitted[0]
    _default = form.get('default_action') or 'save_metadata'
    return (_default or 'save_metadata').strip().lower()

















def _schedule_cancelled_task_file_cleanup(task_id, timeout_seconds=300):
    """等待取消的 worker 退出后清理任务文件，超时则保守保留。"""
    def _cleanup_when_inactive():
        deadline = time.monotonic() + timeout_seconds
        while _is_task_active(task_id) and time.monotonic() < deadline:
            time.sleep(0.25)
        if _is_task_active(task_id):
            logger.warning("任务取消后 worker 未在超时内退出，跳过文件清理: %s", task_id)
            return
        try:
            from y2b.pipeline.task_manager import delete_task_files
            if not delete_task_files(task_id):
                logger.warning("取消任务文件清理失败: %s", task_id)
        except Exception as exc:
            logger.warning("取消任务文件清理异常: %s", exc)

    threading.Thread(
        target=_cleanup_when_inactive,
        name=f"cleanup-cancelled-{task_id[:8]}",
        daemon=True,
    ).start()



# 系统健康检查辅助函数

def check_docker_volumes():
    """检查Docker挂载卷状态"""
    volumes = {}
    app_root = get_app_root_dir()
    
    volume_paths = [
        ('config', 'config'),
        ('db', 'db'),
        ('downloads', 'downloads'),
        ('logs', 'logs'),
        ('cookies', 'cookies'),
        ('temp', 'temp')
    ]
    
    for name, path in volume_paths:
        full_path = os.path.join(app_root, path)
        volumes[name] = {
            'path': full_path,
            'exists': os.path.exists(full_path),
            'is_mount': os.path.ismount(full_path),
            'writable': os.access(full_path, os.W_OK) if os.path.exists(full_path) else False,
            'size_mb': get_directory_size(full_path) if os.path.exists(full_path) else 0
        }
    
    return volumes

def get_directory_size(path):
    """获取目录大小(MB)"""
    try:
        total_size = 0
        for dirpath, dirnames, filenames in os.walk(path):
            for f in filenames:
                fp = os.path.join(dirpath, f)
                if os.path.exists(fp):
                    total_size += os.path.getsize(fp)
        return round(total_size / 1024 / 1024, 2)
    except Exception as e:
        logger.warning(f"获取目录大小失败 {path}: {e}")
        return 0

def _public_health_check_error_message(component: str = '检查项') -> str:
    return f'{component}检查失败，请查看服务日志。'

def get_database_info():
    """获取数据库文件信息"""
    try:
        from y2b.pipeline.task_manager import get_db_path
        db_path = get_db_path()
        
        if os.path.exists(db_path):
            stat_info = os.stat(db_path)
            return {
                'path': db_path,
                'size': stat_info.st_size,
                'writable': os.access(db_path, os.W_OK),
                'last_modified': stat_info.st_mtime
            }
        else:
            return {
                'path': db_path,
                'size': 0,
                'writable': False,
                'last_modified': None
            }
    except Exception as e:
        logger.warning("获取数据库文件信息失败: %s", e)
        return {
            'path': 'unknown',
            'size': 0,
            'writable': False,
            'error': _public_health_check_error_message('数据库')
        }

def get_database_debug_info():
    """获取数据库调试信息"""
    try:
        from y2b.pipeline.task_manager import get_db_path
        db_path = get_db_path()
        
        debug_info = {
            'db_path': db_path,
            'db_exists': os.path.exists(db_path),
            'db_dir': os.path.dirname(db_path),
            'db_dir_exists': os.path.exists(os.path.dirname(db_path)),
            'db_dir_writable': os.access(os.path.dirname(db_path), os.W_OK) if os.path.exists(os.path.dirname(db_path)) else False,
            'current_user': os.environ.get('USER', 'unknown'),
            'current_uid': os.getuid() if hasattr(os, 'getuid') else 'unknown',  # type: ignore
            'current_gid': os.getgid() if hasattr(os, 'getgid') else 'unknown'   # type: ignore
        }
        
        if os.path.exists(db_path):
            stat_info = os.stat(db_path)
            debug_info.update({
                'db_size': stat_info.st_size,
                'db_mode': oct(stat_info.st_mode)[-3:],
                'db_uid': stat_info.st_uid,
                'db_gid': stat_info.st_gid
            })
        
        return debug_info
    except Exception as e:
        logger.warning("获取数据库调试信息失败: %s", e)
        return {'error': _public_health_check_error_message('数据库')}

def get_file_info(file_path):
    """获取文件详细信息"""
    try:
        info = {
            'exists': os.path.exists(file_path),
            'size': 0,
            'readable': False,
            'last_modified': None
        }
        
        if info['exists']:
            stat_info = os.stat(file_path)
            info.update({
                'size': stat_info.st_size,
                'readable': os.access(file_path, os.R_OK),
                'last_modified': stat_info.st_mtime
            })
        
        return info
    except Exception as e:
        logger.warning("获取文件信息失败: %s", e)
        return {
            'exists': False,
            'size': 0,
            'readable': False,
            'last_modified': None,
            'error': _public_health_check_error_message('文件')
        }

def get_path_debug_info(file_path):
    """获取路径调试信息"""
    try:
        debug_info = {
            'path': file_path,
            'dirname': os.path.dirname(file_path),
            'basename': os.path.basename(file_path),
            'dirname_exists': os.path.exists(os.path.dirname(file_path)),
            'dirname_readable': os.access(os.path.dirname(file_path), os.R_OK) if os.path.exists(os.path.dirname(file_path)) else False,
            'dirname_writable': os.access(os.path.dirname(file_path), os.W_OK) if os.path.exists(os.path.dirname(file_path)) else False
        }
        
        # 列出目录内容
        if debug_info['dirname_exists'] and debug_info['dirname_readable']:
            try:
                debug_info['directory_contents'] = os.listdir(os.path.dirname(file_path))
            except:
                debug_info['directory_contents'] = 'permission_denied'
        
        return debug_info
    except Exception as e:
        logger.warning("获取路径调试信息失败: %s", e)
        return {'error': _public_health_check_error_message('路径')}




















def bilibili_qrcode_start():
    """发起 bilibili 二维码登录并返回二维码图片。只写入指定账号的 Cookie 文件。"""
    config = load_config()
    try:
        account, scope = _bilibili_account_from_request(config)
    except ValueError as exc:
        return jsonify({'success': False, 'message': str(exc)}), 400
    from y2b.bilibili.bilibili_accounts import account_cookie_file
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
        session_id, qr_session = _create_bilibili_qr_session(
            account_id=account.get('id'),
            proxy=scope.get('proxy') or '',
            trust_env=bool(scope.get('trust_env')),
        )
        qr_data = qr_session.generate()
        return jsonify({
            'success': True,
            'session_id': session_id,
            'account_id': account.get('id'),
            'account_name': account.get('name') or '',
            'image_base64': qr_data.get('image_base64', ''),
            'mime_type': qr_data.get('mime_type', 'image/png'),
            'expires_in': _BILIBILI_QR_SESSION_TTL_SECONDS,
            'cookie_path': cookie_path,
        })
    except Exception as e:
        logger.error(f"发起 bilibili 二维码登录失败: {e}")
        return jsonify({'success': False, 'message': '二维码登录失败，请稍后重试'}), 500


def _monitor_form_context(extra=None):
    from y2b.bilibili.bilibili_accounts import get_default_account, list_accounts, public_account
    from y2b.config.upload_templates import build_default_profile, list_templates, public_template
    config = load_config()
    context = {
        'upload_templates': [public_template(item) for item in list_templates()],
        'bilibili_accounts': [public_account(item) for item in list_accounts(config)],
        'default_upload_profile': build_default_profile(config, get_default_account(config).get('id') or ''),
    }
    if extra:
        context.update(extra)
    return context


def _bilibili_accounts_payload(config):
    from y2b.bilibili.bilibili_accounts import list_accounts, public_account
    return [public_account(item) for item in list_accounts(config)]























def _human_readable_size(num_bytes: float) -> str:
    # Simple helper for human readable file sizes
    if num_bytes is None:
        return '0B'
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if num_bytes < 1024.0:
            return f"{num_bytes:.2f}{unit}"
        num_bytes /= 1024.0
    return f"{num_bytes:.2f}PB"


def cleanup_logs(hours: int):
    """删除logs目录下指定小时之前的日志文件（不包括当前运行日志）"""
    try:
        logs_dir = get_app_subdir('logs')
        if not os.path.exists(logs_dir):
            return {'success': True, 'files_removed': 0, 'bytes_freed': 0, 'bytes_freed_readable': '0B'}

        cutoff = time.time() - float(hours) * 3600
        files_removed = 0
        bytes_freed = 0

        for filename in os.listdir(logs_dir):
            path = os.path.join(logs_dir, filename)
            # skip current top-level app and manager logs when present
            if filename in ('app.log', 'task_manager.log'):
                continue
            try:
                stat = os.stat(path)
                if stat.st_mtime < cutoff:
                    bytes_freed += stat.st_size if stat.st_size else 0
                    if os.path.isfile(path):
                        os.remove(path)
                        files_removed += 1
                    elif os.path.isdir(path):
                        # 统计目录内实际文件数和大小
                        for dirpath, dirnames, dir_filenames in os.walk(path):
                            for df in dir_filenames:
                                try:
                                    bytes_freed += os.path.getsize(os.path.join(dirpath, df))
                                except Exception:
                                    pass
                                files_removed += 1
                        shutil.rmtree(path)
            except Exception:
                continue

        return {'success': True, 'files_removed': files_removed, 'bytes_freed': bytes_freed, 'bytes_freed_readable': _human_readable_size(bytes_freed)}
    except Exception as e:
        logger.warning(f"日志清理失败: {e}")
        return {'success': False, 'error': str(e)}


def clear_specific_logs():
    """清空特定日志文件并删除 task_xxx.log 文件"""
    try:
        logs_dir = get_app_subdir('logs')
        processed_files = []
        bytes_freed = 0

        # 清空 app.log 和 task_manager.log
        for fname in ('app.log', 'task_manager.log'):
            fpath = os.path.join(logs_dir, fname)
            if os.path.exists(fpath):
                try:
                    bytes_freed += os.path.getsize(fpath)
                    open(fpath, 'w', encoding='utf-8').close()
                    processed_files.append(fname)
                except Exception:
                    pass

        # 删除所有task_xxx.log文件
        for filename in os.listdir(logs_dir):
            if filename.startswith('task_') and filename.endswith('.log'):
                path = os.path.join(logs_dir, filename)
                try:
                    bytes_freed += os.path.getsize(path) if os.path.exists(path) else 0
                    os.remove(path)
                    processed_files.append(filename)
                except Exception:
                    pass

        return {'success': True, 'files_processed': len(processed_files), 'processed_files': processed_files, 'bytes_freed': bytes_freed, 'bytes_freed_readable': _human_readable_size(bytes_freed)}
    except Exception as e:
        logger.warning(f"清空日志失败: {e}")
        return {'success': False, 'error': str(e)}


def _release_disk_hold_after_cleanup(downloads_dir: str) -> None:
    try:
        from y2b.pipeline.gates import disk_threshold_bytes, maybe_release_disk_hold
        from y2b.pipeline.task_manager import kick_pending_downloads_if_running
        config = load_config()
        if maybe_release_disk_hold(downloads_dir, disk_threshold_bytes(config)):
            logger.info("清理后磁盘已回到阈值以上，自动恢复下载")
            kick_pending_downloads_if_running()
    except Exception:
        logger.warning("清理后检查磁盘闸门失败", exc_info=True)


def cleanup_downloads(hours: int):
    """清理下载目录中指定hours之前的任务目录"""
    try:
        downloads_dir = get_app_subdir('downloads')
        if not os.path.exists(downloads_dir):
            return {'success': True, 'dirs_removed': 0, 'files_removed': 0, 'bytes_freed': 0, 'bytes_freed_readable': '0B'}

        cutoff = time.time() - float(hours) * 3600
        dirs_removed = 0
        files_removed = 0
        bytes_freed = 0

        for entry in os.listdir(downloads_dir):
            path = os.path.join(downloads_dir, entry)
            try:
                if not os.path.isdir(path):
                    continue
                # 只清理规范任务 UUID 目录；未知目录不做猜测性删除。
                try:
                    task_id = str(uuid.UUID(entry))
                except (ValueError, AttributeError, TypeError):
                    continue
                with _TASK_SCHEDULING_LOCK:
                    task = get_task(task_id)
                    if not task or _is_task_active(task_id):
                        continue
                    # 未完成任务及可恢复/上传中的任务均保留；仅清理终态目录。
                    if task.get('status') not in (TASK_STATES['COMPLETED'], TASK_STATES['FAILED']):
                        continue
                    # 取消标记存在时仍可能有线程尚未退出，保守跳过本轮。
                    from y2b.pipeline.task_manager import is_task_cancelled
                    if is_task_cancelled(task_id) and _is_task_active(task_id):
                        continue
                    file_entries = []
                    latest_mtime = os.path.getmtime(path)
                    for root, dirs, files in os.walk(path):
                        for filename in files:
                            fp = os.path.join(root, filename)
                            if os.path.exists(fp):
                                file_mtime = os.path.getmtime(fp)
                                latest_mtime = max(latest_mtime, file_mtime)
                                file_entries.append((fp, os.path.getsize(fp)))
                    # 任务状态更新时间也是活动信号，避免刚失败/完成的任务立即被清理。
                    updated_at = task.get('updated_at')
                    if updated_at:
                        try:
                            latest_mtime = max(
                                latest_mtime,
                                datetime.strptime(str(updated_at), '%Y-%m-%d %H:%M:%S').timestamp(),
                            )
                        except (TypeError, ValueError, OSError):
                            pass
                    # 目录 mtime 可能很旧，但重试/编码刚写入的文件应保护整个任务目录。
                    if latest_mtime >= cutoff:
                        continue
                    bytes_freed += sum(size for _, size in file_entries)
                    files_removed += len(file_entries)
                    shutil.rmtree(path)
                    dirs_removed += 1
            except Exception:
                continue

        _release_disk_hold_after_cleanup(downloads_dir)
        return {'success': True, 'dirs_removed': dirs_removed, 'files_removed': files_removed, 'bytes_freed': bytes_freed, 'bytes_freed_readable': _human_readable_size(bytes_freed)}
    except Exception as e:
        logger.warning(f"下载内容清理失败: {e}")
        return {'success': False, 'error': str(e)}


def configure_app(app, config):
    """为Flask app应用一些基础配置值（如 secret_key、上传限制等）"""
    try:
        # 使用配置中的SECRET_KEY提高会话安全，首次运行时自动生成并持久化
        secret = config.get('SECRET_KEY') if isinstance(config, dict) else None
        if secret:
            app.secret_key = secret
        elif isinstance(config, dict):
            # 若新配置中无 SECRET_KEY 但 app 已有，则复用，避免 session 全部失效
            if app.secret_key:
                config['SECRET_KEY'] = app.secret_key
            else:
                import secrets
                new_secret = secrets.token_hex(32)
                config['SECRET_KEY'] = new_secret
                app.secret_key = new_secret
                try:
                    update_config({'SECRET_KEY': new_secret})
                    logger.info("已自动生成并保存SECRET_KEY")
                except Exception as e:
                    logger.warning(f"保存自动生成的SECRET_KEY失败: {e}")

        max_content = config.get('MAX_CONTENT_LENGTH_MB', None) if isinstance(config, dict) else None
        if max_content:
            try:
                app.config['MAX_CONTENT_LENGTH'] = int(max_content) * 1024 * 1024
            except Exception:
                pass

        timeout_minutes = 30
        if isinstance(config, dict):
            timeout_value = config.get('LOGIN_SESSION_TIMEOUT_MINUTES', 30)
            try:
                timeout_minutes = int(timeout_value)
            except (TypeError, ValueError):
                timeout_minutes = 30
        app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(minutes=max(1, timeout_minutes))
        app.config['SESSION_REFRESH_EACH_REQUEST'] = True
        app.config['SESSION_COOKIE_HTTPONLY'] = True
        app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
        secure_cookie = config.get('SESSION_COOKIE_SECURE') if isinstance(config, dict) else None
        if secure_cookie is None:
            secure_cookie = os.environ.get('Y2A_COOKIE_SECURE', '').strip().lower() in ('1', 'true', 'yes', 'on')
        app.config['SESSION_COOKIE_SECURE'] = bool(secure_cookie)

        # 允许覆盖的内容
        app.config['Y2A_SETTINGS'] = config
    except Exception as e:
        logger.warning(f"应用配置失败: {e}")


def auto_start_pending_tasks(config):
    """在启动时尝试自动启动pending状态的任务"""
    try:
        from y2b.pipeline.task_manager import get_global_task_processor, get_tasks_by_status, TASK_STATES
        processor = get_global_task_processor(config)
        if not processor:
            return

        # 循环尝试启动下一个pending任务，直到并发数或没有更多pending
        # 我们设置一个上限避免无限循环
        attempts = 0
        while attempts < 200:
            attempts += 1
            try:
                processor._check_and_start_next_pending_task()
            except Exception:
                break
            # 如果没有pending则退出
            pending = get_tasks_by_status(TASK_STATES['PENDING'])
            if not pending:
                break
            time.sleep(0.05)
    except Exception as e:
        logger.warning(f"自动启动pending任务失败: {e}")


def schedule_log_cleanup():
    """为日志清理创建并启动一个BackgroundScheduler, 返回调度器对象"""
    try:
        config = load_config()
        interval_hours = int(config.get('LOG_CLEANUP_INTERVAL', 12))
        if not config.get('LOG_CLEANUP_ENABLED', False):
            return None

        scheduler = BackgroundScheduler()
        def _job():
            cleanup_logs(int(config.get('LOG_CLEANUP_HOURS', DEFAULT_CONFIG['LOG_CLEANUP_HOURS'])))
        scheduler.add_job(_job, 'interval', hours=interval_hours, id='log_cleanup', replace_existing=True)
        scheduler.start()
        return scheduler
    except Exception as e:
        logger.warning(f"启动日志清理定时任务失败: {e}")
        return None


def schedule_download_cleanup():
    try:
        config = load_config()
        interval_hours = int(config.get('DOWNLOAD_CLEANUP_INTERVAL', 24))
        if not config.get('DOWNLOAD_CLEANUP_ENABLED', False):
            return None

        scheduler = BackgroundScheduler()
        def _job():
            cleanup_downloads(int(config.get('DOWNLOAD_CLEANUP_HOURS', 72)))
        scheduler.add_job(_job, 'interval', hours=interval_hours, id='download_cleanup', replace_existing=True)
        scheduler.start()
        return scheduler
    except Exception as e:
        logger.warning(f"启动下载内容清理定时任务失败: {e}")
        return None


# 启动流程运行时状态。源码入口和 PyInstaller 入口共享同一状态，避免重复启动。
_application_runtime = None


def initialize_application():
    """初始化应用服务一次，返回可供 shutdown_application 使用的运行时状态。"""
    global _application_runtime
    if _application_runtime is not None:
        return _application_runtime

    logger.info("Y2A-Auto 启动中...")
    runtime = {
        'log_cleanup_scheduler': None,
        'download_cleanup_scheduler': None,
        'task_processor_started': False,
    }
    try:
        init_id_mapping()
        config = load_config()
        app.config['Y2A_SETTINGS'] = config
        logger.info(
            "配置已加载（摘要）: %s",
            json.dumps(_build_startup_config_log_summary(config), ensure_ascii=False),
        )
        _sync_notification_service(config)

        from y2b.pipeline.task_manager import get_global_task_processor
        get_global_task_processor(config)
        runtime['task_processor_started'] = True

        if config.get('AUTO_MODE_ENABLED', False):
            logger.info("自动模式已启用，正在启动所有pending任务...")
            auto_start_pending_tasks(config)

        if config.get('YOUTUBE_API_KEY'):
            api_ready, api_status = youtube_monitor.reload_api_client(config)
            if api_ready:
                youtube_monitor.start_all_schedules()
                if api_status == 'proxy_ready':
                    logger.info("YouTube监控系统已初始化，独立代理已启用")
                else:
                    logger.info("YouTube监控系统已初始化，当前为直连模式")
            elif api_status == 'missing_api_key':
                logger.warning('YouTube API 密钥未配置，请先在设置页完成接入。')
            else:
                logger.warning('YouTube监控 API 初始化失败，请检查 API 密钥、代理配置与网络连通性。')

        configure_app(app, config)
        app.config['DEBUG'] = False
        app.config['TEMPLATES_AUTO_RELOAD'] = False
        runtime['log_cleanup_scheduler'] = schedule_log_cleanup()
        runtime['download_cleanup_scheduler'] = schedule_download_cleanup()
        runtime['config'] = config
        _application_runtime = runtime
        return runtime
    except Exception:
        shutdown_application(runtime)
        raise


def shutdown_application(runtime=None):
    """关闭启动流程创建的资源一次；重复调用不会重复关闭。"""
    global _application_runtime
    runtime = runtime or _application_runtime
    if runtime is None or runtime.get('cleaned'):
        return

    runtime['cleaned'] = True
    for key in ('log_cleanup_scheduler', 'download_cleanup_scheduler'):
        scheduler = runtime.get(key)
        if scheduler is not None:
            try:
                scheduler.shutdown()
            except Exception:
                pass

    if runtime.get('task_processor_started'):
        try:
            from y2b.pipeline.task_manager import shutdown_global_task_processor
            shutdown_global_task_processor()
        except Exception:
            pass

    if _application_runtime is runtime:
        _application_runtime = None


















