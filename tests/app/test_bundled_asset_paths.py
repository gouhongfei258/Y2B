"""Bundled AcFun mapping and subtitle fonts stay on their new paths."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from y2b.subtitles.ass_layout import SubtitleLayoutMixin
from y2b.utils import (
    get_acfun_id_mapping_path,
    get_subtitle_fonts_dir,
    locate_bundled_dir,
)

ROOT = Path(__file__).resolve().parents[2]


class BundledAssetPathTests(unittest.TestCase):
    def test_repo_root_no_longer_holds_moved_directories(self):
        self.assertFalse((ROOT / "acfunid").exists())
        self.assertFalse((ROOT / "fonts").exists())
        mapping = ROOT / "y2b" / "acfun" / "acfunid" / "id_mapping.json"
        fonts = ROOT / "frontend" / "fonts"
        self.assertTrue(mapping.is_file())
        self.assertTrue((fonts / "NotoSansCJKsc-Regular.otf").is_file())
        self.assertTrue((fonts / "SourceHanSansHWSC-VF.otf").is_file())
        self.assertTrue((fonts / "LICENSE.txt").is_file())

    def test_dev_resolvers_use_package_and_frontend_paths(self):
        fonts = ROOT / "frontend" / "fonts"
        mapping = ROOT / "y2b" / "acfun" / "acfunid" / "id_mapping.json"
        self.assertEqual(Path(get_subtitle_fonts_dir()), fonts)
        self.assertEqual(Path(get_acfun_id_mapping_path()), mapping)
        with mapping.open(encoding="utf-8") as handle:
            payload = json.load(handle)
        self.assertIsInstance(payload, list)
        self.assertTrue(payload)

        names = {Path(path).name for path in SubtitleLayoutMixin._iter_bundled_font_paths()}
        self.assertIn("NotoSansCJKsc-Regular.otf", names)
        self.assertIn("SourceHanSansHWSC-VF.otf", names)
        for path in SubtitleLayoutMixin._iter_bundled_font_paths():
            self.assertEqual(Path(path).parent, fonts)

    def test_frozen_build_finds_assets_in_meipass_or_beside_exe(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            exe = root / "Y2A-Auto.exe"
            exe.write_bytes(b"")
            meipass = root / "_internal"
            meipass_fonts = meipass / "frontend" / "fonts"
            meipass_fonts.mkdir(parents=True)
            (meipass_fonts / "NotoSansCJKsc-Regular.otf").write_bytes(b"otf")
            meipass_mapping = meipass / "y2b" / "acfun" / "acfunid"
            meipass_mapping.mkdir(parents=True)
            (meipass_mapping / "id_mapping.json").write_text("[]", encoding="utf-8")

            # An empty directory next to the executable must not hide packaged files.
            (root / "frontend" / "fonts").mkdir(parents=True)
            (root / "y2b" / "acfun" / "acfunid").mkdir(parents=True)

            with patch.object(sys, "frozen", True, create=True), patch.object(
                sys, "executable", str(exe), create=True
            ), patch.object(sys, "_MEIPASS", str(meipass), create=True):
                self.assertEqual(Path(get_subtitle_fonts_dir()), meipass_fonts)
                self.assertEqual(
                    Path(get_acfun_id_mapping_path()),
                    meipass_mapping / "id_mapping.json",
                )

            exe_fonts = root / "frontend" / "fonts"
            (exe_fonts / "NotoSansCJKsc-Regular.otf").write_bytes(b"otf")
            exe_mapping = root / "y2b" / "acfun" / "acfunid"
            (exe_mapping / "id_mapping.json").write_text("[]", encoding="utf-8")
            with patch.object(sys, "frozen", True, create=True), patch.object(
                sys, "executable", str(exe), create=True
            ), patch.object(sys, "_MEIPASS", None, create=True):
                self.assertEqual(Path(locate_bundled_dir("frontend", "fonts")), exe_fonts)
                self.assertEqual(
                    Path(get_acfun_id_mapping_path()),
                    exe_mapping / "id_mapping.json",
                )
