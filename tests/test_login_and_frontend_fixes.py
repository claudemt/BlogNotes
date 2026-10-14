"""Regressions for signed-in login loops, stale server reuse and browser assets.

The auth probe is now driven by (a) whether the creator root redirects to
/login and (b) whether the shared browser context actually holds a real
session cookie (web_session / a1). It no longer relies on the signed
statistics endpoint, which bare fetch() calls from page.evaluate() cannot
authenticate and was being misread as "not logged in" right after the user
scanned the QR.
"""
from __future__ import annotations
import unittest
from unittest.mock import Mock, patch
from fastapi.testclient import TestClient
from src import server
from src.platforms import xiaohongshu as xhs


def _fake_context(cookies: list[dict]):
    ctx = Mock()
    ctx.cookies.return_value = cookies
    return ctx


class ProbeTests(unittest.TestCase):
    def _run_probe(self, final_url: str, cookies: list[dict]) -> dict:
        page = Mock()
        page.url = final_url
        page.context = _fake_context(cookies)
        page.goto = Mock()
        page.wait_for_timeout = Mock()
        return xhs._probe_creator_page(page)

    def test_cookie_present_means_authenticated_regardless_of_url(self):
        state = self._run_probe(
            'https://creator.xiaohongshu.com/new/note-manager',
            [{'name': 'web_session', 'value': 'abc'},
             {'name': 'a1', 'value': 'device-xyz'}],
        )
        self.assertEqual(state['state'], 'authenticated')

    def test_login_url_without_cookie_is_unauthenticated(self):
        state = self._run_probe(
            'https://creator.xiaohongshu.com/login?redirect=...',
            [{'name': 'webId', 'value': 'visitor-only'}],
        )
        self.assertEqual(state['state'], 'unauthenticated')

    def test_neither_login_url_nor_cookie_is_unknown(self):
        # A pre-login SPA shell that has not bounced to /login yet must not be
        # read as "logged in" and must not be read as "logged out" either.
        state = self._run_probe(
            'https://creator.xiaohongshu.com/',
            [{'name': 'webId', 'value': 'visitor-only'}],
        )
        self.assertEqual(state['state'], 'unknown')

    def test_navigates_to_creator_root_not_analytics(self):
        page = Mock()
        page.url = 'https://creator.xiaohongshu.com/new/note-manager'
        page.context = _fake_context([{'name': 'web_session', 'value': 'x'}])
        xhs._probe_creator_page(page)
        # Must navigate the root, not /statistics/data-analysis, which does not
        # reliably 302 to /login when unauthenticated.
        navigated_to = page.goto.call_args.args[0]
        self.assertEqual(navigated_to, xhs.CREATOR)


