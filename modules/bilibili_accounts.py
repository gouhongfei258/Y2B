#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""bilibili 多账号、每账号代理和每账号冷却。

现有 BILIBILI_COOKIES_PATH 会迁成「默认账号」，Cookie 文件仍放在原来的路径，
不需要重新登录。每个账号的扫码和上传只写自己的 Cookie 文件。

代理默认关闭。关闭时强制直连，不读取进程里的 HTTP_PROXY / HTTPS_PROXY。
同一账号的投稿串行；不同账号只受全局 MAX_CONCURRENT_UPLOADS 限制。
406、137022、603 只暂停触发它们的那个账号。
"""

import json
import os
import threading
import time
import uuid
from typing import Any, Dict, List, Optional
from urllib.parse import quote, urlsplit, urlunsplit

from modules.utils import get_app_subdir

DEFAULT_ACCOUNT_ID = "default"
DEFAULT_ACCOUNT_NAME = "默认账号"
DEFAULT_COOKIE_PATH = "cookies/bili_cookies.json"

COOLDOWN_SPECS = {
    406: ("BILIBILI_PREUPLOAD_COOLDOWN_MINUTES", 30, "preupload 406"),
    137022: ("BILIBILI_SUBMIT_COOLDOWN_MINUTES", 15, "投稿 137022"),
    603: ("BILIBILI_RISK_COOLDOWN_MINUTES", 30, "风控 603"),
}
ALLOWED_PROXY_SCHEMES = {"http", "https", "socks5", "socks5h"}

_LOCK = threading.RLock()
_ACCOUNT_LOCKS_GUARD = threading.Lock()
_ACCOUNT_LOCKS: Dict[str, threading.Lock] = {}


def accounts_store_path() -> str:
    return os.path.join(get_app_subdir("db"), "bilibili_accounts.json")


def cooldowns_store_path() -> str:
    return os.path.join(get_app_subdir("db"), "bilibili_cooldowns.json")


def _as_bool(value, default=False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return value != 0
    text = str(value).strip().lower()
    if not text:
        return default
    return text in ("1", "true", "yes", "on")


def _read_json(path: str, fallback: dict) -> dict:
    if not os.path.exists(path):
        return dict(fallback)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return dict(fallback)
    if not isinstance(data, dict):
        return dict(fallback)
    return data


def _write_json(path: str, payload: dict) -> None:
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _cookie_path_from_config(config: Optional[dict]) -> str:
    path = str((config or {}).get("BILIBILI_COOKIES_PATH") or DEFAULT_COOKIE_PATH).strip()
    return path or DEFAULT_COOKIE_PATH


def _blank_account(account_id: str, name: str, cookie_path: str, is_default: bool = False) -> Dict[str, Any]:
    return {
        "id": account_id,
        "name": name,
        "mid": "",
        "uname": "",
        "cookie_path": cookie_path,
        "proxy_enabled": False,
        "proxy_url": "",
        "proxy_username": "",
        "proxy_password": "",
        "is_default": bool(is_default),
    }


def ensure_accounts(config: Optional[dict] = None) -> Dict[str, Any]:
    """保证至少有一个默认账号，并把现有 Cookie 路径迁进去。"""
    with _LOCK:
        store = _read_json(accounts_store_path(), {"accounts": []})
        accounts = [item for item in store.get("accounts") or [] if isinstance(item, dict) and item.get("id")]
        if not accounts:
            accounts = [
                _blank_account(
                    DEFAULT_ACCOUNT_ID,
                    DEFAULT_ACCOUNT_NAME,
                    _cookie_path_from_config(config),
                    is_default=True,
                )
            ]
        if not any(item.get("is_default") for item in accounts):
            accounts[0]["is_default"] = True
        store = {"accounts": accounts}
        _write_json(accounts_store_path(), store)
        return store


def list_accounts(config: Optional[dict] = None) -> List[Dict[str, Any]]:
    return list(ensure_accounts(config)["accounts"])


def get_account(account_id: str, config: Optional[dict] = None) -> Optional[dict]:
    wanted = str(account_id or "").strip()
    if not wanted:
        return None
    for item in list_accounts(config):
        if item.get("id") == wanted:
            return dict(item)
    return None


def get_default_account(config: Optional[dict] = None) -> dict:
    accounts = list_accounts(config)
    for item in accounts:
        if item.get("is_default"):
            return dict(item)
    return dict(accounts[0])


def resolve_account_id(requested: str, config: Optional[dict] = None) -> str:
    """空值或找不到的 id 都落到当前默认账号。返回的是具体 id，不是“以后的默认”。"""
    wanted = str(requested or "").strip()
    if wanted:
        account = get_account(wanted, config)
        if account:
            return str(account["id"])
    return str(get_default_account(config)["id"])


def resolve_task_account(task: Optional[dict], config: Optional[dict] = None) -> dict:
    account_id = ""
    if task:
        account_id = str(task.get("bilibili_account_id") or "").strip()
        if not account_id:
            profile_raw = task.get("upload_profile_json")
            if isinstance(profile_raw, dict):
                account_id = str(profile_raw.get("bilibili_account_id") or "").strip()
            elif profile_raw:
                try:
                    profile = json.loads(profile_raw)
                except (TypeError, ValueError):
                    profile = None
                if isinstance(profile, dict):
                    account_id = str(profile.get("bilibili_account_id") or "").strip()
    if account_id:
        account = get_account(account_id, config)
        if account:
            return account
    return get_default_account(config)


def sync_default_cookie_path(config: Optional[dict]) -> None:
    """设置页里的 BILIBILI_COOKIES_PATH 继续表示默认账号的 Cookie 文件。"""
    path = _cookie_path_from_config(config)
    with _LOCK:
        store = ensure_accounts(config)
        # ensure_accounts 已经写过一次；这里再读内存中的列表并更新默认账号。
        accounts = store["accounts"]
        changed = False
        for item in accounts:
            if item.get("is_default") and item.get("cookie_path") != path:
                item["cookie_path"] = path
                changed = True
                break
        if changed:
            _write_json(accounts_store_path(), {"accounts": accounts})


def _new_cookie_path(account_id: str) -> str:
    return os.path.join("cookies", "bili_accounts", f"{account_id}.json")


def create_account(name: str, config: Optional[dict] = None) -> dict:
    ensure_accounts(config)
    account_id = uuid.uuid4().hex[:12]
    account = _blank_account(account_id, str(name or "").strip() or "新账号", _new_cookie_path(account_id), False)
    with _LOCK:
        store = _read_json(accounts_store_path(), {"accounts": []})
        accounts = [item for item in store.get("accounts") or [] if isinstance(item, dict)]
        accounts.append(account)
        _write_json(accounts_store_path(), {"accounts": accounts})
    return dict(account)


def update_account(account_id: str, fields: dict, config: Optional[dict] = None) -> Optional[dict]:
    wanted = str(account_id or "").strip()
    if not wanted:
        return None
    editable = ("name", "proxy_enabled", "proxy_url", "proxy_username", "proxy_password")
    with _LOCK:
        store = ensure_accounts(config)
        updated = None
        accounts = []
        for item in store["accounts"]:
            if item.get("id") != wanted:
                accounts.append(item)
                continue
            current = dict(item)
            for key in editable:
                if key not in fields:
                    continue
                if key == "proxy_enabled":
                    current[key] = _as_bool(fields.get(key), False)
                elif key == "proxy_password":
                    password = str(fields.get(key) or "")
                    if password != "":
                        current[key] = password
                else:
                    current[key] = str(fields.get(key) or "").strip()
            if current.get("proxy_enabled"):
                build_account_proxy(current)
            accounts.append(current)
            updated = dict(current)
        if updated is None:
            return None
        _write_json(accounts_store_path(), {"accounts": accounts})
        return updated


def set_default_account(account_id: str, config: Optional[dict] = None) -> Optional[dict]:
    wanted = str(account_id or "").strip()
    updated = None
    with _LOCK:
        store = ensure_accounts(config)
        if not any(item.get("id") == wanted for item in store["accounts"]):
            return None
        accounts = []
        for item in store["accounts"]:
            current = dict(item)
            current["is_default"] = current.get("id") == wanted
            if current["is_default"]:
                updated = dict(current)
            accounts.append(current)
        _write_json(accounts_store_path(), {"accounts": accounts})
    if updated:
        try:
            from modules.config_manager import load_config, update_config
            update_config({"BILIBILI_COOKIES_PATH": updated.get("cookie_path") or DEFAULT_COOKIE_PATH})
            load_config()
        except Exception:
            pass
    return updated


def _tasks_reference_account(account_id: str, is_default: bool) -> int:
    from modules.task_manager import get_db_connection
    conn = get_db_connection()
    try:
        columns = [row[1] for row in conn.execute("PRAGMA table_info(tasks)")]
        if "bilibili_account_id" not in columns:
            if not is_default:
                return 0
            row = conn.execute(
                """
                SELECT COUNT(*) FROM tasks
                WHERE LOWER(COALESCE(upload_target, 'acfun')) IN ('bilibili', 'both')
                """
            ).fetchone()
            return int(row[0] or 0)
        row = conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE bilibili_account_id = ?",
            (account_id,),
        ).fetchone()
        count = int(row[0] or 0)
        if is_default:
            legacy = conn.execute(
                """
                SELECT COUNT(*) FROM tasks
                WHERE (bilibili_account_id IS NULL OR TRIM(bilibili_account_id) = '')
                  AND LOWER(COALESCE(upload_target, 'acfun')) IN ('bilibili', 'both')
                """
            ).fetchone()
            count += int(legacy[0] or 0)
        return count
    finally:
        conn.close()


def _templates_reference_account(account_id: str) -> int:
    from modules.upload_templates import list_templates
    return sum(1 for item in list_templates() if str(item.get("bilibili_account_id") or "") == account_id)


def account_block_reason(account_id: str, config: Optional[dict] = None) -> str:
    account = get_account(account_id, config)
    if not account:
        return "账号不存在"
    if len(list_accounts(config)) <= 1:
        return "至少保留一个 bilibili 账号"
    task_count = _tasks_reference_account(account_id, bool(account.get("is_default")))
    if task_count:
        return f"还有 {task_count} 个任务使用这个账号，不能删除"
    template_count = _templates_reference_account(account_id)
    if template_count:
        return f"还有 {template_count} 个投稿模板使用这个账号，不能删除"
    return ""


def delete_account(account_id: str, config: Optional[dict] = None) -> tuple:
    reason = account_block_reason(account_id, config)
    if reason:
        return False, reason
    with _LOCK:
        store = ensure_accounts(config)
        accounts = [item for item in store["accounts"] if item.get("id") != account_id]
        if len(accounts) == len(store["accounts"]):
            return False, "账号不存在"
        if not any(item.get("is_default") for item in accounts):
            accounts[0]["is_default"] = True
        _write_json(accounts_store_path(), {"accounts": accounts})
    return True, "账号已删除"


def account_cookie_file(account: dict, config: Optional[dict] = None) -> str:
    from modules.task_manager import resolve_cookie_file_path
    path_value = str((account or {}).get("cookie_path") or "").strip()
    if not path_value and account and account.get("is_default"):
        path_value = _cookie_path_from_config(config)
    if not path_value:
        path_value = _new_cookie_path(str(account.get("id") or "account"))
    return resolve_cookie_file_path(
        path_value=path_value,
        default_relative_path=path_value,
        service_name="Bilibili",
        logger_obj=None,
        allow_json_txt_fallback=False,
    )


def build_account_proxy(account: Optional[dict]) -> str:
    """返回这个账号实际使用的代理 URL。关闭时返回空字符串，调用方必须直连。"""
    if not account or not _as_bool(account.get("proxy_enabled"), False):
        return ""
    proxy_url = str(account.get("proxy_url") or "").strip()
    if not proxy_url:
        raise ValueError("已开启代理，但没有填写代理地址")
    if "://" not in proxy_url:
        proxy_url = "http://" + proxy_url
    parts = urlsplit(proxy_url)
    scheme = (parts.scheme or "").lower()
    if scheme not in ALLOWED_PROXY_SCHEMES or not parts.hostname:
        raise ValueError("代理地址无效，请使用 http://127.0.0.1:7890 或 socks5://127.0.0.1:1080")
    username = str(account.get("proxy_username") or "").strip()
    password = str(account.get("proxy_password") or "")
    host = parts.hostname
    port = f":{parts.port}" if parts.port else ""
    if username or password:
        user = quote(username, safe="")
        if password:
            netloc = f"{user}:{quote(password, safe='')}@{host}{port}"
        else:
            netloc = f"{user}@{host}{port}"
    elif parts.username:
        netloc = parts.netloc
    else:
        netloc = f"{host}{port}"
    return urlunsplit((scheme, netloc, "", "", ""))


def request_scope_for_account(account: Optional[dict]) -> Dict[str, Any]:
    """账号请求范围。关闭代理时 proxy 为空且 trust_env 为 False，环境变量代理不会生效。"""
    return {
        "proxy": build_account_proxy(account),
        "trust_env": False,
    }


def public_account(account: dict) -> Dict[str, Any]:
    """页面展示用。不返回代理密码，也不返回 Cookie 内容。"""
    proxy_enabled = _as_bool(account.get("proxy_enabled"), False)
    return {
        "id": account.get("id") or "",
        "name": account.get("name") or "",
        "mid": str(account.get("mid") or ""),
        "uname": str(account.get("uname") or ""),
        "cookie_path": account.get("cookie_path") or "",
        "proxy_enabled": proxy_enabled,
        "proxy_url": account.get("proxy_url") or "",
        "proxy_username": account.get("proxy_username") or "",
        "proxy_password_set": bool(account.get("proxy_password")),
        "is_default": bool(account.get("is_default")),
        "route": "独立代理" if proxy_enabled else "直连",
    }


def account_upload_lock(account_id: str) -> threading.Lock:
    key = str(account_id or DEFAULT_ACCOUNT_ID)
    with _ACCOUNT_LOCKS_GUARD:
        lock = _ACCOUNT_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _ACCOUNT_LOCKS[key] = lock
        return lock


def _cooldown_minutes(config: Optional[dict], code: int) -> int:
    spec = COOLDOWN_SPECS.get(int(code))
    if not spec:
        return 0
    key, default, _label = spec
    try:
        minutes = int(str((config or {}).get(key, default)).strip())
    except (TypeError, ValueError):
        minutes = default
    if minutes < 1:
        minutes = default
    return minutes


def _load_cooldowns() -> dict:
    data = _read_json(cooldowns_store_path(), {"accounts": {}})
    accounts = data.get("accounts")
    if not isinstance(accounts, dict):
        accounts = {}
    return {"accounts": accounts}


def record_account_cooldown(account_id: str, code: int, config: Optional[dict] = None, now: Optional[float] = None) -> Optional[float]:
    """记下账号冷却。窗口还没结束时不把到期时间再往后推。"""
    try:
        code = int(code)
    except (TypeError, ValueError):
        return None
    if code not in COOLDOWN_SPECS:
        return None
    account_key = str(account_id or DEFAULT_ACCOUNT_ID)
    moment = time.time() if now is None else float(now)
    with _LOCK:
        store = _load_cooldowns()
        bucket = dict(store["accounts"].get(account_key) or {})
        current = bucket.get(str(code))
        try:
            current_until = float(current) if current else 0.0
        except (TypeError, ValueError):
            current_until = 0.0
        if current_until > moment:
            return current_until
        until = moment + _cooldown_minutes(config, code) * 60
        bucket[str(code)] = until
        store["accounts"][account_key] = bucket
        _write_json(cooldowns_store_path(), store)
        return until


def active_cooldowns(account_id: str, now: Optional[float] = None) -> List[dict]:
    account_key = str(account_id or "")
    moment = time.time() if now is None else float(now)
    store = _load_cooldowns()
    bucket = store["accounts"].get(account_key) or {}
    active = []
    if not isinstance(bucket, dict):
        return active
    for code, spec in COOLDOWN_SPECS.items():
        raw = bucket.get(str(code))
        try:
            until = float(raw) if raw else 0.0
        except (TypeError, ValueError):
            until = 0.0
        if until <= moment:
            continue
        active.append({
            "code": code,
            "until": until,
            "remaining_seconds": max(0, int(until - moment)),
            "label": spec[2],
        })
    active.sort(key=lambda item: item["remaining_seconds"], reverse=True)
    return active


def _format_remaining(seconds: int) -> str:
    seconds = max(0, int(seconds))
    minutes, secs = divmod(seconds, 60)
    if minutes <= 0:
        return f"{secs} 秒"
    if secs == 0:
        return f"{minutes} 分钟"
    return f"{minutes} 分钟 {secs} 秒"


def account_display_name(account: Optional[dict]) -> str:
    if not account:
        return "未知账号"
    name = str(account.get("name") or "").strip() or "未命名账号"
    uname = str(account.get("uname") or "").strip()
    if uname and uname != name:
        return f"{name}（{uname}）"
    return name


def cooldown_block_message(account: Optional[dict], config: Optional[dict] = None, now: Optional[float] = None) -> str:
    if not account:
        return ""
    active = active_cooldowns(str(account.get("id") or ""), now=now)
    if not active:
        return ""
    longest = active[0]
    labels = "、".join(item["label"] for item in active)
    return (
        f"bilibili 账号「{account_display_name(account)}」正在冷却（{labels}），"
        f"剩余 {_format_remaining(longest['remaining_seconds'])}。"
        "这段时间不会再请求该账号，其他账号不受影响。"
    )


def homepage_cooldown_notices(config: Optional[dict] = None, now: Optional[float] = None) -> List[str]:
    notices = []
    for account in list_accounts(config):
        message = cooldown_block_message(account, config, now=now)
        if message:
            notices.append(message)
    return notices


def remember_identity(account_id: str, mid: str, uname: str) -> None:
    with _LOCK:
        store = _read_json(accounts_store_path(), {"accounts": []})
        accounts = []
        for item in store.get("accounts") or []:
            if not isinstance(item, dict):
                continue
            if item.get("id") == account_id:
                item = dict(item)
                item["mid"] = str(mid or "")
                item["uname"] = str(uname or "")
            accounts.append(item)
        _write_json(accounts_store_path(), {"accounts": accounts})


def refresh_account_identity(account_id: str, config: Optional[dict] = None) -> tuple:
    """登录后读取 mid / uname。失败时保留 Cookie，不把 Cookie 写进日志。"""
    account = get_account(account_id, config)
    if not account:
        return False, "账号不存在"
    try:
        scope = request_scope_for_account(account)
    except ValueError as exc:
        return False, str(exc)
    cookie_file = account_cookie_file(account, config)
    if not cookie_file or not os.path.exists(cookie_file):
        return False, "Cookie 文件不存在"
    from modules.bilibili_auth import load_credential_from_file
    from modules.bili_sdk.utils.network import bili_request_scope
    try:
        credential = load_credential_from_file(cookie_file)
    except Exception as exc:
        return False, f"Cookie 无法读取: {exc}"
    try:
        with bili_request_scope(scope["proxy"], scope["trust_env"]):
            from modules.bilibili_auth import _fetch_nav_profile, _run_async
            profile = _run_async(_fetch_nav_profile(credential))
    except Exception as exc:
        return False, f"暂时无法获取账号昵称: {exc.__class__.__name__}"
    if not profile or not profile.get("is_login"):
        return False, "登录态无效，未能获取昵称"
    remember_identity(account_id, str(profile.get("mid") or ""), str(profile.get("uname") or ""))
    return True, "已更新账号昵称"
