"""Both platforms must expose one consistent HTTP note contract after refactors."""
from __future__ import annotations
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from fastapi.testclient import TestClient
from PIL import Image
from src import service, zhihu_service, workspace, editor
from src.storage import Store
from src.zhihu_store import ZhihuStore
from src.server import app


class UnifiedInterfaceTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        xhs, zh = root/'xhs', root/'zhihu'
        for p in (xhs, zh):p.mkdir()
        one = xhs/'旅途';one.mkdir()
        (one/'文案.txt').write_text('山路的风景。\n\n#旅行', encoding='utf-8')
        Image.new('RGB', (10, 12)).save(one/'01.png')
        two = zh/'旅途';two.mkdir()
        (two/'文案.md').write_text('# 随笔\n\n一篇简单的旅行记录。\n\n![图](01.png)', encoding='utf-8')
        Image.new('RGB', (10, 12)).save(two/'01.png')
        state = Store(root/'state.sqlite')
        zhstore = ZhihuStore(state)
        for owner, attr, value in (
            (service, 'CONTENT', xhs), (service, 'store', state),
            (zhihu_service, 'ZHIHU', zh), (zhihu_service, 'zs', zhstore),
            (editor, 'ROOTS', {'xhs':xhs,'zhihu':zh}), (editor,'RUNTIME',root/'runtime'),
        ):
            p=patch.object(owner,attr,value);p.start();self.addCleanup(p.stop)
        self.client=TestClient(app,base_url='http://127.0.0.1')

    def test_common_catalog_and_editor(self):
        a=self.client.get('/api/notes?platform=xhs').json()['notes'][0]
        b=self.client.get('/api/notes?platform=zhihu').json()['notes'][0]
        self.assertEqual(set(a),set(b))
        self.assertEqual(a['title'],'旅途')
        self.assertEqual(b['title'],'随笔')
        self.assertEqual(workspace.resolve(a['uid']).key,'xhs')
        self.assertEqual(workspace.resolve(b['uid']).key,'zhihu')
        for summary in (a,b):
            uid=summary['uid']
            detail=self.client.get(f'/api/notes/{uid}')
            self.assertEqual(detail.status_code,200)
            note=detail.json()['note']
            self.assertEqual(note['platform'],summary['platform'])
            self.assertEqual(self.client.get(f'/api/notes/{uid}/trend').status_code,200)
            self.assertEqual(self.client.get(f'/api/notes/{uid}/images/0').status_code,200)
        self.assertIn('<img',self.client.get(f"/api/notes/{b['uid']}").json()['note']['html'])

    def test_unknown_and_collision(self):
        self.assertEqual(self.client.get('/api/notes/unknown').status_code,404)
        with patch.object(service.store,'note',return_value={'uid':'same'}),patch.object(zhihu_service.zs,'get',return_value={'uid':'same'}):
            with self.assertRaisesRegex(ValueError,'冲突'):
                workspace.resolve('same')

    def test_refresh_is_one_entry_and_no_duplicate_scan_button(self):
        html=self.client.get('/').text
        self.assertNotIn('id="scan-btn"',html)
        self.assertEqual(html.count('id="refresh-btn"'),1)
        self.assertNotIn('id="refresh-scope"',html)
        self.assertIn('id="help-btn"',html)
        self.assertIn('class="banner-area"',html)
        self.assertEqual(self.client.post('/api/refresh',json={'platform':'none'}).status_code,400)
        self.assertEqual(self.client.post('/api/sync',json={'platform':'none'}).status_code,400)

if __name__=='__main__': unittest.main()
