#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""上传线路、Cookie 粘贴、下载闸门和 Bilibili 冷却。"""

import asyncio
import json
import logging
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from modules.bili_sdk.video_uploader import (
    LINES_INFO,
    Lines,
    VideoUploader,
    _probe,
    resolve_upload_line,
)
from modules.bilibili_uploader import BilibiliUploader
from modules.config_manager import DEFAULT_CONFIG
from modules.pipeline import gates
from modules.tools.convert_cookies import cookies_export_to_netscape_text
from modules.youtube_handler import (
    import_pasted_youtube_cookies,
    youtube_download_gate_reason,
)


class _GateStateMixin(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        gates.override_state_path(os.path.join(self._tmp.name, "runtime_gates.json"))
        gates.set_clock(None)
        self.addCleanup(gates.override_state_path, None)
        self.addCleanup(gates.set_clock, None)


class UploadLineTests(unittest.TestCase):
    def test_six_lines_keep_old_probe_versions(self):
        self.assertEqual(
            set(LINES_INFO),
            {"bda2", "bldsa", "qn", "ws", "txa", "alia"},
        )
        for name in ("bda2", "bldsa", "qn", "ws"):
            self.assertEqual(LINES_INFO[name]["probe_version"], 20221109)
        for name in ("txa", "alia"):
            info = LINES_INFO[name]
            self.assertEqual(info["probe_version"], 20250923)
            self.assertIn("probe_version=20250923", info["query"])
            self.assertIn(f"upcdn={name}", info["query"])
            self.assertIn("zone=cs", info["query"])
            self.assertEqual(info["probe_url"], f"//upos-cs-upcdn{name}.bilivideo.com/OK")
        self.assertEqual(Lines.TXA.value, "txa")
        self.assertEqual(Lines.ALIA.value, "alia")
        self.assertEqual(resolve_upload_line("auto"), None)
        self.assertEqual(resolve_upload_line("txa"), Lines.TXA)
        self.assertEqual(DEFAULT_CONFIG["BILIBILI_UPLOAD_LINE"], "auto")

    def test_probe_covers_all_six_lines(self):
        seen = []

        class _Client:
            async def request(self, **kwargs):
                seen.append(kwargs["url"])
                return SimpleNamespace(code=200)

        with patch("modules.bili_sdk.video_uploader.get_client", return_value=_Client()):
            fastest = asyncio.run(_probe())

        self.assertEqual(len(seen), 6)
        joined = "\n".join(seen)
        self.assertIn("upcdntxa", joined)
        self.assertIn("upcdnalia", joined)
        self.assertIn("upcdnbldsa", joined)
        self.assertIn(fastest["upcdn"], {item["upcdn"] for item in LINES_INFO.values()})

    def test_switch_endpoint_accepts_bldsa_txa_and_alia(self):
        for upcdn in ("bda2", "bldsa", "qn", "ws", "txa", "alia"):
            preupload = {"endpoint": f"//upos-cs-upcdn{upcdn}.bilivideo.com"}
            switched = VideoUploader._switch_upload_endpoint(preupload, {"upcdn": "txa"})
            self.assertEqual(switched["endpoint"], "//upos-cs-upcdntxa.bilivideo.com")
        untouched = {"endpoint": "//upload.example.com"}
        self.assertEqual(
            VideoUploader._switch_upload_endpoint(untouched, {"upcdn": "alia"})["endpoint"],
            "//upload.example.com",
        )


class CookiePasteTests(unittest.TestCase):
    def test_header_json_and_netscape_imports_do_not_log_values(self):
        secret = "super-secret-sapisid-value"
        header = f"SID=sid-value; SAPISID={secret}; HSID=hsid-value"
        payload = json.dumps([
            {
                "name": "SAPISID",
                "value": secret,
                "domain": ".youtube.com",
                "path": "/",
                "secure": True,
                "expirationDate": 1893456000,
            }
        ])
        netscape = (
            "# Netscape HTTP Cookie File\n"
            f".youtube.com\tTRUE\t/\tTRUE\t1893456000\tSAPISID\t{secret}\n"
        )
        records = []
        handler = logging.Handler()
        handler.emit = lambda record: records.append(record.getMessage())
        logger = logging.getLogger("modules.youtube_handler")
        previous_level = logger.level
        logger.setLevel(logging.INFO)
        logger.addHandler(handler)
        self.addCleanup(logger.removeHandler, handler)
        self.addCleanup(logger.setLevel, previous_level)

        with tempfile.TemporaryDirectory() as directory:
            for index, text in enumerate((header, payload, netscape)):
                path = os.path.join(directory, f"cookies-{index}.txt")
                result = import_pasted_youtube_cookies(text, path)
                with open(path, encoding="utf-8") as handle:
                    body = handle.read()
                self.assertTrue(result["ok"], result)
                self.assertTrue(result["authenticated"], result)
                self.assertIn("SAPISID", body)
                self.assertNotIn(secret, result["message"])
                self.assertGreaterEqual(body.splitlines()[1].count("\t"), 6)

        logged = "\n".join(records)
        self.assertNotIn(secret, logged)
        self.assertIn("登录态完整", logged)

    def test_incomplete_login_mentions_httponly_without_values(self):
        text = cookies_export_to_netscape_text("VISITOR_INFO1_LIVE=abc")
        self.assertIsNotNone(text)
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "yt_cookies.txt")
            result = import_pasted_youtube_cookies("VISITOR_INFO1_LIVE=abc", path)
        self.assertTrue(result["ok"])
        self.assertFalse(result["authenticated"])
        self.assertIn("document.cookie", result["message"])
        self.assertNotIn("abc", result["message"])


