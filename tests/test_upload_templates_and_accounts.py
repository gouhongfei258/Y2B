#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""投稿模板、bilibili 多账号、代理和冷却。"""

import json
import os
import tempfile
import unittest
from unittest import mock

from modules.bilibili_uploader import format_bilibili_description
from modules.upload_templates import (
    DEFAULT_TEMPLATE_ID,
    resolve_upload_profile,
    save_template,
)


def _config():
    return {
        "UPLOAD_TARGET_DEFAULT": "acfun",
        "UPLOAD_APPEND_REPOST_NOTICE": True,
        "RECOMMEND_PARTITION": False,
        "FIXED_PARTITION_ID": "",
        "FIXED_PARTITION_ID_BILIBILI": "36",
        "PRESET_TAGS_ENABLED": True,
        "PRESET_TAGS": "科技",
        "TRANSLATE_TITLE": False,
        "TRANSLATE_DESCRIPTION": True,
        "BILIBILI_COOKIES_PATH": "cookies/bili_cookies.json",
        "BILIBILI_PREUPLOAD_COOLDOWN_MINUTES": 30,
        "BILIBILI_SUBMIT_COOLDOWN_MINUTES": 15,
        "BILIBILI_RISK_COOLDOWN_MINUTES": 30,
    }


class UploadTemplateResolutionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.template_path = os.path.join(self.temp.name, "templates.json")
        self.account_path = os.path.join(self.temp.name, "accounts.json")
        self.patches = [
            mock.patch("modules.upload_templates.templates_store_path", return_value=self.template_path),
            mock.patch("modules.bilibili_accounts.accounts_store_path", return_value=self.account_path),
        ]
        for patcher in self.patches:
            patcher.start()
        from modules.bilibili_accounts import ensure_accounts
        ensure_accounts(_config())

    def tearDown(self):
        for patcher in self.patches:
            patcher.stop()
        self.temp.cleanup()

    def test_fallback_order_and_enqueue_stamp_stays_put(self):
        science = save_template({
            "name": "科技频道",
            "title_template": "【科技】{title}",
            "description_template": "{description}\n{url}",
            "append_repost_notice": True,
            "repost_notice_text": "转载自{channel}",
            "partition_strategy": "ai_fallback",
            "fixed_partition_bilibili": "188",
            "fixed_partition_acfun": "86",
            "preset_tags": "数码",
            "translate_enabled": "on",
            "tone_prompt": "口语化",
            "upload_target": "bilibili",
            "bilibili_account_id": "",
        })
        life = save_template({
            "name": "生活",
            "title_template": "生活-{title}",
            "upload_target": "both",
            "partition_strategy": "fixed",
            "fixed_partition_bilibili": "21",
        })

        default_profile = resolve_upload_profile(_config())
        self.assertEqual(default_profile["template_id"], DEFAULT_TEMPLATE_ID)
        self.assertEqual(default_profile["title_template"], "{title}")
        self.assertTrue(default_profile["append_repost_notice"])
        self.assertEqual(default_profile["fixed_partition_bilibili"], "36")
        self.assertEqual(default_profile["preset_tags"], "科技")
        self.assertFalse(default_profile["translate_title"])
        self.assertTrue(default_profile["translate_description"])
        self.assertEqual(default_profile["upload_target"], "acfun")
        self.assertEqual(default_profile["bilibili_account_id"], "default")

        monitor_profile = resolve_upload_profile(
            _config(),
            monitor_template_id_value=science["id"],
        )
        self.assertEqual(monitor_profile["template_id"], science["id"])
        self.assertEqual(monitor_profile["title_template"], "【科技】{title}")
        self.assertEqual(monitor_profile["upload_target"], "bilibili")
        self.assertTrue(monitor_profile["recommend_partition"])
        self.assertEqual(monitor_profile["fixed_partition_bilibili"], "")
        self.assertEqual(monitor_profile["fallback_partition_bilibili"], "188")
        self.assertEqual(monitor_profile["bilibili_account_id"], "default")

        task_profile = resolve_upload_profile(
            _config(),
            template_id=life["id"],
            monitor_template_id_value=science["id"],
            upload_target_override="acfun",
        )
        self.assertEqual(task_profile["template_id"], life["id"])
        self.assertEqual(task_profile["upload_target"], "acfun")
        self.assertFalse(task_profile["recommend_partition"])
        self.assertEqual(task_profile["fixed_partition_bilibili"], "21")

        explicit_default = resolve_upload_profile(
            _config(),
            template_id=DEFAULT_TEMPLATE_ID,
            monitor_template_id_value=science["id"],
        )
        self.assertEqual(explicit_default["template_id"], DEFAULT_TEMPLATE_ID)
        self.assertEqual(explicit_default["title_template"], "{title}")

        missing = resolve_upload_profile(
            _config(),
            template_id="missing-template",
            monitor_template_id_value=science["id"],
        )
        self.assertEqual(missing["template_id"], science["id"])

        stamped = dict(monitor_profile)
        save_template({
            "id": science["id"],
            "name": "科技频道",
            "title_template": "改过的{title}",
            "upload_target": "both",
        })
        from modules.bilibili_accounts import create_account
        other = create_account("第二个账号", _config())
        with open(self.account_path, "r", encoding="utf-8") as handle:
            store = json.load(handle)
        for item in store["accounts"]:
            item["is_default"] = item["id"] == other["id"]
        with open(self.account_path, "w", encoding="utf-8") as handle:
            json.dump(store, handle)

        self.assertEqual(stamped["title_template"], "【科技】{title}")
        self.assertEqual(stamped["bilibili_account_id"], "default")
        self.assertEqual(stamped["upload_target"], "bilibili")
        again = resolve_upload_profile(_config(), monitor_template_id_value=science["id"])
        self.assertEqual(again["title_template"], "改过的{title}")
        self.assertEqual(again["bilibili_account_id"], other["id"])
        self.assertNotEqual(again["bilibili_account_id"], stamped["bilibili_account_id"])

    def test_add_task_persists_stamp(self):
        science = save_template({
            "name": "科技频道",
            "title_template": "【科技】{title}",
            "upload_target": "bilibili",
        })
        db_path = os.path.join(self.temp.name, "tasks.db")
        import modules.pipeline.store as store
        with mock.patch.object(store, "DB_PATH", db_path), \
                mock.patch("modules.task_manager.get_global_task_processor", return_value=None):
            store.init_db()
            task_id = store.add_task(
                "https://www.youtube.com/watch?v=abcdefghijk",
                monitor_id=7,
                template_id=science["id"],
            )
            row = store.get_task(task_id)
        self.assertEqual(row["source_monitor_id"], "7")
        self.assertEqual(row["upload_template_id"], science["id"])
        self.assertEqual(row["bilibili_account_id"], "default")
        self.assertEqual(row["upload_target"], "bilibili")
        profile = json.loads(row["upload_profile_json"])
        self.assertEqual(profile["title_template"], "【科技】{title}")

        save_template({
            "id": science["id"],
            "name": "科技频道",
            "title_template": "改过的{title}",
            "upload_target": "acfun",
        })
        with mock.patch.object(store, "DB_PATH", db_path):
            stored = store.get_task(task_id)
        self.assertEqual(json.loads(stored["upload_profile_json"])["title_template"], "【科技】{title}")
        self.assertEqual(stored["bilibili_account_id"], "default")
        self.assertEqual(stored["upload_target"], "bilibili")


class BilibiliAccountTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.account_path = os.path.join(self.temp.name, "accounts.json")
        self.cooldown_path = os.path.join(self.temp.name, "cooldowns.json")
        self.cookie = os.path.join(self.temp.name, "bili_cookies.json")
        with open(self.cookie, "w", encoding="utf-8") as handle:
            handle.write("{}\n")
        self.patches = [
            mock.patch("modules.bilibili_accounts.accounts_store_path", return_value=self.account_path),
            mock.patch("modules.bilibili_accounts.cooldowns_store_path", return_value=self.cooldown_path),
        ]
        for patcher in self.patches:
            patcher.start()

    def tearDown(self):
        for patcher in self.patches:
            patcher.stop()
        self.temp.cleanup()

    def test_existing_cookie_file_becomes_default_account(self):
        from modules.bilibili_accounts import create_account, ensure_accounts, get_default_account
        config = {"BILIBILI_COOKIES_PATH": self.cookie}
        first = ensure_accounts(config)
        self.assertEqual(len(first["accounts"]), 1)
        account = first["accounts"][0]
        self.assertEqual(account["id"], "default")
        self.assertEqual(account["name"], "默认账号")
        self.assertEqual(account["cookie_path"], self.cookie)
        self.assertFalse(account["proxy_enabled"])
        self.assertTrue(os.path.exists(self.cookie))
        second = ensure_accounts(config)
        self.assertEqual(second["accounts"][0]["id"], "default")
        other = create_account("备用", config)
        self.assertNotEqual(other["cookie_path"], self.cookie)
        self.assertTrue(other["cookie_path"].endswith(other["id"] + ".json"))
        self.assertEqual(get_default_account(config)["id"], "default")

    def test_proxy_off_is_direct_and_ignores_env(self):
        from modules.bili_sdk.utils.network import bili_request_scope, get_client, request_settings
        from modules.bilibili_accounts import build_account_proxy, request_scope_for_account
        disabled = {
            "proxy_enabled": False,
            "proxy_url": "http://secret-user:secret-pass@127.0.0.1:7890",
            "proxy_username": "secret-user",
            "proxy_password": "secret-pass",
        }
        scope = request_scope_for_account(disabled)
        self.assertEqual(scope["proxy"], "")
        self.assertFalse(scope["trust_env"])
        self.assertEqual(build_account_proxy(disabled), "")

        enabled = {
            "proxy_enabled": True,
            "proxy_url": "socks5://127.0.0.1:1080",
            "proxy_username": "alice",
            "proxy_password": "s3cret",
        }
        enabled_scope = request_scope_for_account(enabled)
        self.assertFalse(enabled_scope["trust_env"])
        self.assertTrue(enabled_scope["proxy"].startswith("socks5://"))
        self.assertIn("alice", enabled_scope["proxy"])
        self.assertIn("s3cret", enabled_scope["proxy"])
        self.assertNotIn("secret-pass", enabled_scope["proxy"])

        original_proxy = request_settings.get_proxy()
        original_trust = request_settings.get_trust_env()
        try:
            with mock.patch.dict(os.environ, {"HTTPS_PROXY": "http://env-user:env-pass@127.0.0.1:8888"}, clear=False):
                with bili_request_scope(scope["proxy"], scope["trust_env"]):
                    direct_client = get_client()
                with bili_request_scope(enabled_scope["proxy"], enabled_scope["trust_env"]):
                    proxy_client = get_client()
            self.assertIsNot(direct_client, proxy_client)
            self.assertFalse(direct_client.get_wrapped_session().trust_env)
            self.assertFalse(proxy_client.get_wrapped_session().trust_env)
            self.assertEqual(direct_client.get_wrapped_session().proxies.get("all"), "")
            self.assertEqual(proxy_client.get_wrapped_session().proxies.get("all"), enabled_scope["proxy"])
            self.assertEqual(request_settings.get_proxy(), original_proxy)
            self.assertEqual(request_settings.get_trust_env(), original_trust)
        finally:
            request_settings.set_proxy(original_proxy)
            request_settings.set_trust_env(original_trust)

    def test_cooldown_is_isolated_per_account(self):
        from modules.bilibili_accounts import (
            active_cooldowns,
            cooldown_block_message,
            create_account,
            ensure_accounts,
            record_account_cooldown,
        )
        ensure_accounts(_config())
        other = create_account("备用", _config())
        now = 1_700_000_000
        first_until = record_account_cooldown("default", 406, _config(), now=now)
        self.assertEqual(first_until, now + 30 * 60)
        again = record_account_cooldown("default", 406, _config(), now=now + 60)
        self.assertEqual(again, first_until)
        record_account_cooldown("default", 137022, _config(), now=now)
        self.assertEqual(active_cooldowns(other["id"], now=now + 10), [])
        default_codes = {item["code"] for item in active_cooldowns("default", now=now + 10)}
        self.assertEqual(default_codes, {406, 137022})
        blocked = cooldown_block_message(
            {"id": "default", "name": "默认账号", "uname": "科技号"},
            _config(),
            now=now + 10,
        )
        self.assertIn("默认账号", blocked)
        self.assertIn("科技号", blocked)
        self.assertIn("406", blocked)
        self.assertNotIn(other["id"], blocked)
        record_account_cooldown(other["id"], 603, {"BILIBILI_RISK_COOLDOWN_MINUTES": 30}, now=now)
        other_codes = {item["code"] for item in active_cooldowns(other["id"], now=now + 5)}
        self.assertEqual(other_codes, {603})
        self.assertNotIn(603, {item["code"] for item in active_cooldowns("default", now=now + 5)})

    def test_custom_repost_notice_replaces_fixed_text(self):
        default_text = format_bilibili_description(
            "正文",
            original_url="https://www.youtube.com/watch?v=abcdefghijk",
            original_uploader="Example",
            original_upload_date="20260101",
        )
        self.assertIn("本视频转载自YouTube", default_text)
        custom = format_bilibili_description(
            "正文",
            original_url="https://www.youtube.com/watch?v=abcdefghijk",
            original_uploader="Example",
            original_upload_date="20260101",
            repost_notice_text="转载自Example",
        )
        self.assertTrue(custom.startswith("转载自Example"))
        self.assertNotIn("本视频转载自YouTube", custom)


if __name__ == "__main__":
    unittest.main()
