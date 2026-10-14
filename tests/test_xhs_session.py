"""Regressions for the Xiaohongshu session layer.

These cover the four field problems this change fixes:
  * a still-booting page must not be read as "logged out" (tri-state probe),
  * a verified login must survive a busy/absent browser (positive-only cache),
  * a second click must not launch a second Chrome or a second login tab,
  * a degraded work list must not be reported as complete, and must not wipe
    real interaction numbers.

Everything here is offline and mock-based; no test opens a browser.
"""
from __future__ import annotations
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

from fastapi.testclient import TestClient

from src import server, service
from src.platforms import xiaohongshu as xhs


class WwwLoginStateTests(unittest.TestCase):
    def _state(self, signals: dict) -> dict:
        page = Mock()
        page.evaluate.return_value = signals
        return xhs._www_login_state(page)

    def test_channel_element_means_authenticated(self):
        self.assertEqual(self._state({'channel': True, 'state_defined': True})['state'],
                         'authenticated')

    def test_identity_means_authenticated(self):
        state = self._state({'identity': {'userId': 'u1', 'nickname': 'N'}, 'state_defined': True})
        self.assertEqual(state['state'], 'authenticated')
        self.assertEqual(state['user_id'], 'u1')

    def test_visible_login_modal_means_unauthenticated(self):
        self.assertEqual(self._state({'modal': True, 'state_defined': True})['state'],
                         'unauthenticated')

    def test_booting_page_is_unknown_not_logged_out(self):
        # The regression: a half-loaded SPA used to report "logged out" and
        # made the UI demand a fresh QR scan right after a successful one.
        self.assertEqual(self._state({'state_defined': False})['state'], 'unknown')

    def test_evaluate_failure_is_unknown(self):
        page = Mock()
        page.evaluate.side_effect = RuntimeError('detached')
        self.assertEqual(xhs._www_login_state(page)['state'], 'unknown')


class PortOwnershipTests(unittest.TestCase):
    """Sharing a debug port with an unrelated Chrome must not mean driving it."""

    def setUp(self):
        xhs._OWNER_CACHE.update(port=None, verdict='unknown', checked_at=0.0)

    def _verdict(self, command_line: str) -> str:
        with patch.object(xhs, 'is_browser_open', return_value=True), \
             patch.object(xhs, '_cdp_port_pid', return_value=4242), \
             patch.object(xhs, '_process_command_line', return_value=command_line):
            return xhs.cdp_port_owner(refresh=True)

    def test_our_own_profile_is_recognised(self):
        self.assertEqual(
            self._verdict(f'chrome.exe --remote-debugging-port=9237 '
                          f'--user-data-dir={xhs.PROFILE_DIR} about:blank'),
            'ours')

    def test_another_profile_is_foreign(self):
        self.assertEqual(
            self._verdict('chrome.exe --remote-debugging-port=9237 '
                          '--user-data-dir=C:\\Users\\someone\\AppData\\Local\\Chrome'),
            'foreign')

    def test_unreadable_command_line_is_unknown_not_foreign(self):
        # No PowerShell, no /proc: we must not block the user on a guess.
        self.assertEqual(self._verdict(''), 'unknown')

    def test_unknown_pid_is_unknown(self):
        with patch.object(xhs, 'is_browser_open', return_value=True), \
             patch.object(xhs, '_cdp_port_pid', return_value=None):
            self.assertEqual(xhs.cdp_port_owner(refresh=True), 'unknown')

    def test_foreign_owner_blocks_reuse_instead_of_hijacking(self):
        with patch.object(xhs, 'is_browser_open', return_value=True), \
             patch.object(xhs, '_cdp_port_pid', return_value=4242), \
             patch.object(xhs, '_process_command_line',
                          return_value='chrome.exe --user-data-dir=C:\\other'):
            with self.assertRaisesRegex(xhs.PlatformError, '其它账号'):
                xhs.ensure_browser()

    def test_foreign_owner_blocks_data_reads(self):
        with patch.object(xhs, 'is_browser_open', return_value=True), \
             patch.object(xhs, '_cdp_port_pid', return_value=4242), \
             patch.object(xhs, '_process_command_line',
                          return_value='chrome.exe --user-data-dir=C:\\other'):
            with self.assertRaises(xhs.PlatformError):
                with xhs.session():
                    pass

    def test_foreign_owner_never_reports_someone_elses_account(self):
        with patch.object(xhs, 'is_browser_open', return_value=True), \
             patch.object(xhs, '_cdp_port_pid', return_value=4242), \
             patch.object(xhs, '_process_command_line',
                          return_value='chrome.exe --user-data-dir=C:\\other'):
            result = xhs.probe_login()
        self.assertEqual(result['state'], 'unknown')
        self.assertIn('占用', result['reason'])


class LoginCacheTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.cache = Path(self.dir.name) / 'cache.json'
        patcher = patch.object(xhs, 'CACHE_FILE', self.cache)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_negative_result_is_never_stored(self):
        xhs._set_login_cache('www', False)
        self.assertIsNone(xhs._cached_login('www'))
        self.assertFalse(self.cache.exists())

    def test_positive_result_hits_and_clears(self):
        xhs._set_login_cache('www', True)
        self.assertTrue(xhs._cached_login('www'))
        xhs._set_login_cache('www', False)
        self.assertIsNone(xhs._cached_login('www'))

    def test_expired_entry_is_a_miss(self):
        stored = {'entries': {xhs._cache_key('www'): {'logged_in': True, 'checked_at': 1000.0}}}
        self.cache.write_text(json.dumps(stored), encoding='utf-8')
        with patch.object(xhs.time, 'time', return_value=1000.0 + xhs._cache_ttl_seconds() + 1):
            self.assertIsNone(xhs._cached_login('www'))

    def test_cache_is_scoped_per_port(self):
        xhs._set_login_cache('www', True)
        with patch.object(xhs, '_cache_key', return_value='9999:www'):
            self.assertIsNone(xhs._cached_login('www'))

    def test_corrupt_cache_file_is_ignored(self):
        self.cache.write_text('{not json', encoding='utf-8')
        self.assertIsNone(xhs._cached_login('www'))


class ProbeLoginTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.cache = Path(self.dir.name) / 'cache.json'
        patcher = patch.object(xhs, 'CACHE_FILE', self.cache)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_closed_browser_with_cache_is_authenticated(self):
        xhs._set_login_cache('www', True)
        with patch.object(xhs, 'is_browser_open', return_value=False):
            result = xhs.probe_login()
        self.assertEqual(result['state'], 'authenticated')
        self.assertTrue(result['cached'])

    def test_closed_browser_without_cache_is_unknown_never_unauthenticated(self):
        with patch.object(xhs, 'is_browser_open', return_value=False):
            result = xhs.probe_login()
        self.assertEqual(result['state'], 'unknown')

    def test_cdp_transport_error_never_means_logged_out(self):
        with patch.object(xhs, 'is_browser_open', return_value=True), \
             patch('playwright.sync_api.sync_playwright',
                   side_effect=RuntimeError('connection refused')):
            result = xhs.probe_login()
        self.assertEqual(result['state'], 'unknown')

    def test_check_login_bypasses_the_cache(self):
        # A read-only CLI check must report the live browser, not a stored guess.
        xhs._set_login_cache('www', True)
        with patch.object(xhs, 'is_browser_open', return_value=False):
            self.assertFalse(xhs.check_login())

    def _probe_with_booting_page(self, **kwargs) -> dict:
        page = Mock()
        page.url = 'https://www.xiaohongshu.com/explore'
        page.evaluate.return_value = {'state_defined': False}
        context = Mock()
        context.pages = [page]
        context.cookies.return_value = []
        browser = Mock()
        browser.contexts = [context]
        pw = MagicMock()
        pw.chromium.connect_over_cdp.return_value = browser
        cm = MagicMock()
        cm.__enter__.return_value = pw
        with patch.object(xhs, 'is_browser_open', return_value=True), \
             patch('playwright.sync_api.sync_playwright', return_value=cm):
            return xhs.probe_login(**kwargs)

    def test_unknown_probe_never_downgrades_a_verified_session(self):
        # A still-booting page must not flip a verified login to logged-out and
        # send the user back to the QR code.
        xhs._set_login_cache('www', True)
        result = self._probe_with_booting_page()
        self.assertNotEqual(result['state'], 'unauthenticated')
        self.assertTrue(xhs._cached_login('www'))

    def test_unknown_probe_does_not_write_a_cache_entry(self):
        result = self._probe_with_booting_page(use_cache=False)
        self.assertEqual(result['state'], 'unknown')
        self.assertFalse(self.cache.exists())


