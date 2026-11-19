#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""下载与投稿运行时闸门。

状态写在 db/runtime_gates.json，进程重启后仍然有效。这里只做本地判定：
冷却窗口内的拒绝不会改写到期时间，也不会去请求外部接口。
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
import time
from datetime import datetime
from typing import Any, Callable, Optional

from y2b.config.config_manager import (
    normalize_bilibili_submit_cooldown_minutes,
    normalize_bilibili_upload_line,
    normalize_disk_threshold_gb,
    normalize_preupload_cooldown_minutes,
)
from y2b.utils import get_app_subdir

# 第 1 次 1 分钟，之后翻倍，封顶 1 小时。
BOT_BACKOFF_MINUTES = (1, 2, 4, 8, 16, 32, 60)

_POST_DOWNLOAD_STATUSES = {
    "downloaded",
    "translating_subtitle",
    "encoding_video",
    "uploading",
    "asr_transcribing",
    "ready_for_upload",
    "completed",
}

_LOCK = threading.RLock()
_PROCS_LOCK = threading.Lock()
_PROCS: set[Any] = set()
_STATE_PATH_OVERRIDE: Optional[str] = None
_NOW: Optional[Callable[[], float]] = None


def _default_state() -> dict:
    return {
        "manual_pause": False,
        "bot": {
            "until": 0.0,
            "retries": 0,
            "reason": "",
            "tripped_at": 0.0,
        },
        "disk": {
            "hold": False,
            "free_bytes": None,
            "threshold_bytes": None,
            "checked_at": 0.0,
        },
        "bili_406": {"until": 0.0, "reason": "", "tripped_at": 0.0},
        "bili_137022": {"until": 0.0, "reason": "", "tripped_at": 0.0},
        # 按账号分开的 406 / 137022 / 603。不带账号的旧计时器仍留在上面两个键里。
        "bili_accounts": {},
        "submit_reuse": {},
    }


def _now() -> float:
    if _NOW is not None:
        return float(_NOW())
    return time.time()


def set_clock(clock: Optional[Callable[[], float]]) -> None:
    """测试注入时钟。传入 None 恢复真实时间。"""
    global _NOW
    _NOW = clock


def state_path() -> str:
    if _STATE_PATH_OVERRIDE:
        return _STATE_PATH_OVERRIDE
    directory = get_app_subdir("db")
    os.makedirs(directory, exist_ok=True)
    return os.path.join(directory, "runtime_gates.json")


def override_state_path(path: Optional[str]) -> None:
    """测试把状态文件指到临时路径。传入 None 恢复默认位置。"""
    global _STATE_PATH_OVERRIDE
    _STATE_PATH_OVERRIDE = path


def _merge_state(raw: Any) -> dict:
    state = _default_state()
    if not isinstance(raw, dict):
        return state
    state["manual_pause"] = bool(raw.get("manual_pause"))
    for key in ("bot", "disk", "bili_406", "bili_137022"):
        incoming = raw.get(key)
        if isinstance(incoming, dict):
            state[key].update(incoming)
    accounts = raw.get("bili_accounts")
    if isinstance(accounts, dict):
        cleaned = {}
        for account_id, bucket in accounts.items():
            if not isinstance(account_id, str) or not account_id or not isinstance(bucket, dict):
                continue
            cleaned[account_id] = {
                "name": str(bucket.get("name") or account_id),
                "406": _blank_cooldown_slot(),
                "137022": _blank_cooldown_slot(),
                "603": _blank_cooldown_slot(),
            }
            for code in ("406", "137022", "603"):
                incoming_slot = bucket.get(code)
                if isinstance(incoming_slot, dict):
                    cleaned[account_id][code].update(incoming_slot)
                    try:
                        cleaned[account_id][code]["until"] = float(incoming_slot.get("until") or 0)
                    except (TypeError, ValueError):
                        cleaned[account_id][code]["until"] = 0.0
        state["bili_accounts"] = cleaned
    reuse = raw.get("submit_reuse")
    if isinstance(reuse, dict):
        state["submit_reuse"] = {
            task_id: payload
            for task_id, payload in reuse.items()
            if isinstance(task_id, str) and isinstance(payload, dict)
        }
    state["bot"]["retries"] = max(0, int(state["bot"].get("retries") or 0))
    for slot in ("bot", "bili_406", "bili_137022"):
        try:
            state[slot]["until"] = float(state[slot].get("until") or 0)
        except (TypeError, ValueError):
            state[slot]["until"] = 0.0
    state["disk"]["hold"] = bool(state["disk"].get("hold"))
    return state


