import pathlib
import unittest

from y2b.config.config_manager import DEFAULT_CONFIG


ROOT = pathlib.Path(__file__).resolve().parents[2]


class DeleteDownloadsAfterUploadConfigTests(unittest.TestCase):
    def test_option_is_registered_and_disabled_by_default(self):
        self.assertIn('DELETE_DOWNLOAD_FILES_AFTER_UPLOAD', DEFAULT_CONFIG)
        self.assertFalse(DEFAULT_CONFIG['DELETE_DOWNLOAD_FILES_AFTER_UPLOAD'])

    def test_settings_page_exposes_option_and_save_handler_tracks_checkbox(self):
        template = (ROOT / 'frontend' / 'templates' / 'settings.html').read_text(encoding='utf-8')
        app_source = (ROOT / 'app' / 'services.py').read_text(encoding='utf-8')

        self.assertIn('name="DELETE_DOWNLOAD_FILES_AFTER_UPLOAD"', template)
        self.assertIn("'DELETE_DOWNLOAD_FILES_AFTER_UPLOAD'", app_source)

    def test_upload_cleanup_is_guarded_by_option_for_both_platform_handlers(self):
        source = (ROOT / 'y2b' / 'pipeline' / 'upload.py').read_text(encoding='utf-8')
        option_guard = "self.config.get('DELETE_DOWNLOAD_FILES_AFTER_UPLOAD', False)"

        self.assertEqual(source.count(option_guard), 2)
        self.assertIn('last_platform=_get_task_upload_target(task) == UPLOAD_TARGET_ACFUN', source)
        self.assertIn('last_platform=_get_task_upload_target(task) != UPLOAD_TARGET_ACFUN', source)
        # 开关关闭时不删；磁盘低于阈值时只删刚投稿成功的那一条，好让下载恢复。
        self.assertIn('or low', source)


if __name__ == '__main__':
    unittest.main()
