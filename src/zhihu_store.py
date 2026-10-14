"""Separate tables avoid mutating existing XHS publication and snapshot records."""
from __future__ import annotations
import uuid,json
from .storage import utcnow,Store

class ZhihuStore:
    def __init__(self,store:Store):
        self.store=store
        with store.db() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS zhihu_notes (
                uid TEXT PRIMARY KEY, folder TEXT NOT NULL UNIQUE,title TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'draft',zhihu_id TEXT UNIQUE,zhihu_url TEXT,
                submitted_at TEXT,published_at TEXT,published_hash TEXT,last_error TEXT,
                modified_at REAL NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS zhihu_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,note_uid TEXT NOT NULL,
                captured_at TEXT NOT NULL,views INTEGER,likes INTEGER,comments INTEGER,
                favorites INTEGER,shares INTEGER,raw_json TEXT NOT NULL DEFAULT '{}',
                source TEXT NOT NULL,UNIQUE(note_uid,captured_at)
            );
            CREATE INDEX IF NOT EXISTS idx_zhihu_snapshots ON zhihu_snapshots(note_uid,captured_at);
            ''')
    def touch(self,a):
        with self.store.db() as db:
            db.execute("INSERT INTO zhihu_notes(uid,folder,title,modified_at) VALUES(?,?,?,?) ON CONFLICT(folder) DO UPDATE SET title=excluded.title,modified_at=excluded.modified_at",(uuid.uuid4().hex,a.folder,a.title,a.modified_at))
    def records(self):
        with self.store.db() as db:
            return [dict(x) for x in db.execute('SELECT * FROM zhihu_notes')]
    def get(self,uid):
        with self.store.db() as db:
            r=db.execute('SELECT * FROM zhihu_notes WHERE uid=?',(uid,)).fetchone()
            return dict(r) if r else None
    def edit(self,uid,**kwargs):
        assert kwargs and set(kwargs)<={'status','zhihu_id','zhihu_url','submitted_at','published_at','published_hash','last_error'}
        with self.store.db() as db:
            db.execute('UPDATE zhihu_notes SET '+','.join(f'{x}=?' for x in kwargs)+' WHERE uid=?',(*kwargs.values(),uid))
    def snapshot(self,uid,data:dict,source:str,at=None):
        at=at or utcnow()
        with self.store.db() as db:
            cur=db.execute('INSERT OR IGNORE INTO zhihu_snapshots(note_uid,captured_at,views,likes,comments,favorites,shares,raw_json,source) VALUES(?,?,?,?,?,?,?,?,?)',
              (uid,at,*(data.get(x) for x in ('views','likes','comments','favorites','shares')),json.dumps(data,ensure_ascii=False),source))
            return cur.rowcount>0
    def latest(self,uid):
        with self.store.db() as db:
            r=db.execute('SELECT * FROM zhihu_snapshots WHERE note_uid=? ORDER BY captured_at DESC,id DESC LIMIT 1',(uid,)).fetchone()
            return dict(r) if r else None
    def trend(self,uid):
        with self.store.db() as db:
            return [dict(r) for r in db.execute('SELECT captured_at,views,likes,comments,favorites,shares,source FROM zhihu_snapshots WHERE note_uid=? ORDER BY captured_at ASC,id ASC',(uid,))]
