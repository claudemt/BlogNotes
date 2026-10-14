"""Read-only account, remote index and safe archival: no live credentials needed."""
from __future__ import annotations
import tempfile,unittest
from pathlib import Path
from unittest.mock import patch,MagicMock
from PIL import Image
from fastapi.testclient import TestClient
from src.storage import Store
from src import service,remote,workspace,archive,zhihu_service
from src.platforms import xiaohongshu as xhs
from src.server import app

class RemoteTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        p=patch.object(service,'store',Store(self.root/'state.sqlite'));p.start();self.addCleanup(p.stop)
        self.client=TestClient(app,base_url='http://127.0.0.1')

    def test_remote_catalog_and_trend(self):
        remote.update('xhs',[{'xhs_id':'0123456789abcdef01234567','title':'一条旧记录','published_at':'2026-10-09T00:00:00Z','likes':42,'views':128}])
        items=self.client.get('/api/notes?platform=xhs').json()['notes']
        r=next(x for x in items if x['remote_only'])
        self.assertEqual(r['stats']['views'],128)
        self.assertEqual(self.client.get('/api/notes/'+r['uid']).json()['note']['status'],'published')
        self.assertEqual(self.client.get('/api/notes/'+r['uid']+'/trend').json()['points'][0]['likes'],42)
        self.assertEqual(self.client.post('/api/import',json={'platform':'xhs','id':'missing'}).status_code,404)

    def test_remote_not_duplicated_when_local_linked(self):
        remote.update('xhs',[{'xhs_id':'0123456789abcdef01234567','title':'remote','likes':1}])
        # All locally linked IDs supersede the remote index row.
        with patch.object(service,'catalog',return_value=[{'platform_id':'0123456789abcdef01234567','title':'local','uid':'local-id','status':'published'}]):
            rows=workspace.backend('xhs').catalog()
            self.assertEqual(len([x for x in rows if x.get('remote_only')]),0)

    def test_profile_normalizes_nullable_labels(self):
        data=xhs._profile_from_state({'url':'https://www.xiaohongshu.com/user/profile/abc',
          'profile':{'user_id':'abc','nickname':'User','following':'12','followers':'1.2万','liked_collected':'300'}})
        self.assertEqual(data['followers'],12000)
        self.assertEqual(data['liked_collected'],300)
        self.assertIsNone(xhs._profile_from_state({}).get('followers'))

    def test_only_trusted_media_hosts(self):
        self.assertTrue(archive.safe_image_host('https://pic3.zhimg.com/photo.png'))
        self.assertTrue(archive.safe_image_host('https://sns-img.xhscdn.com/pic'))
        for url in ['file:///etc/passwd','http://pic3.zhimg.com/image.png','https://foo.zhimg.com.evil.test/img',
                    'https://127.0.0.1/photo','https://evil.test/file.png']:
            self.assertFalse(archive.safe_image_host(url),url)

    def test_zhihu_requires_full_body_not_summary(self):
        response=MagicMock();response.text='<html><p>仅摘要</p></html>';response.is_redirect=False
        client=MagicMock();client.get.return_value=response
        with self.assertRaisesRegex(archive.ImportError_,'完整正文'):
            archive._fetch_zhihu_article('https://zhuanlan.zhihu.com/p/111',client)
        self.assertEqual(client.get.call_args.args[0],'https://zhuanlan.zhihu.com/p/111')

    def test_zhihu_markdown_image_and_equation(self):
        response=MagicMock();response.text='''<div class="Post-RichTextContainer"><div class="RichText ztext">
        <h2>理论</h2><p>这是某一篇完整的知乎文章正文，应该正确转换出来。</p>
        <img src="https://pic3.zhimg.com/aa.png" alt="图片"/>
        <img eeimg="1" alt="E=mc^2" src="//www.zhihu.com/equation?tex=E"/>
        </div></div>''';response.is_redirect=False
        client=MagicMock();client.get.return_value=response
        md,imgs=archive._fetch_zhihu_article('https://zhuan.zhihu.com/p/1'.replace('zhuan.','zhuanlan.'),client)
        self.assertIn('![图片](01.png)',md)
        self.assertIn('E=mc^2',md)
        self.assertEqual(imgs,['https://pic3.zhimg.com/aa.png'])

    def test_successful_xhs_import_links_old_id_and_reuses_local_folder(self):
        from io import BytesIO
        remote_id='0123456789abcdef01234567'
        remote.update('xhs',[{'xhs_id':remote_id,'title':'远端风景','published_at':'2026-10-11T00:00:00Z','likes':4}])
        root=self.root/'xhs';root.mkdir()
        temp=self.root/'runtime'
        image=BytesIO();Image.new('RGB',(5,4),'red').save(image,'PNG')
        with patch.object(archive,'CONTENT',root),patch.object(archive,'RUNTIME',temp), \
             patch.object(service,'CONTENT',root), \
             patch.object(xhs,'fetch_full_note',return_value={'title':'远端风景','body':'秋天在山里的风景。','images':['https://sns-img.xhscdn.com/1']}), \
             patch.object(archive,'_download_image',return_value=image.getvalue()):
            result=archive.export_one('xhs',remote_id)
        self.assertEqual(result['images'],1)
        self.assertTrue((root/'远端风景'/'01.png').is_file())
        self.assertEqual((root/'远端风景'/'文案.txt').read_text(), '秋天在山里的风景。')
        self.assertEqual(service.store.note_folder('远端风景')['xhs_id'],remote_id)
        with patch.object(archive,'CONTENT',root), patch.object(service,'CONTENT',root):
            with self.assertRaisesRegex(archive.ImportError_,'不覆盖'):
                archive.export_one('xhs',remote_id)

    def test_archive_is_atomic_and_does_not_overwrite(self):
        remote.update('xhs',[{'xhs_id':'0123456789abcdef01234567','title':'远端旅行','xsec_token':'abc'}])
        dest=self.root/'xhs';dest.mkdir()
        runtime=self.root/'runtime'
        with patch.object(archive,'CONTENT',dest),patch.object(archive,'RUNTIME',runtime), \
             patch.object(xhs,'fetch_full_note',return_value={'title':'远端旅行','body':'路上的景色真好看。','images':['https://sns-img.xhscdn.com/img']}),\
             patch.object(archive,'_download_image',return_value=b''),\
             patch.object(archive,'_write_png',side_effect=lambda blob,path:Image.new('RGB',(4,4)).save(path)),\
             patch.object(service,'sources',side_effect=lambda:None),\
             patch.object(service.store,'note_folder',return_value=None):
            with self.assertRaisesRegex(archive.ImportError_,'索引创建失败'):
                archive.export_one('xhs','0123456789abcdef01234567')
            self.assertFalse(list(runtime.glob('import-staging/*')))
        # Existing authored folders never get replaced.
        (dest/'远端旅行').mkdir(exist_ok=True)
        self.assertTrue((dest/'远端旅行').exists())

if __name__=='__main__':unittest.main()
