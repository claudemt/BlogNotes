"""Application use-cases. Both CLI and HTTP routes invoke these functions."""
from __future__ import annotations
import json
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

from .content import NoteSource, scan
from .media import prepare_pngs
from .storage import Store, utcnow
from .settings import CONTENT, RUNTIME, settings, setup_dirs
from .jobs import JobManager
from .publish_queue import queue
from .platforms import xiaohongshu as xhs
from . import remote
from .presentation import note_summary, note_detail

logger = logging.getLogger(__name__)
setup_dirs()
store = Store(RUNTIME / 'data.sqlite')
jobs = JobManager()


def sources() -> dict[str, NoteSource]:
    found = scan(CONTENT)
    for source in found:
        store.touch_source(source)
    return {source.slug: source for source in found}


def get(uid: str) -> tuple[dict, NoteSource]:
    listed = sources()
    note = store.note(uid)
    if not note or note['folder'] not in listed:
        raise KeyError('笔记不存在或者目录已移除')
    return note, listed[note['folder']]


def _present(note: dict, source: NoteSource) -> dict:
    return note_summary('xhs', note, source, store.latest(note['uid']))


def catalog() -> list[dict]:
    found = sources()
    records = [_present(x, found[x['folder']]) for x in store.records() if x['folder'] in found]
    # Live statuses can be draft, publishing, pending, or published.
    # User-facing 'published' tab displays only verified records.
    records.sort(key=lambda x: (x['published_at'] or '' if x['status']=='published' else '', x['modified_at']), reverse=True)
    return records


def detail(uid: str) -> dict:
    note, source = get(uid)
    result = note_detail(_present(note, source), source)
    result['tags'] = source.tags
    result['submitted_at'] = note['submitted_at']
    result['timeline_count'] = len(store.timeline(uid))
    return result


def _reconcile(stats: list[dict], report=lambda message:None) -> tuple[int, int]:
    """Only trust exact ID, or unambiguous recent title+time for pending submissions."""
    found = sources()
    allrows = store.records()
    by_xhs_id = {x['xhs_id']: x for x in allrows if x['xhs_id'] and x['folder'] in found}
    linked = 0
    saved = 0
    for row in stats:
        note_id = row.get('xhs_id')
        if not note_id:
            continue
        rec = by_xhs_id.get(note_id)
        if rec is None:
            pending = []
            for item in allrows:
                if item['folder'] not in found or item['status'] != 'pending' or item['title'] != row.get('title'):
                    continue
                if item['xhs_id'] and item['xhs_id'] != note_id:
                    continue
                # Already obtained an exact note link: let ID matching handle this.
                if item['submitted_at'] and row.get('published_at'):
                    try:
                        sent = datetime.fromisoformat(item['submitted_at'])
                        when = datetime.fromisoformat(row['published_at'])
                        # Broad enough for clock differences but not to bind old posts.
                        if sent - timedelta(minutes=5) <= when <= sent + timedelta(hours=2):
                            pending.append(item)
                    except (ValueError, TypeError):
                        pass
            if len(pending) == 1:
                rec = pending[0]
                store.modify(rec['uid'], xhs_id=note_id, status='published', published_at=row.get('published_at') or utcnow(), last_error=None)
                by_xhs_id[note_id] = {**rec, 'status': 'published'}
                rec = by_xhs_id[note_id]
                linked += 1
                report(f"已核对发布：{rec['title']}")
        if rec:
            if rec['status'] == 'pending':
                store.modify(rec['uid'],status='published',published_at=row.get('published_at') or utcnow(),last_error=None)
                linked += 1
            if rec['status'] in ('published', 'pending'):
                if store.snapshot(rec['uid'], row):
                    saved += 1
    return linked, saved


def synchronize(job=None) -> dict:
    def report(msg):
        if job: job.report(msg)
    report('读取小红书数据')
    try:
        stats = xhs.list_statistics(report=report)
    except Exception as exc:
        stats = []
        report('统计暂不可用：'+str(exc))
    linked, saved = _reconcile(stats, report=report)
    # The creator's complete published-work inventory includes older notes
    # without local directories. Keep existing remote rows if pagination fails.
    inventory_ok=False
    try:
        posted=xhs.list_published()
        inventory_ok=True
        # Present keys win: a creator-center row carries interaction numbers, a
        # www-profile row carries none. Merging in that order means a degraded
        # source never overwrites real metrics with nulls.
        merged={r['xhs_id']:r for r in stats if r.get('xhs_id')}
        for item in posted:
            merged[item['xhs_id']]={**merged.get(item['xhs_id'],{}),**item}
        status = xhs.inventory_status()
        for row in merged.values():
            row['partial'] = bool(status.get('partial'))
            row['sources'] = status.get('sources', [])
        remote.update('xhs',list(merged.values()))
        store.set_meta('xhs_inventory', json.dumps(status, ensure_ascii=False))
        if status.get('partial'):
            report('部分数据：'+(str(status.get('reason')) or '无互动指标'))
        elif status.get('empty'):
            report('暂无已发布作品')
        report(f'远端作品 {len(merged)} 篇')
    except Exception as exc:
        remote.update('xhs',stats)
        report('作品列表暂不可用：'+str(exc))
    if not stats and not inventory_ok:
        raise ValueError('未取到任何数据，请检查登录')
    store.set_meta('last_synced_at', utcnow())
    report(f"完成：读取 {len(stats)} · 新增 {saved} · 确认 {linked}")
    return {'read':len(stats),'saved':saved,'linked':linked}


