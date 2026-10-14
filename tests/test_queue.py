"""Offline queue and quota regression tests; never touches a real account."""
import tempfile
import unittest
import threading
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch
from fastapi.testclient import TestClient
from src.publish_queue import PublishQueue, RateLimited, now, iso
from src.server import app
from src import service, batch_service

class QueueTests(unittest.TestCase):
    def make(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        return PublishQueue(Path(temp.name)/'queue.sqlite')
    def test_quota_and_minimum_gap(self):
        q=self.make()
        with patch('src.publish_queue.settings',return_value={'daily_publish_limit':30,'publish_min_interval_minutes':48}):
            q.reserve('xhs','note-1')
            with self.assertRaises(RateLimited):q.reserve('xhs','note-2')
            next_time,used,limit,gap=q.next_allowed('xhs')
            self.assertEqual((used,limit,gap),(1,30,48))
            self.assertGreater(next_time,now()+timedelta(minutes=47))
            with q._db() as db:
                db.execute('UPDATE submissions SET attempted_at=?',(iso(now()-timedelta(hours=2)),))
            q.reserve('xhs','note-2')
    def test_day_quota_clamped_to_30_and_platform_isolated(self):
        q=self.make()
        with patch('src.publish_queue.settings',return_value={'daily_publish_limit':90,'publish_min_interval_minutes':10}):
            with q._db() as db:
                db.executemany('INSERT INTO submissions(platform,note_uid,attempted_at,origin) VALUES(?,?,?,?)',
                               [('xhs',str(i),iso(now()-timedelta(hours=23,minutes=30-i)), 'manual') for i in range(30)])
            with self.assertRaisesRegex(RateLimited,'本地上限'):q.reserve('xhs','new')
            q.reserve('zhihu','zhihu-first')
            self.assertEqual(q.next_allowed('xhs')[2],30)
    def test_concurrent_reservations_have_one_winner(self):
        q=self.make()
        results=[]
        guard=threading.Lock()
        def run(i):
            try:q.reserve('xhs',f'uid-{i}');status='ok'
            except RateLimited:status='limited'
            with guard:results.append(status)
        threads=[threading.Thread(target=run,args=(i,)) for i in range(12)]
        for t in threads:t.start()
        for t in threads:t.join()
        self.assertEqual(results.count('ok'),1)
        self.assertEqual(results.count('limited'),11)

    def test_queue_progress_pause_resume_and_crash_recovery(self):
        q=self.make()
        bid=q.create('xhs',['a','b'])
        self.assertEqual(q.list('xhs')[0]['items'][0]['state'],'queued')
        self.assertEqual(q.claim(bid),'a')
        self.assertEqual(q.list('xhs')[0]['items'][0]['state'],'processing')
        q2=PublishQueue(q.file)  # recovery of an in-flight upload
        self.assertEqual(q2.list('xhs')[0]['status'],'paused')
        self.assertEqual(q2.list('xhs')[0]['items'][0]['state'],'attention')
        with self.assertRaisesRegex(ValueError,'核对'):q2.set_state(bid,'resume')
        q2.skip_attention(bid,'a')
        self.assertEqual(q2.set_state(bid,'resume'),'running')
        self.assertEqual(q2.claim(bid),'b')
        q2.finish(bid,'b','done')
        self.assertEqual(q2.list('xhs')[0]['status'],'completed')
    def test_stop_during_in_flight_does_not_reactivate_batch(self):
        q=self.make()
        bid=q.create('zhihu',['a'])
        self.assertEqual(q.claim(bid),'a')
        q.set_state(bid,'stop')
        q.finish(bid,'a','done','发布完成')
        self.assertEqual(q.list('zhihu')[0]['status'],'stopped')

    def test_duplicated_batch_rejected(self):
        q=self.make()
        q.create('xhs',['a'])
        with self.assertRaisesRegex(ValueError,'已有'):q.create('xhs',['b'])
        with self.assertRaises(ValueError):q.create('zhihu',list(map(str,range(31))))
    def test_http_queue_endpoints(self):
        client=TestClient(app,base_url='http://127.0.0.1')
        data=client.get('/api/publish-policy?platform=xhs').json()
        self.assertTrue(data['ok'])
        self.assertIn('gap_minutes',data['quota'])
        self.assertEqual(client.get('/api/batches?platform=invalid').status_code,400)
        self.assertEqual(client.post('/api/batches',json={'platform':'xhs','uids':[]}).status_code,409)
        self.assertEqual(client.post('/api/refresh',json={'platform':'invalid'}).status_code,400)
        self.assertEqual(client.post('/api/refresh',json={'platform':'xhs'},headers={'Origin':'https://evil.example'}).status_code,403)

if __name__=='__main__':unittest.main()
