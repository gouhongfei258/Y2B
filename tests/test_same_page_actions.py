"""同页操作：带 X-Requested-With 的请求返回 JSON，普通表单仍重定向。

登录、退出，以及编辑页「保存并上传」这种真正离开当前页的动作保持跳转。
"""
import pathlib
import unittest
from unittest.mock import patch

import app as web_app
from werkzeug.datastructures import MultiDict


AJAX_HEADERS = {
    'X-Requested-With': 'XMLHttpRequest',
    'Accept': 'application/json',
}

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


class SamePageActionRouteTests(unittest.TestCase):
    def setUp(self):
        web_app.app.config['TESTING'] = True
        self.client = web_app.app.test_client()

    def _open_config(self):
        return patch.object(web_app, 'load_config', return_value={'password_protection_enabled': False})

    def test_add_task_validation_stays_json_for_ajax_and_redirects_otherwise(self):
        with self._open_config():
            ajax = self.client.post('/tasks/add', data={'youtube_url': ''}, headers=AJAX_HEADERS)
            plain = self.client.post('/tasks/add', data={'youtube_url': ''})

        self.assertEqual(ajax.status_code, 400)
        payload = ajax.get_json()
        self.assertFalse(payload['success'])
        self.assertIn('不能为空', payload['message'])
        self.assertNotIn('redirect', payload)

        self.assertEqual(plain.status_code, 302)
        self.assertTrue(plain.headers['Location'].endswith('/tasks'))

    def test_start_task_ajax_returns_fragment_without_navigation(self):
        task = {
            'id': 'task-1',
            'status': web_app.TASK_STATES['PENDING'],
            'upload_target': 'acfun',
        }
        with self._open_config(), \
             patch.object(web_app, 'get_task', return_value=task), \
             patch.object(web_app, 'start_task', return_value=True) as start_mock, \
             patch.object(web_app, '_render_task_fragments', return_value={
                 'row_html': '<tr data-task-id="task-1"></tr>',
                 'card_html': '<div class="task-card" data-task-id="task-1"></div>',
             }):
            ajax = self.client.post('/tasks/task-1/start', headers=AJAX_HEADERS)
            plain = self.client.post('/tasks/task-1/start')

        self.assertEqual(ajax.status_code, 200)
        payload = ajax.get_json()
        self.assertTrue(payload['success'])
        self.assertIn('row_html', payload)
        self.assertNotIn('redirect', payload)
        self.assertEqual(plain.status_code, 302)
        self.assertTrue(plain.headers['Location'].endswith('/tasks'))
        self.assertEqual(start_mock.call_count, 2)

    def test_edit_save_ajax_stays_on_page_and_upload_still_navigates(self):
        task = {
            'id': 'edited-review-task',
            'status': web_app.TASK_STATES['AWAITING_REVIEW'],
            'upload_target': 'bilibili',
            'selected_partition_id_bilibili': '123',
        }
        with self._open_config(), \
             patch.object(web_app, 'get_task', return_value=task), \
             patch.object(web_app, 'update_task', return_value=True), \
             patch.object(web_app, '_start_background_force_upload', lambda *args, **kwargs: None), \
             patch.object(web_app, '_missing_upload_partition_labels', lambda *args, **kwargs: []):
            saved = self.client.post(
                '/tasks/edited-review-task/edit',
                data={'action': 'save_metadata', 'video_title_translated': '标题', 'tags_json': '[]'},
                headers=AJAX_HEADERS,
            )
            uploaded = self.client.post(
                '/tasks/edited-review-task/edit',
                data=MultiDict([('action', 'save_metadata'), ('action', 'force_upload')]),
                headers=AJAX_HEADERS,
            )
            plain_upload = self.client.post(
                '/tasks/edited-review-task/edit',
                data=MultiDict([('action', 'force_upload')]),
            )

        saved_payload = saved.get_json()
        self.assertEqual(saved.status_code, 200)
        self.assertTrue(saved_payload['success'])
        self.assertIn('保存', saved_payload['message'])
        self.assertNotIn('redirect', saved_payload)
        self.assertIn('status_label', saved_payload)

        uploaded_payload = uploaded.get_json()
        self.assertEqual(uploaded.status_code, 200)
        self.assertIn('manual_review', uploaded_payload.get('redirect', ''))

        self.assertEqual(plain_upload.status_code, 302)
        self.assertIn('manual_review', plain_upload.headers.get('Location', ''))

    def test_settings_maintenance_and_reset_do_not_reload(self):
        with self._open_config(), \
             patch.object(web_app, 'cleanup_logs', return_value={
                 'success': True,
                 'files_removed': 2,
                 'bytes_freed_readable': '1KB',
             }):
            cleanup = self.client.post('/logs/cleanup', data={'hours': '24'}, headers=AJAX_HEADERS)

        self.assertEqual(cleanup.status_code, 200)
        cleanup_payload = cleanup.get_json()
        self.assertTrue(cleanup_payload['success'])
        self.assertNotIn('redirect', cleanup_payload)

        sample_key = 'LOG_CLEANUP_HOURS'
        with self._open_config(), \
             patch.object(web_app, 'reset_specific_config', return_value={sample_key: 72}) as reset_mock:
            reset = self.client.post('/settings/reset', json={'keys': [sample_key]}, headers=AJAX_HEADERS)

        self.assertEqual(reset.status_code, 200)
        reset_payload = reset.get_json()
        self.assertEqual(reset_payload['status'], 'success')
        self.assertEqual(reset_payload['values'][sample_key], 72)
        reset_mock.assert_called_once_with([sample_key])

    def test_logout_still_redirects_even_for_ajax(self):
        token = 't' * 32
        with self.client.session_transaction() as session:
            session['csrf_token'] = token
            session['logged_in'] = True
        with self._open_config():
            response = self.client.post('/logout', data={'csrf_token': token}, headers=AJAX_HEADERS)
        self.assertEqual(response.status_code, 302)
        self.assertIn('/login', response.headers.get('Location', ''))