def publish_one(uid: str, job=None) -> dict:
    def report(msg):
        if job: job.report(msg)
    note, source = get(uid)
    if note['status'] not in ('draft',):
        raise ValueError('当前状态不允许再次发布；待确认笔记需先对账，不能重复发布')
    if source.issues:
        raise ValueError('请先修正：'+'；'.join(source.issues))
    max_images = int(settings().get('image_limit',18))
    if len(source.images) > max_images:
        raise ValueError(f'图片数量 {len(source.images)} 超过配置上限 {max_images}')
    report('转换与检查图片')
    files = prepare_pngs(source.images, RUNTIME)
    def before_submit():
        # Called by the browser adapter directly before the irreversible click.
        queue.reserve('xhs',uid,origin='single-or-batch')
        store.modify(uid,status='pending',submitted_at=utcnow(),published_hash=source.fingerprint,last_error=None)
    try:
        # source.tags is the trailing "#tag" line, which the adapter re-attaches
        # as real topics; passing it keeps the body from carrying the line twice.
        suggested_id = xhs.publish(source.title, source.body, files, report=report, before_submit=before_submit,
                                   topics=source.tags)
    except Exception as exc:
        if store.note(uid)['status'] == 'pending':
            store.modify(uid,last_error='提交状态未知，请先刷新账号作品数据对账。'+str(exc))
        else:
            store.modify(uid,last_error='发布准备未完成，尚未点击发布：'+str(exc))
        raise
    if store.note(uid)['status'] != 'pending':
        raise RuntimeError('发布适配器未进入提交阶段；禁止假定发布成功')
    if suggested_id:
        owner = next((r for r in store.records() if r['xhs_id']==suggested_id and r['uid'] != uid),None)
        if owner:
            store.modify(uid,last_error='检测到已有笔记使用了相同的 ID，禁止关联。')
            raise RuntimeError('检测到笔记 ID 冲突，需人工处理')
        store.modify(uid,xhs_id=suggested_id)
    report('发布按钮已提交，正在核对账号作品列表')
    try:
        result = synchronize(job)
    except Exception as exc:
        store.modify(uid,last_error='已提交，但自动确认失败：'+str(exc))
        report('无法确认最终发布结果，已保留“待确认”以防重复发布')
        return {'status':'pending'}
    final = store.note(uid)
    if final and final['status']=='published':
        return {'status':'published','xhs_id':final['xhs_id']}
    store.modify(uid,last_error='尚未从账号作品列表核对到此笔记。请稍后刷新数据，不要重复发布。')
    report('尚未确认发布，已保留“待确认”')
    return {'status':'pending'}


def release_pending(uid: str):
    note, _ = get(uid)
    if note['status'] != 'pending':
        raise ValueError('只有待确认状态可以手工复位')
    # User deliberately attests note is absent. Do not silently reset on network failure.
    store.modify(uid, status='draft', xhs_id=None,submitted_at=None,published_hash=None,last_error=None)


def manual_link(uid: str, platform_id: str, job=None):
    note, source = get(uid)
    if note['status'] == 'published':
        raise ValueError('该笔记已关联')
    if not platform_id or not platform_id.isalnum() or len(platform_id) > 48:
        raise ValueError('笔记 ID 不合法')
    if any(r['xhs_id']==platform_id for r in store.records()):
        raise ValueError('该平台 ID 已关联其他笔记')
    records = xhs.list_statistics(report=(job.report if job else lambda msg:None))
    verified = next((r for r in records if r['xhs_id']==platform_id),None)
    if not verified:
        raise ValueError('创作者中心未找到该笔记，无法建立关联')
    store.modify(uid,xhs_id=platform_id,status='published',published_at=verified['published_at'] or utcnow(),published_hash=source.fingerprint,last_error=None)
    store.snapshot(uid,verified)
