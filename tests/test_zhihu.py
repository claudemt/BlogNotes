"""Offline contract tests. Live Zhihu credentials and network are NOT required."""
from __future__ import annotations
import tempfile, unittest, hashlib, hmac,base64
from pathlib import Path
from unittest.mock import patch
from PIL import Image
from fastapi.testclient import TestClient
from src.zhihu_content import load,render,formula
from src.zhihu_store import ZhihuStore
from src.storage import Store
from src import zhihu_service,editor
from src.platforms import zhihu
from src.server import app

class FakeResponse:
    def __init__(self,json_data,status_code=200):self.data=json_data;self.status_code=status_code
    def json(self):return self.data
    def raise_for_status(self):
        if self.status_code>=400:raise RuntimeError('http failure')
class FakeTransport:
    def __init__(self,result):self.result=result;self.last=None
    def post(self,url,headers,json):self.last=(url,headers,json);return self.result

class ZhihuContentTests(unittest.TestCase):
    def setUp(self):
        self.t=tempfile.TemporaryDirectory();self.addCleanup(self.t.cleanup)
        self.folder=Path(self.t.name)/'我的文章';self.folder.mkdir()
        (self.folder/'文案.md').write_text('# 正式标题\n\n这是一篇完整文章 **加粗**。\n\n![图](01.png)',encoding='utf-8')
        Image.new('RGB',(500,300)).save(self.folder/'01.png')
    def test_heading_and_image_html(self):
        article=load(self.folder)
        self.assertEqual(article.title,'正式标题')
        self.assertFalse(article.issues)
        preview=render(article,lambda x:'/api/image/'+x)
        self.assertIn('/api/image/01.png',preview)
        self.assertNotIn('<h2>正式标题</h2>',preview)
        official=render(article,publisher=True,uploaded={'01.png':{
          'media_url':{'primary':'https://pic4.zhimg.com/real.png'},'media_meta':{'width':500,'height':300},'media_key':'media-1','extra':{}}})
        self.assertIn('<b>加粗</b>',official)
        self.assertIn('data-original-src=',official)
        self.assertIn('data-rawwidth="500"',official)
        self.assertNotIn('file://',official)
    def test_invalid_references_and_plain_html(self):
        (self.folder/'文案.md').write_text('Hello\n\n![x](../../secret.png)\n\n<script>alert(1)</script>',encoding='utf-8')
        article=load(self.folder)
        self.assertTrue(article.issues)
        with self.assertRaises(ValueError):render(article)
        (self.folder/'文案.md').write_text('Text\n\n<script>alert(1)</script>',encoding='utf-8')
        self.assertNotIn('<script>',render(load(self.folder)))
    def test_tables_and_formula(self):
        (self.folder/'文案.md').write_text('# Test\n\n|A|B|\n|-|-|\n|1|2|',encoding='utf-8')
        html=render(load(self.folder),publisher=True,uploaded={'01.png':{'media_url':{'primary':'https://pic4.zhimg.com/ex.png'},'media_meta':{'width':500,'height':300},'media_key':'k','extra':{}}})
        self.assertEqual(html.count('<tbody>'),1)
        self.assertEqual(html.count('</tbody>'),1)
        self.assertNotIn('<thead>',html)
        self.assertIn('data-draft-type="table"',html)
        self.assertIn('%20',formula(' E=mc^2 ',2))
    def test_unreferenced_images_appended(self):
        Image.new('RGB',(12,12)).save(self.folder/'02.png')
        art=load(self.folder)
        self.assertEqual(render(art,lambda x:x).count('<img'),2)

class ZhihuApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.folder=Path(self.tmp.name)
        (self.folder/'文案.md').write_text('写给自己的一篇长文章，包含很多字和一些内容。',encoding='utf-8')
        self.article=load(self.folder)
    def test_hmac_matches_independent_calculation(self):
        msg='app_key:alice|ts:123|logid:ID|extra_info:'
        expected=base64.b64encode(hmac.new(b'secret',msg.encode(),hashlib.sha256).digest()).decode()
        self.assertEqual(zhihu.signature('alice','secret','123','ID'),expected)
    def test_official_post_success_and_reject(self):
        fake=FakeTransport(FakeResponse({'status':0,'msg':'success','data':{'content_token':'1234','url':'https://zhuanlan.zhihu.com/p/1234'}}))
        with patch.object(zhihu,'credentials',return_value={'ZHIHU_OPENAPI_APP_KEY':'alice','ZHIHU_OPENAPI_APP_SECRET':'secret'}):
            result=zhihu.publish(self.article,[],True,transport=fake)
            self.assertEqual(result,('1234','https://zhuanlan.zhihu.com/p/1234'))
            self.assertEqual(fake.last[2]['content']['html'].count('<p>'),1)
            self.assertIn('X-Sign',fake.last[1])
            fake.result=FakeResponse({'status':429,'msg':'rate limit','data':None},429)
            with self.assertRaises(zhihu.KnownReject):zhihu.publish(self.article,[],True,transport=fake)
            fake.result=FakeResponse({'status':0,'data':{}},200)
            with self.assertRaises(zhihu.ZhihuError):zhihu.publish(self.article,[],True,transport=fake)
            fake.result=FakeResponse({'msg':'success'},200)
            with self.assertRaises(zhihu.ZhihuError):zhihu.publish(self.article,[],True,transport=fake)
    def test_creator_stats_exact_fields(self):
        items,paging=zhihu._extract_items({'Data':{'Items':[{'ContentType':'article','Url':'https://zhuanlan.zhihu.com/p/222','LikeCount':3}],'Paging':{'IsEnd':True}}})
        self.assertEqual(len(items),1)
        self.assertTrue(paging['IsEnd'])
        with self.assertRaises(zhihu.ZhihuError):zhihu._extract_items({'Data':{'Wrong':[]}})

class ZhihuLocalTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.base=Path(self.tmp.name)
        self.zhroot=self.base/'zhihu';self.zhroot.mkdir()
        self.xhsroot=self.base/'xhs';self.xhsroot.mkdir()
        self.artdir=self.zhroot/'First';self.artdir.mkdir()
        (self.artdir/'文案.md').write_text('# 文章标题\n\n我们来详细谈谈这篇文章的内容。',encoding='utf-8')
        Image.new('RGB',(9,9)).save(self.artdir/'01.jpg')
        self.store=Store(self.base/'state.db')
        self.zs=ZhihuStore(self.store)
        def patcher(target,name,value):
            p=patch.object(target,name,value);p.start();self.addCleanup(p.stop)
        patcher(zhihu_service,'ZHIHU',self.zhroot)
        patcher(zhihu_service,'zs',self.zs)
        patcher(editor,'ROOTS',{'xhs':self.xhsroot,'zhihu':self.zhroot})
        patcher(editor,'RUNTIME',self.base/'runtime')
        zhihu_service.sources()
        self.uid=self.zs.records()[0]['uid']
    def test_frontend_save_and_convert(self):
        client=TestClient(app,base_url='http://127.0.0.1')
        listed=client.get('/api/notes?platform=zhihu').json()['notes']
        self.assertEqual(listed[0]['title'],'文章标题')
        self.assertEqual(listed[0]['status'],'draft')
        item=client.get('/api/notes/'+self.uid).json()['note']
        self.assertIn('文章',item['html'])
        result=client.put('/api/notes/'+self.uid+'/body',json={'text':'# 改标题\n\n这是一篇更新过内容的文章。\n\n![图](01.jpg)','expected_hash':item['fingerprint']})
        self.assertEqual(result.status_code,200)
        conflict=client.put('/api/notes/'+self.uid+'/body',json={'text':'abc','expected_hash':item['fingerprint']})
        self.assertEqual(conflict.status_code,409)
        conv=client.post('/api/notes/'+self.uid+'/convert',json={'filename':'01.jpg'})
        self.assertEqual(conv.status_code,200,conv.text)
        self.assertTrue((self.artdir/'01.png').exists())
        self.assertFalse((self.artdir/'01.jpg').exists())
        self.assertIn('![图](01.png)',(self.artdir/'文案.md').read_text())
        self.assertTrue(list((self.base/'runtime'/'originals').rglob('*01.jpg')))
    def test_mocked_pub_pending_and_success(self):
        # Preflight without auth must not mark any submission.
        with patch.object(zhihu,'credential_status',return_value={'publish_ready':False}):
            with self.assertRaises(ValueError):zhihu_service.publish_one(self.uid)
        self.assertEqual(self.zs.get(self.uid)['status'],'draft')
        with patch.object(zhihu,'credential_status',return_value={'publish_ready':True}),\
             patch.object(zhihu,'publish',return_value=('999','https://zhuanlan.zhihu.com/p/999')):
            # Avoid importing optional media uploader during test by removing image file.
            (self.artdir/'01.jpg').unlink()
            out=zhihu_service.publish_one(self.uid)
        self.assertEqual(out['status'],'published')
        self.assertEqual(self.zs.get(self.uid)['zhihu_id'],'999')
        with self.assertRaises(ValueError):zhihu_service.publish_one(self.uid)
    def test_status_and_history(self):
        self.zs.edit(self.uid,status='published',zhihu_id='111',zhihu_url='https://zhuanlan.zhihu.com/p/111')
        self.zs.snapshot(self.uid,{'views':None,'likes':0,'comments':0,'favorites':3},'official_user_contents','2026-01-01T00:00:00Z')
        self.zs.snapshot(self.uid,{'views':None,'likes':9,'comments':1,'favorites':4},'official_user_contents','2026-01-02T00:00:00Z')
        t=self.zs.trend(self.uid)
        self.assertIsNone(t[0]['views']);self.assertEqual([x['likes'] for x in t],[0,9])
        self.assertEqual(zhihu_service.detail(self.uid)['stats']['likes'],9)
    def test_cross_site_mutation_blocked(self):
        client=TestClient(app,base_url='http://127.0.0.1')
        res=client.post('/api/notes',json={'title':'evil','platform':'zhihu'},headers={'Origin':'https://bad.example'})
        self.assertEqual(res.status_code,403)

if __name__=='__main__':unittest.main()
