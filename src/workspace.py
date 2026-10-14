"""One local note API; platform-specific transport and legacy persistence stay isolated.

The existing SQLite tables are intentionally NOT migrated. Every operation goes
through this facade, and the platform modules remain the only integration points
for remote requests. This preserves previously published IDs and snapshots.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .settings import CONTENT, ZHIHU
from . import service, zhihu_service, remote
from .platforms import xiaohongshu as xhs, zhihu
from .storage import utcnow


@dataclass(frozen=True)
class Platform:
    key: str
    label: str
    folder: Path
    operations: Any
    metrics_key: str

    @property
    def store(self):
        return zhihu_service.zs if self.key == 'zhihu' else service.store

    def catalog(self) -> list[dict]:
        local=self.operations.catalog()
        known={x.get('platform_id') for x in local if x.get('platform_id')}
        extra=[remote.summary(p) for p in remote.records(self.key) if p['remote_id'] not in known]
        return local+extra

    def get(self, uid: str):
        return self.operations.get(uid)

    def detail(self, uid: str) -> dict:
        return self.operations.detail(uid)

    def timeline(self, uid: str) -> list[dict]:
        return self.store.trend(uid) if self.key == 'zhihu' else self.store.timeline(uid)

    def publish(self, uid: str, job=None) -> dict:
        return self.operations.publish_one(uid, job)

    def sync(self, job=None) -> dict:
        return self.operations.synchronize(job)

    def link(self, uid: str, platform_id: str, job=None):
        func = self.operations.link if self.key == 'zhihu' else self.operations.manual_link
        return func(uid, platform_id, job)

    def reset(self, uid: str):
        func = self.operations.reset if self.key == 'zhihu' else self.operations.release_pending
        return func(uid)

    def online_ready(self) -> bool:
        if self.key == 'xhs':
            return xhs.is_browser_open()
        return bool(zhihu.credential_status()['metrics_ready'])

    def last_sync(self) -> str | None:
        return service.store.get_meta(self.metrics_key)

    def sync_one(self, uid: str, job=None):
        row, _ = self.get(uid)
        if row['status'] not in ('published', 'pending'):
            raise ValueError('该笔记尚未发布')
        if self.key == 'xhs':
            return self.sync(job)  # XHS creator statistics API returns a full list
        if row['status'] != 'published':
            raise ValueError('知乎待确认文章需先核对文章 ID')
        if not zhihu_service.sync_one(uid):
            raise ValueError('授权作品列表未找到此文章')
        service.store.set_meta(self.metrics_key, utcnow())
        if job:
            job.report('数据已更新')
        return {'matched': 1}


PLATFORMS = {
    'xhs': Platform('xhs', '小红书', CONTENT, service, 'last_synced_at'),
    'zhihu': Platform('zhihu', '知乎', ZHIHU, zhihu_service, 'zhihu_last_synced_at'),
}


def backend(key: str) -> Platform:
    try:
        return PLATFORMS[key]
    except KeyError as exc:
        raise ValueError('未知平台') from exc


def resolve(uid: str) -> Platform:
    """Resolve from persisted publication tables; never guess from a title/path."""
    present = [p for p in PLATFORMS.values() if
               (p.store.get(uid) if p.key == 'zhihu' else p.store.note(uid)) is not None]
    if not present:
        raise KeyError('笔记不存在')
    if len(present) != 1:
        raise ValueError('笔记 ID 冲突，无法确定平台')
    return present[0]


def get(uid: str):
    return resolve(uid).get(uid)


def detail(uid: str) -> dict:
    if uid.startswith('remote:'):
        parts=uid.split(':',2)
        if len(parts)!=3 or parts[1] not in PLATFORMS:raise KeyError('笔记不存在')
        row=remote.get(parts[1],parts[2])
        if not row:raise KeyError('笔记不存在')
        return remote.summary(row)|{'body':'','images':[],'remote_only':True,
            'issues':['请先导入完整正文和图片'], 'tags':[], 'timeline_count':len(remote.timeline(parts[1],parts[2]))}
    return resolve(uid).detail(uid)


def snapshot(uid: str) -> list[dict]:
    if uid.startswith('remote:'):
        parts=uid.split(':',2)
        if len(parts)!=3 or parts[1] not in PLATFORMS or not remote.get(parts[1],parts[2]):raise KeyError('笔记不存在')
        return remote.timeline(parts[1],parts[2])
    return resolve(uid).timeline(uid)