class SamePageTemplateTests(unittest.TestCase):
    def test_same_page_forms_opt_in_and_navigation_forms_do_not(self):
        tasks = (REPO_ROOT / 'frontend' / 'templates' / 'tasks.html').read_text(encoding='utf-8')
        base = (REPO_ROOT / 'frontend' / 'templates' / 'base.html').read_text(encoding='utf-8')
        login = (REPO_ROOT / 'frontend' / 'templates' / 'login.html').read_text(encoding='utf-8')
        settings = (REPO_ROOT / 'frontend' / 'templates' / 'settings.html').read_text(encoding='utf-8')
        monitor = (REPO_ROOT / 'frontend' / 'templates' / 'youtube_monitor.html').read_text(encoding='utf-8')

        self.assertIn('data-same-page="refresh-tasks"', tasks)
        self.assertIn('data-same-page="toast"', settings)
        self.assertIn('applyResetValues', settings)
        self.assertNotIn('window.location.reload()', settings)
        self.assertIn('data-same-page="refresh-region"', monitor)
        self.assertNotIn('window.location.reload()', monitor)
        self.assertNotIn('data-same-page', login)
        logout_form = base.split("url_for('logout')", 1)[1].split('</form>', 1)[0]
        self.assertNotIn('data-same-page', logout_form)
        banner = (REPO_ROOT / 'frontend' / 'templates' / 'partials' / 'runtime_gates_banner.html').read_text(encoding='utf-8')
        self.assertIn('data-same-page="refresh-gates"', banner)
        self.assertIn("{% include 'partials/runtime_gates_banner.html' %}", tasks)


class DownloadGateSamePageTests(unittest.TestCase):
    def setUp(self):
        web_app.app.config['TESTING'] = True
        self.client = web_app.app.test_client()

    def test_download_pause_ajax_stays_on_page(self):
        with patch.object(web_app, 'load_config', return_value={'password_protection_enabled': False}), \
             patch('modules.pipeline.gates.pause_downloads'):
            ajax = self.client.post(
                '/downloads/pause',
                data={'next': 'tasks'},
                headers=AJAX_HEADERS,
            )
            plain = self.client.post('/downloads/pause', data={'next': 'tasks'})
        self.assertEqual(ajax.status_code, 200)
        payload = ajax.get_json()
        self.assertTrue(payload['success'])
        self.assertIn('暂停下载', payload['message'])
        self.assertNotIn('redirect', payload)
        self.assertEqual(plain.status_code, 302)
        self.assertTrue(plain.headers['Location'].endswith('/tasks'))


if __name__ == '__main__':
    unittest.main()