class BotDetectorTests(unittest.TestCase):
    def test_plain_403_player_and_format_errors_do_not_trip(self):
        samples = (
            "ERROR: HTTP Error 403: Forbidden",
            "Some formats may be missing",
            "player response was empty",
            "Requested format is not available",
            "Got error: HTTP Error 403. Retrying fragment 3",
            "The page needs to be reloaded.",
        )
        for sample in samples:
            self.assertIsNone(youtube_download_gate_reason(sample), sample)

    def test_confirmed_bot_and_rate_limit_trip(self):
        self.assertEqual(
            youtube_download_gate_reason("Sign in to confirm you’re not a bot"),
            "YouTube 人机验证/验证码",
        )
        self.assertEqual(
            youtube_download_gate_reason("HTTP Error 429: Too Many Requests"),
            "YouTube 拒绝访问（频率限制）",
        )


class GateTests(_GateStateMixin):
    def test_cooldown_fail_fast_does_not_call_bilibili_or_refresh_timer(self):
        now = {"value": 1_000_000.0}
        gates.set_clock(lambda: now["value"])
        gates.arm_bilibili_406(30)
        until_406 = gates.read_state()["bili_406"]["until"]
        gates.arm_bilibili_137022(15)
        until_submit = gates.read_state()["bili_137022"]["until"]

        # 窗口内再次碰到同类拒绝，不刷新到期时间，两个计时器互不影响。
        now["value"] += 60
        again_406, refreshed_406 = gates.arm_bilibili_406(30)
        again_submit, refreshed_submit = gates.arm_bilibili_137022(15)
        state = gates.read_state()
        self.assertFalse(refreshed_406)
        self.assertFalse(refreshed_submit)
        self.assertEqual(state["bili_406"]["until"], until_406)
        self.assertEqual(state["bili_137022"]["until"], until_submit)
        self.assertAlmostEqual(again_406, until_406 - now["value"], places=3)
        self.assertAlmostEqual(again_submit, until_submit - now["value"], places=3)

        called = {"remote": 0}

        def _remote(*_args, **_kwargs):
            called["remote"] += 1
            return False, "should not run"

        with patch("modules.bilibili_uploader.validate_credential_remote", side_effect=_remote):
            ok, message = BilibiliUploader("cookies.json").upload_video(
                "missing.mp4", "missing.jpg", "title", "desc", ["tag"], "21",
            )
        self.assertFalse(ok)
        self.assertIn("406", message)
        self.assertIn("未请求 Bilibili", message)
        self.assertEqual(called["remote"], 0)
        self.assertEqual(gates.read_state()["bili_406"]["until"], until_406)

        gates.read_state()
        # 只留下 137022，确认本地失败文案换成频率限制，且仍然不打接口。
        state = gates.read_state()
        state["bili_406"]["until"] = 0
        gates._save_unlocked(state)
        with patch("modules.bilibili_uploader.validate_credential_remote", side_effect=_remote):
            ok, message = BilibiliUploader("cookies.json").upload_video(
                "missing.mp4", "missing.jpg", "title", "desc", ["tag"], "21",
            )
        self.assertFalse(ok)
        self.assertIn("137022", message)
        self.assertIn("未请求投稿接口", message)
        self.assertEqual(called["remote"], 0)
        self.assertEqual(gates.read_state()["bili_137022"]["until"], until_submit)

    def test_disk_threshold_holds_and_releases(self):
        self.assertEqual(DEFAULT_CONFIG["DOWNLOAD_DISK_FREE_THRESHOLD_GB"], 10)
        threshold = 10 * 1024 ** 3

        class _Usage:
            def __init__(self, free):
                self.free = free

        with patch("modules.pipeline.gates.shutil.disk_usage", return_value=_Usage(5 * 1024 ** 3)):
            self.assertTrue(gates.poll_disk("/tmp", threshold))
        self.assertIn("disk-low", gates.download_block_reasons())

        with patch("modules.pipeline.gates.shutil.disk_usage", return_value=_Usage(20 * 1024 ** 3)):
            released = gates.maybe_release_disk_hold("/tmp", threshold)
        self.assertTrue(released)
        self.assertNotIn("disk-low", gates.download_block_reasons())

    def test_manual_pause_is_independent_of_auto_mode(self):
        config = {"AUTO_MODE_ENABLED": True}
        self.assertFalse(gates.manual_pause_active())
        gates.pause_downloads()
        self.assertTrue(config["AUTO_MODE_ENABLED"])
        self.assertEqual(gates.download_block_reasons(), ["manual"])
        self.assertTrue(gates.task_needs_download({"status": "pending", "video_path_local": ""}))
        self.assertFalse(gates.task_needs_download({"status": "encoding_video"}))
        self.assertFalse(gates.task_needs_download({"status": "uploading"}))
        gates.resume_downloads()
        self.assertFalse(gates.downloads_blocked())
        self.assertTrue(config["AUTO_MODE_ENABLED"])

    def test_bot_backoff_persists_and_caps_at_one_hour(self):
        now = {"value": 5_000.0}
        gates.set_clock(lambda: now["value"])
        first = gates.trip_youtube_bot_circuit("YouTube 人机验证/验证码")
        self.assertEqual(first["retries"], 1)
        self.assertEqual(first["until"], now["value"] + 60)
        saved = gates.read_state()["bot"]["until"]
        self.assertEqual(saved, first["until"])

        now["value"] = first["until"] + 1
        second = gates.trip_youtube_bot_circuit("YouTube 人机验证/验证码")
        self.assertEqual(second["retries"], 2)
        self.assertEqual(second["until"] - now["value"], 120)

        gates.reset_bot_circuit()
        for _ in range(8):
            now["value"] = gates.read_state()["bot"]["until"] + 1
            gates.trip_youtube_bot_circuit("YouTube 拒绝访问（频率限制）")
        self.assertEqual(gates.read_state()["bot"]["until"] - (now["value"]), 3600)
        gates.skip_bot_wait()
        self.assertGreater(gates.read_state()["bot"]["retries"], 0)
        self.assertFalse(gates.downloads_blocked())


if __name__ == "__main__":
    unittest.main()
