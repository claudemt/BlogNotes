"""Offline regression suite; does not access Xiaohongshu or require login."""
from __future__ import annotations
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from PIL import Image
from fastapi.testclient import TestClient

from src.content import load_note,scan
from src.media import prepare_pngs
from src.storage import Store
from src import service
from src.server import app

class ContentTests(unittest.TestCase):
    def test_txt_body_tags_numeric_sort_and_missing(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/'旅行笔记';p.mkdir()
            (p/'文案.txt').write_text('原始正文\n\n#旅行 #风景',encoding='utf-8')
            Image.new('RGB',(4,4)).save(p/'02.jpg')
            Image.new('RGB',(4,4)).save(p/'01.png')
            n=load_note(p)
            self.assertEqual(n.title,'旅行笔记')
            self.assertEqual(n.body,'原始正文\n\n#旅行 #风景')
            self.assertEqual(n.tags,['旅行','风景'])
            self.assertEqual([f.name for f in n.images],['01.png','02.jpg'])
            self.assertFalse(n.issues)
            (p/'03.jpeg').write_text('bad',encoding='utf-8')
            n=load_note(p)
            self.assertEqual(len(n.images),3)

    def test_invalid_number_and_duplicate(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/'note';p.mkdir();(p/'文案.txt').write_text('hi')
            (p/'01.png').write_bytes(b'bad');(p/'01.jpg').write_bytes(b'bad');(p/'03.png').write_bytes(b'bad')
            n=load_note(p)
            self.assertTrue(any('重复' in x for x in n.issues))
            self.assertTrue(any('跳号' in x for x in n.issues))

    def test_convert_without_mutating(self):
        with tempfile.TemporaryDirectory() as td:
            td=Path(td)
            jpg=td/'01.jpg';png=td/'02.png'
            Image.new('RGB',(16,10),(30,50,90)).save(jpg)
            Image.new('RGB',(6,8)).save(png)
            before=jpg.read_bytes()
            imgs=prepare_pngs((jpg,png),td/'run')
            self.assertEqual(imgs[1],png.resolve())
            self.assertEqual(imgs[0].suffix,'.png')
            self.assertEqual(jpg.read_bytes(),before)
            with Image.open(imgs[0]) as im:self.assertEqual(im.size,(16,10))
            self.assertEqual(prepare_pngs((jpg,),td/'run')[0],imgs[0])

class StoreTests(unittest.TestCase):
    def test_state_snapshots(self):
        with tempfile.TemporaryDirectory() as td:
            db=Store(Path(td)/'db.sqlite')
            class S:slug='test';title='test';modified_at=100
            db.touch_source(S)
            note=db.note_folder('test')
            self.assertEqual(note['status'],'draft')
            db.modify(note['uid'],status='pending')
            self.assertEqual(db.note(note['uid'])['status'],'pending')
            self.assertTrue(db.snapshot(note['uid'],{'views':0},'2026-01-01T01:00:00+00:00'))
            self.assertFalse(db.snapshot(note['uid'],{'views':50},'2026-01-01T01:00:00+00:00'))
            db.snapshot(note['uid'],{'views':25,'likes':2},'2026-01-02T01:00:00+00:00')
            ts=db.timeline(note['uid'])
            self.assertEqual([p['views'] for p in ts],[0,25])
            self.assertIsNone(ts[0]['likes'])

    def test_new_metric_columns_are_added_to_an_older_database(self):
        # A database created before view_time_avg/danmaku existed must gain the
        # columns on open, without losing the rows already in it.
        import sqlite3
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/'db.sqlite'
            legacy=sqlite3.connect(path)
            legacy.executescript("""
                CREATE TABLE snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    note_uid TEXT NOT NULL, captured_at TEXT NOT NULL,
                    impressions INTEGER, views INTEGER, likes INTEGER,
                    favorites INTEGER, comments INTEGER, shares INTEGER,
                    followers INTEGER, click_rate REAL,
                    raw_json TEXT NOT NULL DEFAULT '{}',
                    UNIQUE(note_uid, captured_at));
                INSERT INTO snapshots(note_uid,captured_at,views)
                    VALUES('u1','2026-01-01T00:00:00+00:00',7);
            """)
            legacy.commit(); legacy.close()

            db=Store(path)
            with db.db() as conn:
                columns={r[1] for r in conn.execute("PRAGMA table_info(snapshots)")}
                self.assertIn('view_time_avg',columns)
                self.assertIn('danmaku',columns)
                kept=conn.execute('SELECT views FROM snapshots').fetchall()
            self.assertEqual([r[0] for r in kept],[7])

            db.snapshot('u1',{'views':9,'view_time_avg':31,'danmaku':4},'2026-01-02T00:00:00+00:00')
            latest=db.timeline('u1')[-1]
            self.assertEqual(latest['view_time_avg'],31)
            self.assertEqual(latest['danmaku'],4)

class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        content=self.root/'content';content.mkdir()
        p=content/'一天的路程';p.mkdir();(p/'文案.txt').write_text('照片记录\n\n#旅行',encoding='utf-8')
        Image.new('RGB',(7,7)).save(p/'01.png')
        self.temp_db=Store(self.root/'data.sqlite')
        self.mock_content=patch.object(service,'CONTENT',content)
        self.mock_store=patch.object(service,'store',self.temp_db)
        self.mock_content.start();self.mock_store.start()
        self.mock_quota=patch.object(service.queue,'reserve',return_value=None)
        self.mock_quota.start();self.addCleanup(self.mock_quota.stop)
        self.addCleanup(self.mock_content.stop);self.addCleanup(self.mock_store.stop)
        service.sources();self.entry=self.temp_db.note_folder('一天的路程')

    def test_http_listing_and_preview(self):
        client=TestClient(app,base_url="http://127.0.0.1")
        r=client.get('/api/notes');self.assertEqual(r.status_code,200)
        note=r.json()['notes'][0]
        self.assertEqual(note['title'],'一天的路程')
        self.assertEqual(note['status'],'draft')
        preview=client.get('/api/notes/'+note['uid']).json()['note']
        self.assertIn('#旅行',preview['body'])
        self.assertEqual(client.get('/api/notes/'+note['uid']+'/images/0').status_code,200)
        self.assertEqual(client.get('/api/notes/'+note['uid']+'/images/99').status_code,404)
        self.assertEqual(client.get('/api/notes/not-real').status_code,404)
        self.assertEqual(client.post('/api/open-content',headers={'Origin':'https://evil.example'}).status_code,403)
        self.assertEqual(TestClient(app).get('/api/notes').status_code,403)

    def test_reconciliation_requires_exact_match(self):
        u=self.entry['uid']
        self.temp_db.modify(u,status='pending',submitted_at='2026-10-11T09:00:00+00:00',published_hash='hash')
        rows=[{'xhs_id':'aabbccddeeff001122334455','title':'一天的路程','published_at':'2026-10-11T09:01:00+00:00','views':10,'likes':1}]
        links,count=service._reconcile(rows)
        self.assertEqual((links,count),(1,1))
        self.assertEqual(self.temp_db.note(u)['status'],'published')
        self.assertEqual(self.temp_db.latest(u)['views'],10)
        service._reconcile([{'xhs_id':'zz','title':'一天的路程','published_at':None,'views':20}])
        self.assertEqual(self.temp_db.note(u)['xhs_id'],'aabbccddeeff001122334455')

    def test_mocked_publish_success_and_unknown_result(self):
        uid=self.entry['uid']
        note_id='0123456789abcdef01234567'
        def fake_notes(report):
            return [{'xhs_id':note_id,'title':'一天的路程',
                     'published_at':'2026-10-11T09:02:00+00:00',
                     'views':18,'likes':2,'favorites':1}]
        # First force a deterministic send time within matching publication window.
        with patch.object(service,'utcnow',return_value='2026-10-11T09:00:00+00:00'):
            with patch.object(service.xhs,'publish',side_effect=lambda *a, **kw: (kw['before_submit'](),note_id)[1]), \
                 patch.object(service.xhs,'list_statistics',side_effect=fake_notes):
                result=service.publish_one(uid)
        self.assertEqual(result['status'],'published')
        self.assertEqual(self.temp_db.note(uid)['xhs_id'],note_id)
        self.assertEqual(self.temp_db.latest(uid)['views'],18)
        with self.assertRaises(ValueError):service.publish_one(uid)

    def test_ambiguous_pending_not_auto_matched(self):
        uid=self.entry['uid']
        self.temp_db.modify(uid,status='pending',submitted_at='2026-10-11T09:00:00+00:00')
        # Missing publication timestamp: title alone must not be trusted.
        linked,_=service._reconcile([{'xhs_id':'98765','title':'一天的路程',
                                     'published_at':None,'views':1}])
        self.assertEqual(linked,0)
        self.assertEqual(self.temp_db.note(uid)['status'],'pending')

    def test_prepare_failure_does_not_create_pending(self):
        uid=self.entry['uid']
        with patch.object(service.xhs,'publish',side_effect=RuntimeError('upload failed')):
            with self.assertRaisesRegex(RuntimeError,'upload failed'):service.publish_one(uid)
        self.assertEqual(self.temp_db.note(uid)['status'],'draft')
        self.assertIn('尚未点击发布',self.temp_db.note(uid)['last_error'])

    def test_publish_exception_is_pending(self):
        uid=self.entry['uid']
        with patch.object(service.xhs,'publish',side_effect=lambda *a, **kw: (kw['before_submit'](), (_ for _ in ()).throw(RuntimeError('browser failed')))):
            with self.assertRaises(RuntimeError):service.publish_one(uid)
        self.assertEqual(self.temp_db.note(uid)['status'],'pending')
        with self.assertRaises(ValueError):service.publish_one(uid)
        service.release_pending(uid)
        self.assertEqual(self.temp_db.note(uid)['status'],'draft')

if __name__=='__main__':unittest.main()