class LoginSessionSingletonTests(unittest.TestCase):
    def test_a_new_session_cancels_the_previous_one(self):
        seq1, cancel1 = xhs.begin_login_session()
        seq2, cancel2 = xhs.begin_login_session()
        self.assertNotEqual(seq1, seq2)
        self.assertTrue(cancel1.is_set())
        self.assertFalse(cancel2.is_set())
        self.assertTrue(xhs.login_pending())

    def test_stale_session_cannot_clear_a_newer_one(self):
        seq1, _ = xhs.begin_login_session()
        xhs.begin_login_session()
        xhs.end_login_session(seq1)
        self.assertTrue(xhs.login_pending())


class BrowserSingletonTests(unittest.TestCase):
    def test_open_port_is_reused(self):
        with patch.object(xhs, 'is_browser_open', return_value=True), \
             patch.object(xhs.subprocess, 'Popen') as popen:
            xhs.ensure_browser()
        popen.assert_not_called()

    def test_one_launch_for_repeated_calls(self):
        states = iter([False, True, True, True])
        with patch.object(xhs, 'is_browser_open', side_effect=lambda: next(states)), \
             patch.object(xhs.time, 'sleep'), \
             patch.object(xhs, '_bring_xhs_tab_to_front'), \
             patch.object(xhs, 'subprocess') as sp:
            sp.Popen.return_value = Mock(pid=1234, poll=lambda: None)
            sp.DEVNULL = -3
            xhs.ensure_browser()
            xhs.ensure_browser()
        self.assertEqual(sp.Popen.call_count, 1)

    def test_concurrent_calls_launch_once(self):
        launched = threading.Event()

        def fake_open():
            return launched.is_set()

        def fake_popen(*args, **kwargs):
            launched.set()
            return Mock(pid=7, poll=lambda: None)

        with patch.object(xhs, 'is_browser_open', side_effect=fake_open), \
             patch.object(xhs.time, 'sleep'), \
             patch.object(xhs, '_bring_xhs_tab_to_front'), \
             patch.object(xhs, 'subprocess') as sp:
            sp.Popen.side_effect = fake_popen
            sp.DEVNULL = -3
            threads = [threading.Thread(target=xhs.ensure_browser) for _ in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        self.assertEqual(sp.Popen.call_count, 1)


class LoginTabDedupeTests(unittest.TestCase):
    def setUp(self):
        xhs._LAST_TAB_OPEN['at'] = 0.0

    def _run(self, pages):
        context = Mock()
        context.pages = pages
        browser = Mock()
        browser.contexts = [context]
        pw = MagicMock()
        pw.chromium.connect_over_cdp.return_value = browser
        cm = MagicMock()
        cm.__enter__.return_value = pw
        with patch('playwright.sync_api.sync_playwright', return_value=cm):
            xhs._open_login_tab_if_needed()
        return context

    def test_existing_www_tab_is_focused_not_reloaded(self):
        www = Mock()
        www.url = 'https://www.xiaohongshu.com/explore'
        context = self._run([www])
        www.bring_to_front.assert_called_once()
        www.goto.assert_not_called()
        context.new_page.assert_not_called()

    def test_blank_tab_is_reused_instead_of_opening_another(self):
        blank = Mock()
        blank.url = 'about:blank'
        context = self._run([blank])
        context.new_page.assert_not_called()
        blank.goto.assert_called_once()


class SynchronizeMergeTests(unittest.TestCase):
    def _run(self, stats, posted, status):
        with patch.object(service.xhs, 'list_statistics', return_value=stats), \
             patch.object(service.xhs, 'list_published', return_value=posted), \
             patch.object(service.xhs, 'inventory_status', return_value=status), \
             patch.object(service, '_reconcile', return_value=(0, 0)), \
             patch.object(service, 'remote') as remote_mod, \
             patch.object(service, 'store') as store_mod:
            service.synchronize()
        rows = remote_mod.update.call_args.args[1]
        return {r['xhs_id']: r for r in rows}

    def test_degraded_source_does_not_wipe_real_metrics(self):
        rows = self._run(
            stats=[{'xhs_id': 'A' * 24, 'views': 18, 'likes': 2}],
            posted=[{'xhs_id': 'A' * 24, 'title': 'T', 'xsec_token': 'tok', '_source': 'www/profile'}],
            status={'partial': True, 'sources': ['www/profile'], 'reason': 'x'},
        )
        row = rows['A' * 24]
        self.assertEqual(row['views'], 18)
        self.assertEqual(row['likes'], 2)
        self.assertEqual(row['xsec_token'], 'tok')
        self.assertTrue(row['partial'])

    def test_www_only_note_carries_no_metrics_at_all(self):
        note = xhs._normalize_www_note({
            'id': 'B' * 24, 'xsecToken': 'tok',
            'noteCard': {'displayTitle': '标题', 'cover': {'urlDefault': 'https://x/y.jpg'}},
        })
        for key in ('views', 'likes', 'favorites', 'comments', 'shares', 'impressions'):
            self.assertNotIn(key, note)
        self.assertEqual(note['xsec_token'], 'tok')
        self.assertEqual(note['title'], '标题')


class EmptyAccountTests(unittest.TestCase):
    """A brand-new account answers "0 works" — that is not a failed scrape."""

    def tearDown(self):
        xhs._reset_inventory_status()

    def test_zero_works_is_not_reported_as_partial(self):
        xhs._reset_inventory_status()
        # The statistics endpoint refused (the usual 406) ...
        xhs._note_inventory(source='creator/statistics',
                            reason='创作者中心统计接口未返回数据（可能被风控拒绝，非登录问题）')
        # ... but the creator list answered, and the answer was "no works".
        xhs._note_inventory(source='creator/note-manager', works_ok=True, empty=True,
                            reason='该账号暂无已发布作品')
        status = xhs.inventory_status()
        self.assertTrue(status['works_ok'])
        self.assertTrue(status['empty'])
        self.assertFalse(status['partial'])

    def test_real_works_without_metrics_is_still_partial(self):
        xhs._reset_inventory_status()
        xhs._note_inventory(source='www/profile', works_ok=True,
                            reason='作品取自个人主页，无互动指标')
        status = xhs.inventory_status()
        self.assertTrue(status['partial'])
        self.assertFalse(status['empty'])

    def test_empty_creator_response_yields_an_empty_complete_inventory(self):
        import contextlib

        handlers = {}
        page = Mock()
        page.url = 'https://creator.xiaohongshu.com/new/note-manager?source=official'
        page.context = Mock()
        page.on.side_effect = lambda ev, fn: handlers.__setitem__(ev, fn)
        page.wait_for_timeout = Mock()
        page.evaluate = Mock()
        page.remove_listener = Mock()

        response = Mock()
        response.url = ('https://creator.xiaohongshu.com'
                        '/api/galaxy/v2/creator/note/user/posted?tab=0&page=0')
        response.json.return_value = {'code': 0, 'success': True,
                                      'data': {'notes': [], 'tags': [], 'page': -1}}

        def fake_goto(url, **kwargs):
            handlers['response'](response)

        page.goto.side_effect = fake_goto

        @contextlib.contextmanager
        def fake_session(*args, **kwargs):
            yield page

        xhs._reset_inventory_status()
        with patch.object(xhs, 'session', fake_session), \
             patch.object(xhs, '_ensure_authenticated'), \
             patch.object(xhs, '_is_login_url', return_value=False), \
             patch.object(xhs, '_list_published_from_www', return_value=[]):
            result = xhs.list_published()
        self.assertEqual(result, [])
        status = xhs.inventory_status()
        self.assertTrue(status['empty'])
        self.assertFalse(status['partial'])


class MetricCoverageTests(unittest.TestCase):
    """The creator API returns more than we used to keep; keep all of it."""

    def test_parse_note_reads_the_full_metric_set(self):
        note = xhs._parse_note({
            'id': 'C' * 24, 'title': '标题',
            'imp_count': 100, 'read_count': 80, 'like_count': 5,
            'fav_count': 3, 'comment_count': 2, 'share_count': 1,
            'increase_fans_count': 4, 'coverClickRate': 0.12,
            'view_time_avg': 31, 'danmaku_count': 6,
        })
        self.assertEqual(note['impressions'], 100)
        self.assertEqual(note['followers'], 4)
        self.assertEqual(note['click_rate'], 0.12)
        self.assertEqual(note['view_time_avg'], 31)
        self.assertEqual(note['danmaku'], 6)

    def test_alternative_field_spellings_are_tolerated(self):
        note = xhs._parse_note({'id': 'D' * 24, 'avg_view_time': 12, 'danmaku': 3})
        self.assertEqual(note['view_time_avg'], 12)
        self.assertEqual(note['danmaku'], 3)

    def test_new_metrics_survive_storage_and_presentation_layers(self):
        from src import presentation, remote
        for key in ('click_rate', 'view_time_avg', 'danmaku'):
            self.assertIn(key, remote.METRICS)
            self.assertIn(key, presentation.STATS)


class ProfileIdentityTests(unittest.TestCase):
    def test_user_id_comes_from_the_creator_cookie(self):
        context = Mock()
        context.cookies.return_value = [
            {'name': 'galaxy_creator_session_id', 'value': 'sess'},
            {'name': 'x-user-id-creator.xiaohongshu.com', 'value': '678c92de000000000d00a939'},
        ]
        self.assertEqual(xhs._creator_user_id(context), '678c92de000000000d00a939')

    def test_dotted_cookie_names_are_not_filtered_out(self):
        context = Mock()
        context.cookies.return_value = [
            {'name': 'x-user-id-creator.xiaohongshu.com', 'value': '678c92de000000000d00a939'}]
        self.assertEqual(xhs._creator_user_id(context), '678c92de000000000d00a939')

    def test_nonsense_cookie_is_rejected(self):
        context = Mock()
        context.cookies.return_value = [
            {'name': 'x-user-id-creator.xiaohongshu.com', 'value': 'not-an-id'}]
        self.assertEqual(xhs._creator_user_id(context), '')


class FrontendTests(unittest.TestCase):
    def test_partial_banner_and_click_guard_are_wired(self):
        client = TestClient(server.app, base_url='http://127.0.0.1:8767')
        self.assertIn('id="xhs-partial"', client.get('/').text)
        app = (Path(server.ROOT) / 'src' / 'web' / 'app.js').read_text(encoding='utf-8')
        self.assertIn("dataset.busy", app)
        self.assertIn("guard($('xhs-login')", app)
        self.assertIn("guard($('refresh-btn')", app)

    def test_empty_and_partial_banners_say_different_things(self):
        # An account with no works must not be told its metrics failed to load.
        app = (Path(server.ROOT) / 'src' / 'web' / 'app.js').read_text(encoding='utf-8')
        self.assertIn('暂无已发布作品', app)
        self.assertIn('部分数据：作品取自个人主页，无互动指标', app)
        self.assertIn('inv?.empty', app)


class PublishTopicsTests(unittest.TestCase):
    """Topics must become real chips without ever costing us the publish."""

    def test_trailing_tag_line_is_removed_from_the_body(self):
        body = '原始正文\n\n#旅行 #风景'
        self.assertEqual(xhs._strip_trailing_topics(body), '原始正文')

    def test_tag_inside_prose_is_left_alone(self):
        body = '今天去了 #旅行 很开心'
        self.assertEqual(xhs._strip_trailing_topics(body), body)

    def test_body_without_tags_is_unchanged(self):
        self.assertEqual(xhs._strip_trailing_topics('只有正文'), '只有正文')

    def test_stripping_never_leaves_trailing_blank_lines(self):
        self.assertEqual(xhs._strip_trailing_topics('正文\n\n\n#旅行\n\n'), '正文')

    def _page(self, options: list):
        """A page whose locator() yields one suggestion per call, then none."""
        page = Mock()
        page.keyboard = Mock()
        remaining = list(options)

        def locator(css):
            loc = Mock()
            if remaining:
                loc.first = Mock(count=Mock(return_value=1),
                                 is_visible=Mock(return_value=True))
                loc.first.click = Mock(side_effect=lambda **kw: remaining.pop(0))
            else:
                loc.first = Mock(count=Mock(return_value=0))
            return loc

        page.locator.side_effect = locator
        page.wait_for_timeout = Mock()
        return page

    def test_a_suggestion_is_accepted_for_every_topic(self):
        page = self._page(['ok', 'ok'])
        editor = Mock()
        result = xhs._apply_topics(page, editor, ['旅行', '风景'], report=lambda m: None)
        self.assertEqual(result['attached'], ['旅行', '风景'])
        self.assertEqual(result['literal'], [])

    def test_a_topic_without_a_suggestion_stays_as_text_instead_of_vanishing(self):
        # No suggestion at all: the "#tag" already typed must be left in place,
        # not retyped (that would duplicate it) and not dropped. The wait is
        # stubbed out so the test does not sit through the real suggestion poll.
        page = self._page([])
        editor = Mock()
        seen = []
        with patch.object(xhs, '_wait_topic_option', return_value=None):
            result = xhs._apply_topics(page, editor, ['旅行'], report=seen.append)
        self.assertEqual(result['attached'], [])
        self.assertEqual(result['literal'], ['旅行'])
        typed = ''.join(str(c.args[0]) for c in page.keyboard.type.call_args_list)
        self.assertEqual(typed.count('#旅行'), 1)
        self.assertTrue(any('按文字保留' in m for m in seen))

    def test_topic_failure_never_reaches_the_publish_button(self):
        # A broken editor must not turn into a failed publish: the tag is
        # reported as unattached and the caller keeps going.
        page = Mock()
        page.keyboard = Mock()
        editor = Mock()
        editor.click.side_effect = RuntimeError('no editor')
        seen = []
        result = xhs._apply_topics(page, editor, ['旅行'], report=seen.append)
        self.assertEqual(result['attached'], [])
        # The tag is accounted for rather than quietly disappearing, and the
        # report says so.
        self.assertEqual(set(result['literal']) | set(result['untouched']), {'旅行'})
        self.assertTrue(any('未能写入' in m for m in seen), seen)

    def test_publish_strips_the_tag_line_and_attaches_it(self):
        page = Mock()
        page.evaluate.return_value = None
        page.wait_for_timeout = Mock()
        page.keyboard = Mock()
        editor = Mock()
        filled = []
        editor.fill.side_effect = lambda value: filled.append(value)
        button = Mock()
        button.is_enabled.return_value = True
        with patch.object(xhs, 'session') as sess, \
             patch.object(xhs, '_ensure_authenticated'), \
             patch.object(xhs, '_is_login_url', return_value=False), \
             patch.object(xhs, '_upload'), \
             patch.object(xhs, '_select_first', side_effect=[Mock(), editor, button]), \
             patch.object(xhs, '_apply_topics') as apply_topics:
            sess.return_value.__enter__.return_value = page
            xhs.publish('标题', '正文\n\n#旅行', [Path('a.png')],
                        report=lambda m: None, before_submit=lambda: None,
                        topics=['旅行'])
        # The tag line is removed from the filled text so it is not duplicated.
        self.assertEqual(filled, ['正文'])
        self.assertEqual(apply_topics.call_args.args[2], ['旅行'])

    def test_publish_without_topics_fills_the_body_untouched(self):
        page = Mock()
        page.evaluate.return_value = None
        page.wait_for_timeout = Mock()
        editor = Mock()
        filled = []
        editor.fill.side_effect = lambda value: filled.append(value)
        button = Mock()
        button.is_enabled.return_value = True
        with patch.object(xhs, 'session') as sess, \
             patch.object(xhs, '_ensure_authenticated'), \
             patch.object(xhs, '_is_login_url', return_value=False), \
             patch.object(xhs, '_upload'), \
             patch.object(xhs, '_select_first', side_effect=[Mock(), editor, button]), \
             patch.object(xhs, '_apply_topics') as apply_topics:
            sess.return_value.__enter__.return_value = page
            xhs.publish('标题', '正文\n\n#旅行', [Path('a.png')],
                        report=lambda m: None, before_submit=lambda: None)
        self.assertEqual(filled, ['正文\n\n#旅行'])
        apply_topics.assert_not_called()

    def test_service_passes_the_note_tags_through(self):
        source = Mock(title='T', body='B', images=[], tags=['旅行'], fingerprint='f',
                      slug='s', modified_at=1)
        source.issues = []
        with patch.object(service, 'get', return_value=({'status': 'draft', 'uid': 'u'}, source)), \
             patch.object(service, 'prepare_pngs', return_value=[]), \
             patch.object(service, 'store') as store, \
             patch.object(service.xhs, 'publish', side_effect=RuntimeError('stop here')) as publish:
            store.note.return_value = {'status': 'draft'}
            with self.assertRaises(RuntimeError):
                service.publish_one('u')
        self.assertEqual(publish.call_args.kwargs['topics'], ['旅行'])

    def test_service_passes_the_note_tags_through(self):
        with patch.object(service, 'get') as get, \
             patch.object(service, 'prepare_pngs', return_value=[]), \
             patch.object(service, 'store') as store, \
             patch.object(service.xhs, 'publish', side_effect=RuntimeError('stop here')) as publish:
            source = Mock(title='T', body='B', images=[], tags=['旅行'], fingerprint='f',
                          slug='s', modified_at=1)
            source.issues = []
            get.return_value = ({'status': 'draft', 'uid': 'u'}, source)
            store.note.return_value = {'status': 'draft'}
            with self.assertRaises(RuntimeError):
                service.publish_one('u')
        self.assertEqual(publish.call_args.kwargs['topics'], ['旅行'])


class CommentFetchTests(unittest.TestCase):
    """Reading comments must be honest about what it actually got.

    The page script maps the site's own field names (`likeCount`,
    `subComments`, epoch `createTime`) into the flat shape used here, so these
    fixtures supply that post-script shape — the mapping itself is only
    exercised by a real page. What is checked here is everything downstream:
    normalisation, truncation honesty and the DOM-fallback downgrade.
    """

    NOTE = 'a' * 24

    def _page(self, state):
        page = Mock()
        page.evaluate.side_effect = lambda script, arg=None: (
            state if 'noteDetailMap' in script else None)
        page.wait_for_timeout = Mock()
        page.goto = Mock()
        return page

    def _fetch(self, state, limit=20, replies=10):
        page = self._page(state)
        with patch.object(xhs, 'session') as sess, \
             patch.object(xhs, '_scroll_comments') as scroll:
            sess.return_value.__enter__.return_value = page
            result = xhs.fetch_comments(self.NOTE, 'tok', limit=limit, replies=replies)
        return result, scroll

    def test_state_tree_is_read_with_timestamps_normalised(self):
        state = {'list': [{'id': 'c1', 'content': '很好', 'user': {'user_id': 'u1', 'nickname': '小明'},
                           'like_count': 3, 'created_at': 1735689600000,
                           'replies': [{'id': 'r1', 'content': '同意',
                                        'user': {'user_id': 'u2', 'nickname': '小红'},
                                        'like_count': 0, 'created_at': 1735689600000}]}],
                 'has_more': False}
        result, _ = self._fetch(state)
        self.assertEqual(result['count'], 1)
        self.assertFalse(result['partial'])
        comment = result['comments'][0]
        self.assertEqual(comment['user']['nickname'], '小明')
        self.assertEqual(comment['like_count'], 3)
        self.assertEqual(comment['created_at'], '2025-01-01T00:00:00+00:00')
        self.assertEqual(comment['replies'][0]['content'], '同意')

    def test_more_available_than_returned_is_partial(self):
        state = {'list': [{'id': 'c1', 'content': 'a', 'user': {}, 'replies': []},
                          {'id': 'c2', 'content': 'b', 'user': {}, 'replies': []}],
                 'has_more': True}
        result, _ = self._fetch(state, limit=1)
        self.assertEqual(result['count'], 1)
        self.assertTrue(result['partial'], '截断的评论不能被当作全部')
        self.assertTrue(result['has_more'])

    def test_dom_fallback_is_reported_as_partial(self):
        # No state tree at all: the scrape carries no ids or timestamps, so the
        # caller must be told these are not the real records.
        page = Mock()
        page.wait_for_timeout = Mock()
        page.goto = Mock()
        page.evaluate.side_effect = lambda script, arg=None: (
            None if 'noteDetailMap' in script
            else [{'id': '', 'content': '抓到的', 'user': {'nickname': '路人'},
                   'like_count': 0, 'created_at': None, 'replies': []}])
        with patch.object(xhs, 'session') as sess, patch.object(xhs, '_scroll_comments'):
            sess.return_value.__enter__.return_value = page
            result = xhs.fetch_comments(self.NOTE)
        self.assertTrue(result['partial'])
        self.assertEqual(result['comments'][0]['content'], '抓到的')

    def test_no_comments_at_all_is_not_partial(self):
        with patch.object(xhs, 'session') as sess, patch.object(xhs, '_scroll_comments'):
            page = Mock()
            page.wait_for_timeout = Mock()
            page.goto = Mock()
            page.evaluate.return_value = None
            sess.return_value.__enter__.return_value = page
            result = xhs.fetch_comments(self.NOTE)
        self.assertEqual((result['count'], result['partial']), (0, False))

    def test_invalid_note_id_is_refused_before_opening_a_browser(self):
        with patch.object(xhs, 'session') as sess:
            with self.assertRaises(xhs.PlatformError):
                xhs.fetch_comments('../../etc/passwd')
        sess.assert_not_called()

    def test_reply_count_is_capped_per_comment(self):
        state = {'list': [{'id': 'c1', 'content': 'a', 'user': {}, 'replies':
                           [{'id': f'r{i}', 'content': str(i), 'user': {}} for i in range(5)]}],
                 'has_more': False}
        result, _ = self._fetch(state, replies=2)
        self.assertEqual(len(result['comments'][0]['replies']), 2)


class CommentEndpointTests(unittest.TestCase):
    def test_non_xhs_note_is_refused(self):
        client = TestClient(server.app, base_url='http://127.0.0.1:8767')
        with patch.object(server.workspace, 'resolve', side_effect=KeyError('笔记不存在')):
            self.assertEqual(client.get('/api/notes/nope/comments').status_code, 404)

    def test_unpublished_note_is_refused_without_opening_a_browser(self):
        client = TestClient(server.app, base_url='http://127.0.0.1:8767')
        platform = Mock(key='xhs')
        platform.get.return_value = ({'status': 'draft', 'xhs_id': None}, Mock())
        with patch.object(server.workspace, 'resolve', return_value=platform), \
             patch.object(server.xhs, 'fetch_comments') as fetch:
            r = client.get('/api/notes/u1/comments')
        self.assertEqual(r.status_code, 409)
        fetch.assert_not_called()

    def test_published_note_passes_the_stored_xsec_token(self):
        client = TestClient(server.app, base_url='http://127.0.0.1:8767')
        platform = Mock(key='xhs')
        platform.get.return_value = ({'status': 'published', 'xhs_id': 'a' * 24}, Mock())
        remote_row = {'metadata': {'xsec_token': 'TOKEN'}}
        with patch.object(server.workspace, 'resolve', return_value=platform), \
             patch.object(server.remote, 'get', return_value=remote_row), \
             patch.object(server.xhs, 'fetch_comments',
                          return_value={'comments': [], 'count': 0, 'partial': False}) as fetch:
            r = client.get('/api/notes/u1/comments')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(fetch.call_args.args[1], 'TOKEN')

    def test_frontend_renders_comments_as_text_not_html(self):
        app = (Path(server.ROOT) / 'src' / 'web' / 'app.js').read_text(encoding='utf-8')
        self.assertIn('/comments', app)
        # Comment bodies are other people's input: they must go in as text.
        self.assertIn('body.textContent = c.content', app)
        index = (Path(server.ROOT) / 'src' / 'web' / 'index.html').read_text(encoding='utf-8')
        self.assertIn('id="comment-list"', index)


if __name__ == '__main__':
    unittest.main()