def _load_unlocked() -> dict:
    path = state_path()
    try:
        if not os.path.isfile(path) or os.path.getsize(path) == 0:
            return _default_state()
        with open(path, "r", encoding="utf-8") as handle:
            return _merge_state(json.load(handle))
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return _default_state()


def _save_unlocked(state: dict) -> None:
    path = state_path()
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="runtime_gates_", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def read_state() -> dict:
    with _LOCK:
        return _load_unlocked()


def format_remaining(seconds: float) -> str:
    remaining = max(0, int(seconds))
    minutes, secs = divmod(remaining, 60)
    if minutes and secs:
        return f"{minutes} 分 {secs} 秒"
    if minutes:
        return f"{minutes} 分钟"
    return f"{secs} 秒"


def format_timestamp(ts: float) -> str:
    try:
        value = float(ts or 0)
    except (TypeError, ValueError):
        return ""
    if value <= 0:
        return ""
    return datetime.fromtimestamp(value).strftime("%Y-%m-%d %H:%M:%S")


def _slot_remaining(slot: dict, now: Optional[float] = None) -> float:
    current = _now() if now is None else now
    try:
        until = float(slot.get("until") or 0)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, until - current)


def downloads_blocked(state: Optional[dict] = None) -> bool:
    return bool(download_block_reasons(state))


def download_block_reasons(state: Optional[dict] = None) -> list[str]:
    current = state if state is not None else read_state()
    reasons: list[str] = []
    if current.get("manual_pause"):
        reasons.append("manual")
    disk = current.get("disk") or {}
    if disk.get("hold"):
        reasons.append("disk-low")
    bot = current.get("bot") or {}
    if _slot_remaining(bot) > 0:
        reasons.append("bot")
    return reasons


def task_needs_download(task: Optional[dict]) -> bool:
    """任务是否还要进入下载。编码/上传中的任务返回 False。"""
    if not isinstance(task, dict):
        return True
    status = str(task.get("status") or "").strip().lower()
    if status in _POST_DOWNLOAD_STATUSES:
        return False
    video = task.get("video_path_local")
    if isinstance(video, str) and video and os.path.isfile(video):
        return False
    return True


def describe_download_block(state: Optional[dict] = None) -> str:
    current = state if state is not None else read_state()
    reasons = download_block_reasons(current)
    if not reasons:
        return ""
    parts: list[str] = []
    if "manual" in reasons:
        parts.append("下载已手动暂停，不会自动恢复。已下载完成的编码和上传会继续。")
    if "disk-low" in reasons:
        disk = current.get("disk") or {}
        free = disk.get("free_bytes")
        threshold = disk.get("threshold_bytes")
        free_text = _gb_text(free)
        threshold_text = _gb_text(threshold)
        parts.append(
            f"磁盘剩余空间低于阈值（剩余 {free_text}，阈值 {threshold_text}），已暂停下载。"
        )
    if "bot" in reasons:
        bot = current.get("bot") or {}
        reason = str(bot.get("reason") or "YouTube 人机验证").strip()
        until_text = format_timestamp(bot.get("until")) or "稍后"
        parts.append(
            f"YouTube 下载已熔断：{reason}。下次自动试探时间：{until_text}。"
        )
    return " ".join(parts)


