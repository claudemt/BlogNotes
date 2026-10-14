"""Read-only platform inventory and historical metrics, independent of local drafts.

Remote records never imply that the complete text or media was archived. All
import operations are explicit, transactional and never overwrite local notes.
"""
from __future__ import annotations
import json
from .storage import utcnow
from . import service

METRICS=('views','impressions','likes','favorites','comments','shares','followers',
         'click_rate','view_time_avg','danmaku')

def setup():
    with service.store.db() as db:
        db.executescript('''
          CREATE TABLE IF NOT EXISTS remote_posts (
            platform TEXT NOT NULL, remote_id TEXT NOT NULL, title TEXT NOT NULL,
            url TEXT NOT NULL, published_at TEXT, metadata TEXT NOT NULL,
            last_seen TEXT NOT NULL, PRIMARY KEY(platform, remote_id));
          CREATE TABLE IF NOT EXISTS remote_snapshots (
            platform TEXT NOT NULL, remote_id TEXT NOT NULL, captured_at TEXT NOT NULL,
            metrics TEXT NOT NULL, PRIMARY KEY(platform, remote_id, captured_at));
          CREATE INDEX IF NOT EXISTS idx_remote_post_date
            ON remote_posts(platform, published_at);
        ''')

def update(platform:str, posts:list[dict]):
    if platform not in ('xhs','zhihu'):raise ValueError('Unsupported platform')
    setup()
    now=utcnow()
    with service.store.db() as db:
        for p in posts:
            ident=str(p.get('xhs_id' if platform=='xhs' else 'zhihu_id') or '').strip()
            if not ident:continue
            url=p.get('url') or p.get('zhihu_url') or ('https://www.xiaohongshu.com/explore/'+ident if platform=='xhs' else '')
            if not url:continue
            title=str(p.get('title') or ident)
            at=p.get('published_at')
            if isinstance(at,(int,float)):
                from datetime import datetime, timezone
                at=datetime.fromtimestamp(at/1000 if at>1e11 else at,tz=timezone.utc).isoformat()
            metrics={k:p.get(k) for k in METRICS}
            db.execute('''INSERT INTO remote_posts(platform,remote_id,title,url,published_at,metadata,last_seen)
              VALUES(?,?,?,?,?,?,?) ON CONFLICT(platform,remote_id) DO UPDATE SET
              title=excluded.title,url=excluded.url,published_at=COALESCE(excluded.published_at,remote_posts.published_at),
              metadata=excluded.metadata,last_seen=excluded.last_seen''',
              (platform,ident,title,url,at,json.dumps(p,ensure_ascii=False),now))
            if any(v is not None for v in metrics.values()):
                db.execute('INSERT OR IGNORE INTO remote_snapshots VALUES(?,?,?,?)',
                           (platform,ident,now,json.dumps(metrics,ensure_ascii=False)))

def records(platform):
    setup()
    with service.store.db() as db:
        rows=db.execute('SELECT * FROM remote_posts WHERE platform=? ORDER BY published_at DESC',(platform,)).fetchall()
    return [dict(x)|{'metadata':json.loads(x['metadata'])} for x in rows]

def get(platform,ident):
    setup()
    with service.store.db() as db:
        r=db.execute('SELECT * FROM remote_posts WHERE platform=? AND remote_id=?',(platform,ident)).fetchone()
    return (dict(r)|{'metadata':json.loads(r['metadata'])}) if r else None

def timeline(platform, ident):
    setup()
    with service.store.db() as db:
        rows=db.execute('SELECT * FROM remote_snapshots WHERE platform=? AND remote_id=? ORDER BY captured_at',(platform,ident)).fetchall()
    return [dict(captured_at=r['captured_at'],**json.loads(r['metrics'])) for r in rows]

def summary(p):
    platform=p['platform'];id=p['remote_id'];meta=p['metadata']
    return dict(uid=f'remote:{platform}:{id}',platform=platform,remote_only=True,
      folder='',title=p['title'],status='published',platform_id=id,url=p['url'],
      published_at=p['published_at'],modified_at=0,image_count=0,image_names=[],
      issue_count=0,issues=[],fingerprint='',format='markdown' if platform=='zhihu' else 'text',
      dirty=False,stats={k:meta.get(k) for k in METRICS},last_captured_at=p['last_seen'],last_error=None,
      partial=bool(meta.get('partial')))
