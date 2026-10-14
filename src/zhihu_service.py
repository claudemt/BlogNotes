"""Zhihu article publication and metrics. No blind retries for non-idempotent writes."""
from __future__ import annotations
from datetime import datetime,timezone
from .settings import ZHIHU,RUNTIME,setup_dirs
from . import service
from . import zhihu_content as content
from .zhihu_store import ZhihuStore
from .platforms import zhihu
from . import remote
from .storage import utcnow
from .presentation import note_summary, note_detail
from .publish_queue import queue

setup_dirs()
zs=ZhihuStore(service.store)

def sources():
    found=content.scan(ZHIHU)
    for item in found:zs.touch(item)
    return {x.folder:x for x in found}

def get(uid):
    found=sources();row=zs.get(uid)
    if not row or row['folder'] not in found:raise KeyError('笔记不存在')
    return row,found[row['folder']]

def _iso(epoch):
    if epoch is None:return utcnow()
    if isinstance(epoch,(int,float)):
        return datetime.fromtimestamp(float(epoch),timezone.utc).isoformat(timespec='seconds')
    return str(epoch)

def present(row,item):
    return note_summary('zhihu', row, item, zs.latest(row['uid']))


def catalog():
    found=sources();return [present(r,found[r['folder']]) for r in zs.records() if r['folder'] in found]

def detail(uid):
    row,item=get(uid)
    names={p.name:i for i,p in enumerate(item.images)}
    html=content.render(item,image_url=lambda name:f'/api/notes/{uid}/images/{names[name]}')
    return note_detail(present(row,item),item,html=html)


def link(uid,article_id,job=None):
    row,item=get(uid)
    if row['status']=='published':raise ValueError('已关联')
    records=zhihu.list_articles()
    matches=[x for x in records if x['zhihu_id']==article_id]
    if len(matches)!=1:raise ValueError('没有从知乎授权内容列表核对到这篇文章')
    if any(r['zhihu_id']==article_id and r['uid']!=uid for r in zs.records()):raise ValueError('文章 ID 已关联其他笔记')
    found=matches[0]
    zs.edit(uid,status='published',zhihu_id=article_id,zhihu_url=found['zhihu_url'],published_at=_iso(found['published_at']),published_hash=item.fingerprint,last_error=None)
    zs.snapshot(uid,found,'official_user_contents')
    if job:job.report('关联完成')

def publish_one(uid,job=None):
    row,item=get(uid)
    if row['status']!='draft':raise ValueError('只允许发布未发布笔记')
    if item.issues:raise ValueError('；'.join(item.issues))
    if not zhihu.credential_status()['publish_ready']:raise ValueError('请先配置知乎 Publisher 凭证')
    if item.images:
        try:from mediacloud_uploader.client import MediaCloudUploader
        except ImportError as exc:raise ValueError('缺少官方图片上传依赖，请安装 config/zhihu-media-requirements.txt') from exc
    if job:job.report('检查文章与官方授权')
    # Upload/preparation failures are safe to retry: no article was submitted.
    # Persist 'pending' immediately before the irreversible POST, not before media upload.
    def before_submit():
        queue.reserve('zhihu',uid,origin='single-or-batch')
        zs.edit(uid,status='pending',submitted_at=utcnow(),published_hash=item.fingerprint,last_error=None)
    try:
        token,url=zhihu.publish(item,list(item.images),confirmed=True,report=job.report if job else lambda x:None,before_submit=before_submit)
    except zhihu.KnownReject as exc:
        zs.edit(uid,status='draft',submitted_at=None,published_hash=None,last_error=str(exc))
        raise
    except Exception as exc:
        status_now=zs.get(uid)['status']
        if status_now=='pending':
            zs.edit(uid,last_error='提交结果不确定；请核对知乎文章列表后再恢复。'+str(exc))
        else:
            zs.edit(uid,last_error='准备失败，未提交文章：'+str(exc))
        raise
    if any(r['zhihu_id']==token and r['uid']!=uid for r in zs.records()):
        zs.edit(uid,last_error='知乎文章 ID 冲突，需要人工核对')
        raise ValueError('文章 ID 已关联其他笔记')
    zs.edit(uid,status='published',zhihu_id=token,zhihu_url=url,published_at=utcnow(),last_error=None)
    if job:job.report('发布成功 · '+url)
    return {'status':'published','zhihu_id':token,'url':url}

def sync_one(uid,rows=None):
    row,item=get(uid)
    if row['status']!='published' or not row['zhihu_id']:return False
    if rows is None:rows=zhihu.list_articles()
    match=next((r for r in rows if r['zhihu_id']==row['zhihu_id']),None)
    if not match:return False
    zs.snapshot(uid,match,'official_user_contents')
    return True

def synchronize(job=None):
    rows=zhihu.list_articles()
    remote.update('zhihu',rows)
    seen=0
    for row in zs.records():
        if row['status']!='published' or not row['zhihu_id']:continue
        match=next((r for r in rows if r['zhihu_id']==row['zhihu_id']),None)
        if match:
            zs.snapshot(row['uid'],match,'official_user_contents')
            seen+=1
    service.store.set_meta('zhihu_last_synced_at',utcnow())
    if job:job.report(f'已同步 {seen} 篇知乎文章')
    return {'read':len(rows),'matched':seen}

def reset(uid):
    row,_=get(uid)
    if row['status']!='pending':raise ValueError('只有待确认的文章可复位')
    zs.edit(uid,status='draft',zhihu_id=None,zhihu_url=None,submitted_at=None,published_hash=None,last_error=None)
