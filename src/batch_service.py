"""The queue scheduler executes one approved publication at a time.

No captcha bypass, proxy rotation, stealth, or automatic retry. An ambiguous result
pauses the entire queue until the account owner resolves it.
"""
from __future__ import annotations
import logging
import threading
from . import service, workspace
from .publish_queue import queue

logger=logging.getLogger(__name__)
_DISPATCH_LOCK=threading.Lock()

def tick():
    if not _DISPATCH_LOCK.acquire(blocking=False):return
    try:return _tick_serial()
    finally:_DISPATCH_LOCK.release()

def _tick_serial():
    if service.jobs.busy:return
    for batch in queue.ready_batch():
        future,_,_,_=queue.next_allowed(batch['platform'])
        from .publish_queue import now
        if future>now():continue
        uid=queue.claim(batch['id'])
        if not uid:continue
        def publish(job, bid=batch['id'], p=batch['platform'], item_uid=uid):
            try:
                manager=workspace.backend(p)
                rec,item=manager.get(item_uid)
                if rec['status']=='published':
                    queue.finish(bid,item_uid,'skipped','此前已发布')
                    job.report('跳过已发布笔记')
                    return
                if rec['status']!='draft':
                    raise ValueError('状态不是未发布，请先完成核对')
                result=manager.publish(item_uid,job)
                if result.get('status')=='published':
                    queue.finish(bid,item_uid,'done','已发布')
                else:
                    queue.finish(bid,item_uid,'attention','发布尚未确认，请先对账')
                    job.report('队列已暂停：待核对')
            except Exception as exc:
                queue.finish(bid,item_uid,'attention',str(exc))
                raise
        try:
            service.jobs.submit('批量发布 · '+batch['platform'],publish)
        except RuntimeError:
            queue.finish(batch['id'],uid,'attention','无法启动任务，请检查状态')
        break

def queue_view(platform=None):
    rows=queue.list(platform)
    for row in rows:
        next_time,used,limit,gap=queue.next_allowed(row['platform'])
        row['quota']={'used':used,'limit':limit,'gap_minutes':gap,'next_allowed_at':next_time.isoformat(timespec='seconds')}
        counts={k:sum(item['state']==k for item in row['items']) for k in ('queued','processing','done','attention','skipped')}
        row['counts']=counts
    return rows