def _gb_text(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "未知"
    return f"{number / (1024 ** 3):.2f} GB"


def disk_threshold_bytes(config: Optional[dict] = None) -> int:
    if config is None:
        from y2b.config.config_manager import load_config
        config = load_config()
    gb = normalize_disk_threshold_gb((config or {}).get("DOWNLOAD_DISK_FREE_THRESHOLD_GB"))
    return int(gb) * 1024 * 1024 * 1024


def disk_free_bytes(path: str) -> int:
    usage = shutil.disk_usage(path if path and os.path.exists(path) else os.getcwd())
    return int(usage.free)


def disk_is_low(path: str, threshold_bytes: int) -> bool:
    try:
        return disk_free_bytes(path) < int(threshold_bytes)
    except OSError:
        return False


def disk_hold_active() -> bool:
    return bool(read_state().get("disk", {}).get("hold"))


def pause_downloads() -> None:
    with _LOCK:
        state = _load_unlocked()
        state["manual_pause"] = True
        _save_unlocked(state)
    stop_inflight_downloads()


def resume_downloads() -> None:
    """只解除手动暂停。磁盘闸门和机器人熔断保持原样。"""
    with _LOCK:
        state = _load_unlocked()
        state["manual_pause"] = False
        _save_unlocked(state)


def manual_pause_active() -> bool:
    return bool(read_state().get("manual_pause"))


def register_download_process(proc: Any) -> None:
    if proc is None:
        return
    with _PROCS_LOCK:
        _PROCS.add(proc)


def unregister_download_process(proc: Any) -> None:
    if proc is None:
        return
    with _PROCS_LOCK:
        _PROCS.discard(proc)


def stop_inflight_downloads() -> int:
    """终止正在跑的 yt-dlp 下载进程。编码和上传进程不在这张表里。"""
    with _PROCS_LOCK:
        procs = list(_PROCS)
    stopped = 0
    for proc in procs:
        try:
            if proc.poll() is not None:
                continue
            proc.terminate()
            stopped += 1
        except Exception:
            continue
        try:
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
    return stopped


def _set_disk_hold(active: bool, free_bytes: Optional[int], threshold_bytes: int) -> bool:
    """返回这次是否新进入磁盘暂停。"""
    with _LOCK:
        state = _load_unlocked()
        was = bool(state["disk"].get("hold"))
        state["disk"]["hold"] = bool(active)
        state["disk"]["free_bytes"] = free_bytes
        state["disk"]["threshold_bytes"] = int(threshold_bytes)
        state["disk"]["checked_at"] = _now()
        if not active:
            state["disk"]["reason"] = ""
        else:
            state["disk"]["reason"] = "disk-low"
        _save_unlocked(state)
        return bool(active) and not was


def poll_disk(path: str, threshold_bytes: int) -> bool:
    """空间不足时进入 disk-low 并停掉正在下载的进程。空间够时不解除手动暂停。

    返回 True 表示当前应停止下载。
    """
    try:
        free = disk_free_bytes(path)
    except OSError:
        return disk_hold_active()
    if free < int(threshold_bytes):
        entered = _set_disk_hold(True, free, threshold_bytes)
        if entered:
            stop_inflight_downloads()
        return True
    return False


def maybe_release_disk_hold(path: str, threshold_bytes: int) -> bool:
    """清理或上传删除之后，空间回到阈值以上才解除磁盘暂停。"""
    try:
        free = disk_free_bytes(path)
    except OSError:
        return False
    with _LOCK:
        state = _load_unlocked()
        state["disk"]["free_bytes"] = free
        state["disk"]["threshold_bytes"] = int(threshold_bytes)
        state["disk"]["checked_at"] = _now()
        if not state["disk"].get("hold"):
            _save_unlocked(state)
            return False
        if free < int(threshold_bytes):
            _save_unlocked(state)
            return False
        state["disk"]["hold"] = False
        state["disk"]["reason"] = ""
        _save_unlocked(state)
        return True


def trip_youtube_bot_circuit(reason: str) -> dict:
    """确认人机验证或硬拒绝后拉长退避。已在等待窗口内时不刷新到期时间。"""
    text = str(reason or "YouTube 人机验证").strip() or "YouTube 人机验证"
    with _LOCK:
        state = _load_unlocked()
        bot = state["bot"]
        now = _now()
        if _slot_remaining(bot, now) > 0:
            _save_unlocked(state)
            return dict(bot)
        retries = max(0, int(bot.get("retries") or 0))
        minutes = BOT_BACKOFF_MINUTES[min(retries, len(BOT_BACKOFF_MINUTES) - 1)]
        bot["retries"] = retries + 1
        bot["until"] = now + minutes * 60
        bot["reason"] = text[:200]
        bot["tripped_at"] = now
        _save_unlocked(state)
        snapshot = dict(bot)
    stop_inflight_downloads()
    return snapshot


def skip_bot_wait() -> None:
    """立即恢复：跳过当前等待，保留已累计的退避次数。"""
    with _LOCK:
        state = _load_unlocked()
        state["bot"]["until"] = 0.0
        _save_unlocked(state)


def reset_bot_circuit() -> None:
    """重置熔断：清掉等待和退避计数。"""
    with _LOCK:
        state = _load_unlocked()
        state["bot"] = _default_state()["bot"]
        _save_unlocked(state)


def _arm_slot(slot_name: str, minutes: int, reason: str) -> tuple[float, bool]:
    """窗口未结束时返回剩余时间且 refreshed=False，不改到期时间。"""
    with _LOCK:
        state = _load_unlocked()
        slot = state[slot_name]
        now = _now()
        remaining = _slot_remaining(slot, now)
        if remaining > 0:
            return remaining, False
        duration = max(1, int(minutes)) * 60
        slot["until"] = now + duration
        slot["reason"] = reason
        slot["tripped_at"] = now
        _save_unlocked(state)
        return float(duration), True


def _blank_cooldown_slot() -> dict:
    return {"until": 0.0, "reason": "", "tripped_at": 0.0}


def _arm_account_code(account_id: str, code: str, minutes: int, reason: str, account_name: str = "") -> tuple[float, bool]:
    """只给这一个账号计时。窗口还没结束时不把到期时间往后推。"""
    with _LOCK:
        state = _load_unlocked()
        accounts = state.setdefault("bili_accounts", {})
        bucket = accounts.get(account_id)
        if not isinstance(bucket, dict):
            bucket = {
                "name": account_name or account_id,
                "406": _blank_cooldown_slot(),
                "137022": _blank_cooldown_slot(),
                "603": _blank_cooldown_slot(),
            }
            accounts[account_id] = bucket
        if account_name:
            bucket["name"] = account_name
        slot = bucket.get(code)
        if not isinstance(slot, dict):
            slot = _blank_cooldown_slot()
            bucket[code] = slot
        now = _now()
        remaining = _slot_remaining(slot, now)
        if remaining > 0:
            return remaining, False
        duration = max(1, int(minutes)) * 60
        slot["until"] = now + duration
        slot["reason"] = reason
        slot["tripped_at"] = now
        _save_unlocked(state)
        return float(duration), True


def arm_bilibili_406(minutes: int, account_id: Optional[str] = None, account_name: str = "") -> tuple[float, bool]:
    normalized = normalize_preupload_cooldown_minutes(minutes)
    if account_id:
        return _arm_account_code(str(account_id), "406", normalized, "HTTP 406 preupload 风控", account_name)
    return _arm_slot("bili_406", normalized, "HTTP 406 preupload 风控")


def arm_bilibili_137022(minutes: int, account_id: Optional[str] = None, account_name: str = "") -> tuple[float, bool]:
    normalized = normalize_bilibili_submit_cooldown_minutes(minutes)
    if account_id:
        return _arm_account_code(str(account_id), "137022", normalized, "code 137022 投稿频率限制", account_name)
    return _arm_slot("bili_137022", normalized, "code 137022 投稿频率限制")


def arm_bilibili_603(minutes: int, account_id: Optional[str] = None, account_name: str = "") -> tuple[float, bool]:
    from y2b.config.config_manager import normalize_bounded_int
    normalized = normalize_bounded_int(minutes, 30, 1, 1440)
    if not account_id:
        account_id = "default"
    return _arm_account_code(str(account_id), "603", normalized, "code 603 账号风控", account_name)


def save_submit_reuse(task_id: str, payload: dict) -> None:
    if not task_id or not isinstance(payload, dict):
        return
    videos = payload.get("videos")
    cover_url = str(payload.get("cover_url") or "")
    if not isinstance(videos, list) or not videos or not cover_url:
        return
    stored = {
        "videos": videos,
        "cover_url": cover_url,
        "video_path": str(payload.get("video_path") or ""),
        "size": payload.get("size"),
        "saved_at": _now(),
    }
    with _LOCK:
        state = _load_unlocked()
        state["submit_reuse"][str(task_id)] = stored
        _save_unlocked(state)


def clear_submit_reuse(task_id: Optional[str]) -> None:
    if not task_id:
        return
    with _LOCK:
        state = _load_unlocked()
        if str(task_id) in state["submit_reuse"]:
            state["submit_reuse"].pop(str(task_id), None)
            _save_unlocked(state)


def peek_submit_reuse(task_id: Optional[str], video_path: Optional[str] = None) -> Optional[dict]:
    if not task_id:
        return None
    state = read_state()
    payload = (state.get("submit_reuse") or {}).get(str(task_id))
    if not isinstance(payload, dict):
        return None
    videos = payload.get("videos")
    cover_url = str(payload.get("cover_url") or "")
    if not isinstance(videos, list) or not videos or not cover_url:
        return None
    stored_path = str(payload.get("video_path") or "")
    if video_path and stored_path:
        try:
            if os.path.abspath(video_path) != os.path.abspath(stored_path):
                return None
            size = os.path.getsize(video_path)
        except OSError:
            return None
        stored_size = payload.get("size")
        if stored_size is not None and int(stored_size) != int(size):
            return None
    return {"videos": videos, "cover_url": cover_url}


def bilibili_406_block(state: Optional[dict] = None) -> Optional[dict]:
    current = state if state is not None else read_state()
    remaining = _slot_remaining(current.get("bili_406") or {})
    if remaining <= 0:
        return None
    return {
        "code": 406,
        "reason": "HTTP 406 preupload 风控",
        "remaining": remaining,
    }


def bilibili_137022_block(state: Optional[dict] = None) -> Optional[dict]:
    current = state if state is not None else read_state()
    remaining = _slot_remaining(current.get("bili_137022") or {})
    if remaining <= 0:
        return None
    return {
        "code": 137022,
        "reason": "code 137022 投稿频率限制",
        "remaining": remaining,
    }


def account_cooldown_entries(account_id: str, now: Optional[float] = None, state: Optional[dict] = None) -> list:
    """某个账号当前还在生效的冷却。now 用于测试注入时间。"""
    current = state if state is not None else read_state()
    bucket = (current.get("bili_accounts") or {}).get(str(account_id or "")) or {}
    labels = {
        "406": "preupload 406",
        "137022": "投稿 137022",
        "603": "风控 603",
    }
    active = []
    for code, label in labels.items():
        slot = bucket.get(code) or {}
        remaining = _slot_remaining(slot, now)
        if remaining <= 0:
            continue
        try:
            until = float(slot.get("until") or 0)
        except (TypeError, ValueError):
            until = 0.0
        active.append({
            "code": int(code),
            "until": until,
            "remaining_seconds": int(remaining),
            "label": label,
        })
    active.sort(key=lambda item: item["remaining_seconds"], reverse=True)
    return active


def bilibili_local_block_message(
    state: Optional[dict] = None,
    account_id: Optional[str] = None,
    account_name: str = "",
) -> str:
    """冷却期内的本地失败文案。调用方不得据此去请求 Bilibili，也不得刷新到期时间。

    传入 account_id 时只看这个账号，其他账号的冷却不会挡住它。
    """
    current = state if state is not None else read_state()
    if account_id:
        active = account_cooldown_entries(str(account_id), state=current)
        if not active:
            return ""
        longest = active[0]
        labels = "、".join(item["label"] for item in active)
        name = account_name or ((current.get("bili_accounts") or {}).get(str(account_id)) or {}).get("name") or str(account_id)
        if longest["code"] == 137022 and len(active) == 1:
            tail = "本次未请求投稿接口，到期时间未刷新。"
        else:
            tail = "本次未请求 Bilibili，到期时间未刷新。"
        return (
            f"bilibili 账号「{name}」正在冷却（{labels}），"
            f"剩余 {format_remaining(longest['remaining_seconds'])}。"
            f"{tail}其他账号不受影响。"
        )
    preupload = bilibili_406_block(current)
    if preupload:
        return (
            "Bilibili 风控冷却（HTTP 406 / preupload）仍在生效，"
            f"剩余 {format_remaining(preupload['remaining'])}。"
            "本次未请求 Bilibili，到期时间未刷新。"
        )
    submit = bilibili_137022_block(current)
    if submit:
        return (
            "Bilibili 投稿频率冷却（code 137022）仍在生效，"
            f"剩余 {format_remaining(submit['remaining'])}。"
            "本次未请求投稿接口，到期时间未刷新。"
        )
    return ""


def configured_upload_line(config: Optional[dict]) -> str:
    return normalize_bilibili_upload_line((config or {}).get("BILIBILI_UPLOAD_LINE"))


def configured_406_minutes(config: Optional[dict]) -> int:
    return normalize_preupload_cooldown_minutes(
        (config or {}).get("BILIBILI_PREUPLOAD_COOLDOWN_MINUTES")
    )


def configured_137022_minutes(config: Optional[dict]) -> int:
    return normalize_bilibili_submit_cooldown_minutes(
        (config or {}).get("BILIBILI_SUBMIT_COOLDOWN_MINUTES")
    )


def configured_603_minutes(config: Optional[dict]) -> int:
    from y2b.config.config_manager import normalize_bounded_int
    return normalize_bounded_int((config or {}).get("BILIBILI_RISK_COOLDOWN_MINUTES"), 30, 1, 1440)


def public_status() -> dict:
    state = read_state()
    now = _now()
    bot = state.get("bot") or {}
    disk = state.get("disk") or {}
    preupload = bilibili_406_block(state)
    submit = bilibili_137022_block(state)
    bot_remaining = _slot_remaining(bot, now)
    reasons = download_block_reasons(state)
    retries = max(0, int(bot.get("retries") or 0))
    next_index = min(retries, len(BOT_BACKOFF_MINUTES) - 1)
    return {
        "manual_pause": bool(state.get("manual_pause")),
        "downloads_blocked": bool(reasons),
        "block_reasons": reasons,
        "download_message": describe_download_block(state),
        "bot": {
            "active": bot_remaining > 0,
            "reason": str(bot.get("reason") or ""),
            "until": float(bot.get("until") or 0),
            "until_display": format_timestamp(bot.get("until")) if bot_remaining > 0 else "",
            "remaining_seconds": bot_remaining,
            "remaining_display": format_remaining(bot_remaining) if bot_remaining > 0 else "",
            "retries": retries,
            "next_backoff_minutes": BOT_BACKOFF_MINUTES[next_index],
        },
        "disk": {
            "hold": bool(disk.get("hold")),
            "free_bytes": disk.get("free_bytes"),
            "threshold_bytes": disk.get("threshold_bytes"),
            "free_display": _gb_text(disk.get("free_bytes")),
            "threshold_display": _gb_text(disk.get("threshold_bytes")),
        },
        "bili_406": {
            "active": preupload is not None,
            "reason": "HTTP 406 preupload 风控",
            "remaining_display": format_remaining(preupload["remaining"]) if preupload else "",
            "until_display": format_timestamp((state.get("bili_406") or {}).get("until")) if preupload else "",
        },
        "bili_137022": {
            "active": submit is not None,
            "reason": "code 137022 投稿频率限制",
            "remaining_display": format_remaining(submit["remaining"]) if submit else "",
            "until_display": format_timestamp((state.get("bili_137022") or {}).get("until")) if submit else "",
        },
        "bili_account_cooldowns": _public_account_cooldowns(state, now),
    }


def _public_account_cooldowns(state: dict, now: float) -> list:
    notices = []
    labels = {
        "406": "HTTP 406 / preupload",
        "137022": "code 137022",
        "603": "code 603",
    }
    for account_id, bucket in (state.get("bili_accounts") or {}).items():
        if not isinstance(bucket, dict):
            continue
        name = str(bucket.get("name") or account_id)
        for code, label in labels.items():
            slot = bucket.get(code) or {}
            remaining = _slot_remaining(slot, now)
            if remaining <= 0:
                continue
            notices.append({
                "account_id": account_id,
                "name": name,
                "code": code,
                "label": label,
                "remaining_display": format_remaining(remaining),
                "until_display": format_timestamp(slot.get("until")),
            })
    return notices
