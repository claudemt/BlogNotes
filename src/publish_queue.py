"""Persistent, non-evasive, opt-in publication queue and shared quota ledger.

There is no safe guaranteed posting rate. This module merely imposes upper bounds,
serializes submissions, and pauses on all ambiguous/denied outcomes. A daemon tick
never changes an uncertain note back to draft.
"""
from __future__ import annotations
import sqlite3
import threading
from contextlib import contextmanager
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from .settings import RUNTIME, settings

UTC = timezone.utc

def now(): return datetime.now(UTC)
def iso(dt): return dt.isoformat(timespec='seconds')

class RateLimited(ValueError):
    pass

class PublishQueue:
    def __init__(self, file: Path | None = None):
        self.file = file or (RUNTIME / 'publish_queue.sqlite')
        self.file.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        with self._db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS submissions (
                  id INTEGER PRIMARY KEY, platform TEXT NOT NULL, note_uid TEXT NOT NULL,
                  attempted_at TEXT NOT NULL, origin TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS idx_submission_window ON submissions(platform, attempted_at);
                CREATE TABLE IF NOT EXISTS batches (
                  id TEXT PRIMARY KEY, platform TEXT NOT NULL, status TEXT NOT NULL,
                  created_at TEXT NOT NULL, updated_at TEXT NOT NULL, last_error TEXT);
                CREATE TABLE IF NOT EXISTS batch_items (
                  batch_id TEXT NOT NULL REFERENCES batches(id), pos INTEGER NOT NULL,
                  note_uid TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'queued',
                  message TEXT, started_at TEXT, finished_at TEXT,
                  PRIMARY KEY(batch_id, note_uid));
                CREATE INDEX IF NOT EXISTS idx_batch_items_state ON batch_items(batch_id, state, pos);
            ''')
            # Crash recovery: a browser may have submitted before interruption.
            for row in db.execute("SELECT DISTINCT batch_id FROM batch_items WHERE state='processing'").fetchall():
                db.execute("UPDATE batch_items SET state='attention',message='进程中断，请先核对平台是否已发布' WHERE batch_id=? AND state='processing'", (row['batch_id'],))
                db.execute("UPDATE batches SET status='paused',last_error='进程中断，需要人工核对' WHERE id=?", (row['batch_id'],))

    @contextmanager
    def _db(self):
        with self.lock:
            conn = sqlite3.connect(self.file, timeout=30)
            conn.row_factory = sqlite3.Row
            conn.execute('PRAGMA busy_timeout=30000')
            conn.execute('PRAGMA foreign_keys=ON')
            try:
                yield conn
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
            finally:
                conn.close()

    def limits(self):
        cfg=settings()
        limit=max(1,min(30,int(cfg.get('daily_publish_limit',6))))
        gap=max(10,int(cfg.get('publish_min_interval_minutes',60)))
        return limit,gap

    def next_allowed(self,platform:str):
        """Timestamp of next locally permissible attempt (not a platform approval)."""
        limit,gap=self.limits()
        cutoff=iso(now()-timedelta(hours=24))
        with self._db() as db:
            recent=[datetime.fromisoformat(x['attempted_at']) for x in db.execute(
                'SELECT attempted_at FROM submissions WHERE platform=? AND attempted_at>? ORDER BY attempted_at', (platform,cutoff))]
        candidates=[now()]
        if recent:
            candidates.append(recent[-1]+timedelta(minutes=gap))
        if len(recent)>=limit:
            candidates.append(recent[-limit]+timedelta(hours=24,seconds=1))
        return max(candidates),len(recent),limit,gap

    def reserve(self,platform:str,note_uid:str,origin='manual'):
        """Reserve at most one attempt. Must be called before irreversible submit."""
        if platform not in ('xhs','zhihu'):raise ValueError('未知平台')
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            limit,gap=self.limits()
            t=now()
            recent=[datetime.fromisoformat(x[0]) for x in db.execute(
                'SELECT attempted_at FROM submissions WHERE platform=? AND attempted_at>? ORDER BY attempted_at',
                (platform,iso(t-timedelta(hours=24)))).fetchall()]
            if len(recent)>=limit:
                raise RateLimited(f'24小时内已尝试 {len(recent)} 篇，达到本地上限 {limit}；稍后再试')
            if recent and t < recent[-1]+timedelta(minutes=gap):
                until=recent[-1]+timedelta(minutes=gap)
                raise RateLimited('发布间隔未到：'+until.astimezone().strftime('%m-%d %H:%M'))
            db.execute('INSERT INTO submissions(platform,note_uid,attempted_at,origin) VALUES(?,?,?,?)',
                       (platform,note_uid,iso(t),origin))

    def create(self,platform:str,uids:list[str]):
        if platform not in ('xhs','zhihu'):raise ValueError('未知平台')
        uids=list(dict.fromkeys(uids))
        if not uids or len(uids)>30:raise ValueError('单次队列须包含 1—30 篇笔记')
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute("SELECT 1 FROM batches WHERE platform=? AND status IN ('running','paused') LIMIT 1",(platform,)).fetchone():
                raise ValueError('该平台已有未完成批次，请先处理当前批次')
            ident=uuid.uuid4().hex[:12]
            db.execute('INSERT INTO batches(id,platform,status,created_at,updated_at) VALUES(?,?,?,?,?)',
                       (ident,platform,'running',iso(now()),iso(now())))
            db.executemany('INSERT INTO batch_items(batch_id,pos,note_uid,state) VALUES(?,?,?,?)',
                           [(ident,i,uid,'queued') for i,uid in enumerate(uids)])
        return ident

    def list(self,platform:str | None=None):
        with self._db() as db:
            q='SELECT * FROM batches' + (' WHERE platform=?' if platform else '') + ' ORDER BY created_at DESC LIMIT 20'
            rows=[dict(x) for x in db.execute(q,(platform,) if platform else ())]
            for row in rows:
                row['items']=[dict(x) for x in db.execute('SELECT note_uid,pos,state,message FROM batch_items WHERE batch_id=? ORDER BY pos',(row['id'],))]
        return rows

    def set_state(self,id:str,action:str):
        if action not in ('pause','resume','stop'):raise ValueError('未知操作')
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            r=db.execute('SELECT status FROM batches WHERE id=?',(id,)).fetchone()
            if not r:raise KeyError('批次不存在')
            if r['status'] in ('completed','stopped'):raise ValueError('批次已结束')
            desired={'pause':'paused','resume':'running','stop':'stopped'}[action]
            if action=='resume' and db.execute("SELECT 1 FROM batch_items WHERE batch_id=? AND state IN ('attention','processing')",(id,)).fetchone():
                raise ValueError('请先处理待核对项目；不允许直接继续')
            db.execute('UPDATE batches SET status=?,updated_at=?,last_error=NULL WHERE id=?',(desired,iso(now()),id))
        return desired

    def skip_attention(self,id:str,uid:str):
        """Skip an item only after operator has resolved the uncertain status separately."""
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            b=db.execute('SELECT status FROM batches WHERE id=?',(id,)).fetchone()
            if not b or b['status']!='paused':raise ValueError('请先暂停批次')
            r=db.execute('SELECT state FROM batch_items WHERE batch_id=? AND note_uid=?',(id,uid)).fetchone()
            if not r or r['state']!='attention':raise ValueError('该笔记无需人工处理')
            db.execute("UPDATE batch_items SET state='skipped',message='用户跳过',finished_at=? WHERE batch_id=? AND note_uid=?",(iso(now()),id,uid))

    def ready_batch(self):
        with self._db() as db:
            return [dict(r) for r in db.execute("SELECT * FROM batches WHERE status='running' ORDER BY created_at")]

    def claim(self,id:str):
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT status FROM batches WHERE id=?',(id,)).fetchone()
            if not row or row['status']!='running':return None
            item=db.execute("SELECT note_uid FROM batch_items WHERE batch_id=? AND state='queued' ORDER BY pos LIMIT 1",(id,)).fetchone()
            if not item:
                if not db.execute("SELECT 1 FROM batch_items WHERE batch_id=? AND state IN ('processing','attention')",(id,)).fetchone():
                    db.execute("UPDATE batches SET status='completed',updated_at=? WHERE id=?",(iso(now()),id))
                return None
            db.execute("UPDATE batch_items SET state='processing',started_at=? WHERE batch_id=? AND note_uid=?",(iso(now()),id,item['note_uid']))
            return item['note_uid']

    def finish(self,id:str,uid:str,status:str,message:str=''):
        if status not in ('done','skipped','attention'):raise ValueError('未知结果')
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('UPDATE batch_items SET state=?,message=?,finished_at=? WHERE batch_id=? AND note_uid=?',
                       (status,message[:350],iso(now()),id,uid))
            if status=='attention':
                db.execute("UPDATE batches SET status='paused',last_error=?,updated_at=? WHERE id=?",(message[:350],iso(now()),id))
            elif not db.execute("SELECT 1 FROM batch_items WHERE batch_id=? AND state IN ('queued','processing','attention')",(id,)).fetchone():
                db.execute("UPDATE batches SET status='completed',updated_at=? WHERE id=? AND status='running'",(iso(now()),id))

queue=PublishQueue()