class LoginEndpointTests(unittest.TestCase):
    def test_existing_login_never_opens_login_page(self):
        report = []
        job = type('Job', (), {'report': lambda self, m: report.append(m)})()
        with patch.object(server, '_job', side_effect=lambda name, fn: fn(job)), \
             patch.object(xhs, 'probe_login', return_value={'state': 'authenticated'}), \
             patch.object(xhs, 'ensure_browser') as open_browser, \
             patch.object(server, '_sync_after_login') as sync, \
             patch.object(server, '_mark_xhs_login') as confirm_login:
            server.login()
        open_browser.assert_not_called()
        confirm_login.assert_called_once_with(job)
        self.assertIn('当前账号已经登录', report)
        # An already-signed-in click still pulls profile + works.
        sync.assert_called_once_with(job)

    def test_unknown_still_attempts_to_launch_browser(self):
        # Previously an "unknown" probe (e.g. "浏览器尚未启动" on first click)
        # bailed out before ensure_browser ran, so the user saw a failure and
        # no Chrome window. Now unknown must still trigger browser launch.
        job = type('Job', (), {'report': lambda self, m: None})()
        with patch.object(server, '_job', side_effect=lambda name, fn: fn(job)), \
             patch.object(xhs, 'probe_login', return_value={'state': 'unknown', 'reason': '小红书专用浏览器尚未启动'}), \
             patch.object(xhs, 'ensure_browser') as open_browser, \
             patch.object(server, '_sync_after_login') as sync, \
             patch.object(server.time, 'sleep'), \
             patch.object(server.service.store, 'set_meta') as set_meta:
            with self.assertRaises(ValueError):
                server.login()
        open_browser.assert_called_once_with(open_login=True)
        # No successful login => no login timestamp / profile write.
        set_meta.assert_not_called()
        sync.assert_not_called()

    def test_repeated_login_click_attaches_to_the_running_job(self):
        # A second click must join the task in flight, never launch again.
        with patch.object(server.service.jobs, 'find_running',
                          return_value=type('Job', (), {'id': 'abc123'})()) as finder, \
             patch.object(server, '_job') as submit:
            result = server.login()
        finder.assert_called_once_with('小红书登录')
        submit.assert_not_called()
        self.assertEqual(result, {'ok': True, 'job': 'abc123', 'attached': True})

    def test_browser_launch_failure_is_reported_cleanly(self):
        job = type('Job', (), {'report': lambda self, m: None})()
        with patch.object(server, '_job', side_effect=lambda name, fn: fn(job)), \
             patch.object(server.service.jobs, 'find_running', return_value=None), \
             patch.object(xhs, 'probe_login', return_value={'state': 'unknown', 'reason': '浏览器未启动'}), \
             patch.object(xhs, 'ensure_browser', side_effect=xhs.PlatformError('找不到 Chrome')), \
             patch.object(server.time, 'sleep'):
            with self.assertRaisesRegex(ValueError, '无法启动小红书专用浏览器'):
                server.login()

    def test_login_poll_detects_success_without_a_verification_tab(self):
        # The poll loop must call probe_login() with no page argument (i.e. it
        # reads the shared CDP context directly), and authentication must be
        # detected from that result alone — no throwaway verification tab
        # during polling.
        job = type('Job', (), {'report': lambda self, m: None})()
        calls = []

        def fake_probe(*args, **kwargs):
            calls.append((args, kwargs))
            # First poll: still on QR page. Second poll: cookie landed.
            if len(calls) == 1:
                return {'state': 'unauthenticated', 'reason': 'still on login'}
            return {'state': 'authenticated', 'reason': 'cookie present'}

        with patch.object(server, '_job', side_effect=lambda name, fn: fn(job)), \
             patch.object(server.service.jobs, 'find_running', return_value=None), \
             patch.object(xhs, 'probe_login', side_effect=fake_probe), \
             patch.object(xhs, 'ensure_browser') as open_browser, \
             patch.object(server.time, 'sleep'), \
             patch.object(server, '_sync_after_login') as sync, \
             patch.object(server, '_mark_xhs_login') as confirm_login:
            server.login()
        open_browser.assert_called_once_with(open_login=True)
        confirm_login.assert_called_once_with(job)
        # Every poll reads the shared context: never called with a page arg.
        self.assertTrue(calls)
        self.assertTrue(all(not args for args, _ in calls))
        # Login success triggers the data pull in the same task.
        sync.assert_called_once_with(job)

    def test_check_reports_unknown_without_demanding_a_rescan(self):
        # 'unknown' is not a logout. Telling the user to scan again on a busy
        # browser is exactly what produced the endless re-login loop.
        report = []
        job = type('Job', (), {'report': lambda self, m: report.append(m)})()
        with patch.object(server, '_job', side_effect=lambda name, fn: fn(job)), \
             patch.object(server.service.jobs, 'find_running', return_value=None), \
             patch.object(xhs, 'probe_login', return_value={'state': 'unknown', 'reason': '浏览器忙'}), \
             patch.object(server, '_mark_xhs_login') as confirm_login, \
             patch.object(server, '_sync_after_login') as sync:
            server.check()
        confirm_login.assert_not_called()
        sync.assert_not_called()
        self.assertTrue(any('状态未知' in m for m in report))

    def test_check_positive_logout_raises(self):
        job = type('Job', (), {'report': lambda self, m: None})()
        with patch.object(server, '_job', side_effect=lambda name, fn: fn(job)), \
             patch.object(server.service.jobs, 'find_running', return_value=None), \
             patch.object(xhs, 'probe_login', return_value={'state': 'unauthenticated', 'reason': '停在登录页'}):
            with self.assertRaisesRegex(ValueError, '未登录'):
                server.check()


class FrontendRegressionTests(unittest.TestCase):
    def test_actual_http_html_has_refresh_and_cache_busting(self):
        client = TestClient(server.app, base_url='http://127.0.0.1:8767')
        r = client.get('/')
        self.assertEqual(r.status_code, 200)
        self.assertIn('id="refresh-btn"', r.text)
        self.assertIn('>刷新</button>', r.text)
        self.assertNotIn('__UI_REV__', r.text)
        self.assertIn('no-store', r.headers['cache-control'])
        for resource in ('app.js', 'style.css'):
            result = client.get('/static/' + resource)
            self.assertEqual(result.status_code, 200)
            self.assertIn('no-store', result.headers['cache-control'])
        health = client.get('/api/health').json()
        self.assertEqual(health['build_id'], server.BOOT_FINGERPRINT)
        self.assertEqual(health['ui_revision'],
                         server.hashlib.sha256(server.BOOT_FINGERPRINT.encode()).hexdigest()[:12])

    def test_launcher_rejects_older_server_instead_of_opening_it(self):
        ps = (server.ROOT / 'src' / 'launch.ps1').read_text(encoding='utf-8')
        for marker in ('Stop-OwnListener', '$Health.service -ne $ServiceName',
                       'occupied by another service'):
            self.assertIn(marker, ps)


if __name__ == '__main__':
    unittest.main()
