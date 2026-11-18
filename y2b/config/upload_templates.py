#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""投稿模板。

全局设置仍然是「默认模板」。监控配置可以绑定一个命名模板，手动添加任务
也可以指定模板。解析顺序是：任务级指定 > 监控绑定 > 全局默认。

任务入队时把解析结果整份写入 upload_profile_json，同时记下监控 id、
模板 id 和已经落到具体账号上的 bilibili_account_id。之后再改模板或
默认账号，不会改到已经排队的任务。
"""

import json
import os
import re
import threading
import uuid
from typing import Any, Dict, List, Optional

from y2b.utils import get_app_subdir

DEFAULT_TEMPLATE_ID = "default"
DEFAULT_TEMPLATE_NAME = "默认模板"

PARTITION_INHERIT = "inherit"
PARTITION_AI = "ai"
PARTITION_FIXED = "fixed"
PARTITION_AI_FALLBACK = "ai_fallback"
PARTITION_STRATEGIES = (
    PARTITION_INHERIT,
    PARTITION_AI,
    PARTITION_FIXED,
    PARTITION_AI_FALLBACK,
)

PLACEHOLDER_KEYS = ("title", "description", "url", "channel", "date")
_PLACEHOLDER_RE = re.compile(r"\{([a-zA-Z0-9_]+)\}")

PREVIEW_SAMPLE = {
    "title": "示例视频标题",
    "description": "示例简介正文",
    "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
    "channel": "示例频道",
    "date": "20260101",
}

_LOCK = threading.RLock()


def templates_store_path() -> str:
    return os.path.join(get_app_subdir("db"), "upload_templates.json")


def _empty_store() -> Dict[str, Any]:
    return {"templates": []}


def _read_store() -> Dict[str, Any]:
    path = templates_store_path()
    if not os.path.exists(path):
        return _empty_store()
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return _empty_store()
    if not isinstance(data, dict):
        return _empty_store()
    templates = data.get("templates")
    if not isinstance(templates, list):
        templates = []
    return {"templates": [item for item in templates if isinstance(item, dict)]}


def _write_store(store: Dict[str, Any]) -> None:
    path = templates_store_path()
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(store, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _as_bool(value, default=False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    text = str(value).strip().lower()
    if text in ("",):
        return default
    return text in ("1", "true", "yes", "on")


def _clean_text(value, limit: int = 4000) -> str:
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if len(text) > limit:
        text = text[:limit]
    return text


def normalize_template_record(data: Optional[dict], existing_id: str = "") -> Dict[str, Any]:
    """把表单或 JSON 收成一份可保存的模板。不包含运行时才解析的账号。"""
    source = data or {}
    strategy = str(source.get("partition_strategy") or PARTITION_INHERIT).strip().lower()
    if strategy not in PARTITION_STRATEGIES:
        strategy = PARTITION_INHERIT
    upload_target = str(source.get("upload_target") or "").strip().lower()
    if upload_target not in ("", "acfun", "bilibili", "both"):
        upload_target = ""
    translate_raw = source.get("translate_enabled", None)
    if translate_raw is None or str(translate_raw).strip().lower() in ("", "inherit", "default"):
        translate_enabled = None
    else:
        translate_enabled = _as_bool(translate_raw, False)
    template_id = str(existing_id or source.get("id") or "").strip()
    if not template_id or template_id == DEFAULT_TEMPLATE_ID:
        template_id = uuid.uuid4().hex[:12]
    name = _clean_text(source.get("name"), 80) or "未命名模板"
    return {
        "id": template_id,
        "name": name,
        "title_template": _clean_text(source.get("title_template") or "{title}", 500) or "{title}",
        "description_template": _clean_text(source.get("description_template") or "{description}", 4000) or "{description}",
        "append_repost_notice": _as_bool(source.get("append_repost_notice"), True),
        "repost_notice_text": _clean_text(source.get("repost_notice_text"), 1000),
        "partition_strategy": strategy,
        "fixed_partition_acfun": _clean_text(source.get("fixed_partition_acfun"), 32),
        "fixed_partition_bilibili": _clean_text(source.get("fixed_partition_bilibili"), 32),
        "preset_tags": _clean_text(source.get("preset_tags"), 2000),
        "translate_enabled": translate_enabled,
        "tone_prompt": _clean_text(source.get("tone_prompt"), 2000),
        "upload_target": upload_target,
        "bilibili_account_id": _clean_text(source.get("bilibili_account_id"), 64),
    }


def list_templates() -> List[Dict[str, Any]]:
    with _LOCK:
        return list(_read_store()["templates"])


def get_template(template_id: str) -> Optional[Dict[str, Any]]:
    wanted = str(template_id or "").strip()
    if not wanted or wanted == DEFAULT_TEMPLATE_ID:
        return None
    for item in list_templates():
        if item.get("id") == wanted:
            return dict(item)
    return None


def save_template(data: dict, template_id: str = "") -> Dict[str, Any]:
    record = normalize_template_record(data, existing_id=template_id or (data or {}).get("id") or "")
    with _LOCK:
        store = _read_store()
        replaced = False
        templates = []
        for item in store["templates"]:
            if item.get("id") == record["id"]:
                templates.append(record)
                replaced = True
            else:
                templates.append(item)
        if not replaced:
            templates.append(record)
        store["templates"] = templates
        _write_store(store)
    return dict(record)


def delete_template(template_id: str) -> tuple:
    wanted = str(template_id or "").strip()
    if not wanted or wanted == DEFAULT_TEMPLATE_ID:
        return False, "默认模板不能删除"
    monitors = monitors_using_template(wanted)
    if monitors:
        return False, "仍有监控配置在使用这个模板，请先改绑或拆开那些配置"
    with _LOCK:
        store = _read_store()
        kept = [item for item in store["templates"] if item.get("id") != wanted]
        if len(kept) == len(store["templates"]):
            return False, "模板不存在"
        store["templates"] = kept
        _write_store(store)
    return True, "模板已删除"


def monitors_using_template(template_id: str) -> List[int]:
    wanted = str(template_id or "").strip()
    if not wanted:
        return []
    db_path = os.path.join(get_app_subdir("db"), "youtube_monitor.db")
    if not os.path.exists(db_path):
        return []
    import sqlite3
    try:
        with sqlite3.connect(db_path) as conn:
            columns = [row[1] for row in conn.execute("PRAGMA table_info(monitor_configs)")]
            if "upload_template_id" not in columns:
                return []
            rows = conn.execute(
                "SELECT id FROM monitor_configs WHERE upload_template_id = ?",
                (wanted,),
            ).fetchall()
    except sqlite3.Error:
        return []
    return [int(row[0]) for row in rows]


def monitor_template_id(monitor_id) -> str:
    if monitor_id is None or str(monitor_id).strip() == "":
        return ""
    try:
        config_id = int(monitor_id)
    except (TypeError, ValueError):
        return ""
    db_path = os.path.join(get_app_subdir("db"), "youtube_monitor.db")
    if not os.path.exists(db_path):
        return ""
    import sqlite3
    try:
        with sqlite3.connect(db_path) as conn:
            columns = [row[1] for row in conn.execute("PRAGMA table_info(monitor_configs)")]
            if "upload_template_id" not in columns:
                return ""
            row = conn.execute(
                "SELECT upload_template_id FROM monitor_configs WHERE id = ?",
                (config_id,),
            ).fetchone()
    except sqlite3.Error:
        return ""
    if not row:
        return ""
    return str(row[0] or "").strip()


def _preset_from_global(config: dict) -> tuple:
    enabled = _as_bool((config or {}).get("PRESET_TAGS_ENABLED"), False)
    text = _clean_text((config or {}).get("PRESET_TAGS"), 2000)
    if not enabled:
        text = ""
    return enabled and bool(text), text if enabled else ""


def build_default_profile(config: Optional[dict], account_id: str = "") -> Dict[str, Any]:
    """按当前全局设置生成默认模板快照。未指定时行为与今天一致。"""
    config = config or {}
    preset_enabled, preset_tags = _preset_from_global(config)
    upload_target = str(config.get("UPLOAD_TARGET_DEFAULT") or "acfun").strip().lower()
    if upload_target not in ("acfun", "bilibili", "both"):
        upload_target = "acfun"
    return {
        "template_id": DEFAULT_TEMPLATE_ID,
        "template_name": DEFAULT_TEMPLATE_NAME,
        "title_template": "{title}",
        "description_template": "{description}",
        "append_repost_notice": _as_bool(config.get("UPLOAD_APPEND_REPOST_NOTICE"), True),
        "repost_notice_text": "",
        "partition_strategy": PARTITION_INHERIT,
        "recommend_partition": _as_bool(config.get("RECOMMEND_PARTITION"), False),
        "fixed_partition_acfun": _clean_text(config.get("FIXED_PARTITION_ID"), 32),
        "fixed_partition_bilibili": _clean_text(config.get("FIXED_PARTITION_ID_BILIBILI"), 32),
        "fallback_partition_acfun": "",
        "fallback_partition_bilibili": "",
        "preset_tags_enabled": preset_enabled,
        "preset_tags": preset_tags,
        "translate_title": _as_bool(config.get("TRANSLATE_TITLE"), True),
        "translate_description": _as_bool(config.get("TRANSLATE_DESCRIPTION"), True),
        "tone_prompt": "",
        "upload_target": upload_target,
        "bilibili_account_id": str(account_id or "").strip(),
    }


def freeze_named_template(template: dict, config: Optional[dict], account_id: str = "") -> Dict[str, Any]:
    """把命名模板收成入队快照。inherit 会冻结当时的全局分区和翻译开关。"""
    config = config or {}
    strategy = str(template.get("partition_strategy") or PARTITION_INHERIT).strip().lower()
    if strategy not in PARTITION_STRATEGIES:
        strategy = PARTITION_INHERIT
    global_recommend = _as_bool(config.get("RECOMMEND_PARTITION"), False)
    global_acfun = _clean_text(config.get("FIXED_PARTITION_ID"), 32)
    global_bili = _clean_text(config.get("FIXED_PARTITION_ID_BILIBILI"), 32)
    template_acfun = _clean_text(template.get("fixed_partition_acfun"), 32)
    template_bili = _clean_text(template.get("fixed_partition_bilibili"), 32)
    fallback_acfun = ""
    fallback_bili = ""
    if strategy == PARTITION_AI:
        recommend = True
        fixed_acfun = ""
        fixed_bili = ""
    elif strategy == PARTITION_FIXED:
        recommend = False
        fixed_acfun = template_acfun
        fixed_bili = template_bili
    elif strategy == PARTITION_AI_FALLBACK:
        recommend = True
        fixed_acfun = ""
        fixed_bili = ""
        fallback_acfun = template_acfun
        fallback_bili = template_bili
    else:
        recommend = global_recommend
        fixed_acfun = global_acfun
        fixed_bili = global_bili

    if template.get("translate_enabled") is None:
        translate_title = _as_bool(config.get("TRANSLATE_TITLE"), True)
        translate_description = _as_bool(config.get("TRANSLATE_DESCRIPTION"), True)
    else:
        translate_title = bool(template.get("translate_enabled"))
        translate_description = bool(template.get("translate_enabled"))

    preset_tags = _clean_text(template.get("preset_tags"), 2000)
    preset_enabled = bool(preset_tags)

    upload_target = str(template.get("upload_target") or "").strip().lower()
    if upload_target not in ("acfun", "bilibili", "both"):
        upload_target = str(config.get("UPLOAD_TARGET_DEFAULT") or "acfun").strip().lower()
    if upload_target not in ("acfun", "bilibili", "both"):
        upload_target = "acfun"

    return {
        "template_id": str(template.get("id") or ""),
        "template_name": str(template.get("name") or ""),
        "title_template": str(template.get("title_template") or "{title}"),
        "description_template": str(template.get("description_template") or "{description}"),
        "append_repost_notice": _as_bool(template.get("append_repost_notice"), True),
        "repost_notice_text": _clean_text(template.get("repost_notice_text"), 1000),
        "partition_strategy": strategy,
        "recommend_partition": recommend,
        "fixed_partition_acfun": fixed_acfun,
        "fixed_partition_bilibili": fixed_bili,
        "fallback_partition_acfun": fallback_acfun,
        "fallback_partition_bilibili": fallback_bili,
        "preset_tags_enabled": preset_enabled,
        "preset_tags": preset_tags,
        "translate_title": translate_title,
        "translate_description": translate_description,
        "tone_prompt": _clean_text(template.get("tone_prompt"), 2000),
        "upload_target": upload_target,
        "bilibili_account_id": str(account_id or "").strip(),
    }


def _lookup_account_id(requested: str, config: Optional[dict]) -> str:
    from y2b.bilibili.bilibili_accounts import resolve_account_id
    return resolve_account_id(requested, config)


def resolve_upload_profile(
    config: Optional[dict],
    template_id: Optional[str] = None,
    monitor_id=None,
    monitor_template_id_value: Optional[str] = None,
    upload_target_override: Optional[str] = None,
) -> Dict[str, Any]:
    """按 任务指定 > 监控绑定 > 全局默认 选出模板，并冻结账号 id。"""
    config = config or {}
    explicit = str(template_id or "").strip()
    bound = str(monitor_template_id_value or "").strip()
    if not bound and monitor_id is not None:
        bound = monitor_template_id(monitor_id)

    chosen = None
    if explicit and explicit != DEFAULT_TEMPLATE_ID:
        chosen = get_template(explicit)
        if chosen is None and bound and bound != DEFAULT_TEMPLATE_ID:
            chosen = get_template(bound)
    elif not explicit and bound and bound != DEFAULT_TEMPLATE_ID:
        chosen = get_template(bound)

    if chosen is None:
        account_id = _lookup_account_id("", config)
        profile = build_default_profile(config, account_id=account_id)
    else:
        account_id = _lookup_account_id(str(chosen.get("bilibili_account_id") or ""), config)
        profile = freeze_named_template(chosen, config, account_id=account_id)

    override = str(upload_target_override or "").strip().lower()
    if override in ("acfun", "bilibili", "both"):
        profile["upload_target"] = override
    return profile


def dump_profile(profile: dict) -> str:
    return json.dumps(profile or {}, ensure_ascii=False, separators=(",", ":"))


def load_task_profile(task: Optional[dict]) -> Optional[dict]:
    if not task:
        return None
    raw = task.get("upload_profile_json")
    if not raw:
        return None
    if isinstance(raw, dict):
        return dict(raw)
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def settings_for_task(config: Optional[dict], task: Optional[dict]) -> dict:
    """有入队快照时用快照覆盖分区、翻译、预设标签和转载声明。旧任务保持原配置。"""
    merged = dict(config or {})
    profile = load_task_profile(task)
    if not profile:
        return merged
    merged["TRANSLATE_TITLE"] = bool(profile.get("translate_title"))
    merged["TRANSLATE_DESCRIPTION"] = bool(profile.get("translate_description"))
    merged["UPLOAD_APPEND_REPOST_NOTICE"] = bool(profile.get("append_repost_notice", True))
    merged["RECOMMEND_PARTITION"] = bool(profile.get("recommend_partition"))
    merged["FIXED_PARTITION_ID"] = str(profile.get("fixed_partition_acfun") or "")
    merged["FIXED_PARTITION_ID_BILIBILI"] = str(profile.get("fixed_partition_bilibili") or "")
    merged["PRESET_TAGS_ENABLED"] = bool(profile.get("preset_tags_enabled"))
    merged["PRESET_TAGS"] = str(profile.get("preset_tags") or "")
    merged["METADATA_TONE_PROMPT"] = str(profile.get("tone_prompt") or "")
    merged["_UPLOAD_PROFILE"] = profile
    return merged


def render_placeholders(template: str, values: Optional[dict]) -> str:
    values = values or {}

    def replace(match):
        key = match.group(1)
        if key in PLACEHOLDER_KEYS:
            return str(values.get(key) or "")
        return match.group(0)

    return _PLACEHOLDER_RE.sub(replace, str(template or ""))


def apply_metadata_templates(profile: Optional[dict], title: str, description: str, url: str = "", channel: str = "", date: str = ""):
    """返回 (标题, 简介, 自定义转载声明)。没有快照时原样返回，声明为空。"""
    if not profile:
        return title or "", description or "", ""
    values = {
        "title": title or "",
        "description": description or "",
        "url": url or "",
        "channel": channel or "",
        "date": date or "",
    }
    rendered_title = render_placeholders(profile.get("title_template") or "{title}", values).strip()
    rendered_description = render_placeholders(profile.get("description_template") or "{description}", values)
    notice = render_placeholders(profile.get("repost_notice_text") or "", values).strip()
    if not rendered_title:
        rendered_title = title or ""
    return rendered_title, rendered_description, notice


def preview_fields(fields: Optional[dict], sample: Optional[dict] = None) -> Dict[str, str]:
    """用示例占位符渲染标题、简介和转载声明，供模板页和监控配置页预览。"""
    sample_values = dict(PREVIEW_SAMPLE)
    if sample:
        for key in PLACEHOLDER_KEYS:
            if key in sample and sample.get(key) is not None:
                sample_values[key] = str(sample.get(key) or "")
    record = normalize_template_record(fields or {}, existing_id="preview")
    profile = freeze_named_template(record, {}, account_id="")
    if str((fields or {}).get("partition_strategy") or "") == "":
        profile["partition_strategy"] = PARTITION_INHERIT
    title, description, notice = apply_metadata_templates(
        profile,
        sample_values["title"],
        sample_values["description"],
        sample_values["url"],
        sample_values["channel"],
        sample_values["date"],
    )
    from y2b.bilibili.bilibili_uploader import format_bilibili_description

    full_description = format_bilibili_description(
        base_desc=description,
        original_url=sample_values["url"],
        original_uploader=sample_values["channel"],
        original_upload_date=sample_values["date"],
        append_repost_notice=bool(profile.get("append_repost_notice", True)),
        repost_notice_text=notice,
    )
    return {
        "title": title,
        "description": full_description,
        "repost_notice": notice,
        "append_repost_notice": "1" if profile.get("append_repost_notice", True) else "0",
    }


def public_template(template: dict) -> Dict[str, Any]:
    """给页面用的模板字段。不含 Cookie 或代理。"""
    item = normalize_template_record(template, existing_id=str(template.get("id") or ""))
    item["id"] = str(template.get("id") or item["id"])
    if item["translate_enabled"] is None:
        item["translate_enabled"] = "inherit"
    elif item["translate_enabled"]:
        item["translate_enabled"] = "on"
    else:
        item["translate_enabled"] = "off"
    return item
