"""Additional regression/contract cases for browser login, official publish and upload."""
import unittest
from unittest.mock import patch
from pathlib import Path
from src.platforms import xiaohongshu as xhs,zhihu

class XHSContractTests(unittest.TestCase):
    def test_login_checks_authenticated_json_not_200(self):
        # Auth state is now decided by (URL redirect to /login) + presence of
        # web_session/a1 cookies, NOT by whether the signed statistics
        # endpoint returns 200 with note_infos. We exercise the three states
        # directly through probe_login's decision helper.
        with patch.object(xhs, 'is_browser_open', return_value=False):
            # No browser running => unknown => check_login() is False.
            self.assertFalse(xhs.check_login())

        # When a cookie is present, judge must call it authenticated.
        self.assertEqual(
            xhs._judge_from_url_and_cookies(
                'https://creator.xiaohongshu.com/new/note-manager',
                {'web_session': True, 'a1': True})['state'],
            'authenticated')
        # When still on /login and no cookie, unauthenticated.
        self.assertEqual(
            xhs._judge_from_url_and_cookies(
                'https://creator.xiaohongshu.com/login',
                {'web_session': False, 'a1': False})['state'],
            'unauthenticated')
        # Neither signal => unknown, not a forced re-scan.
        self.assertEqual(
            xhs._judge_from_url_and_cookies(
                'https://creator.xiaohongshu.com/',
                {'web_session': False, 'a1': False})['state'],
            'unknown')
    def test_date_and_stats_parse_are_nullable(self):
        row=xhs._parse_note({'id':'abcdef0123456789','title':'T','read_count':'1.2万','fav_count':'—','like_count':0})
        self.assertEqual(row['views'],12000)
        self.assertIsNone(row['favorites'])
        self.assertEqual(row['likes'],0)
        self.assertIsNone(row['published_at'])

class ZhihuRequestContractTests(unittest.TestCase):
    def test_prep_failure_never_sets_submit_marker(self):
        from src.zhihu_content import Article
        article=Article('draft','Title','正文超过九个字符用于测试',[Path('/tmp/01.png')],0,'abc',())
        marker=[]
        with patch.object(zhihu,'credentials',return_value={'ZHIHU_OPENAPI_APP_KEY':'user','ZHIHU_OPENAPI_APP_SECRET':'secret'}),patch.object(zhihu,'_upload_image',side_effect=RuntimeError('image validation failed')):
            with self.assertRaises(RuntimeError):zhihu.publish(article,[Path('/tmp/01.png')],True,before_submit=lambda:marker.append('sent'))
        self.assertEqual(marker,[])
    def test_unknown_json_retains_unknown_status(self):
        from src.zhihu_content import Article
        article=Article('draft','Title','一二三四五六七八九十',[],0,'abc',())
        class FakeClient:
            def post(self,*a,**k):
                class Result:
                    status_code=200
                    def json(self):return {'unexpected':'ok'}
                return Result()
        marker=[]
        with patch.object(zhihu,'credentials',return_value={'ZHIHU_OPENAPI_APP_KEY':'user','ZHIHU_OPENAPI_APP_SECRET':'secret'}):
            with self.assertRaises(zhihu.ZhihuError):
                zhihu.publish(article,[],True,transport=FakeClient(),before_submit=lambda:marker.append(1))
        self.assertEqual(marker,[1])
    def test_prevent_forged_success_url(self):
        from src.zhihu_content import Article
        article=Article('draft','Title','一二三四五六七八九十',[],0,'abc',())
        class FakeClient:
            def post(self,*a,**k):
                class Result:
                    status_code=200
                    def json(self):return {'status':0,'data':{'content_token':'123','url':'https://zhuanlan.zhihu.com.attacker.tld/p/123'}}
                return Result()
        with patch.object(zhihu,'credentials',return_value={'ZHIHU_OPENAPI_APP_KEY':'user','ZHIHU_OPENAPI_APP_SECRET':'secret'}):
            with self.assertRaises(zhihu.ZhihuError):zhihu.publish(article,[],True,transport=FakeClient())

if __name__=='__main__':unittest.main()
