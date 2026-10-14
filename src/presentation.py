"""Shared HTTP-facing note model. Platform details are deliberately absent from JS."""
from __future__ import annotations

STATS = ('views', 'impressions', 'likes', 'comments', 'favorites', 'shares', 'followers',
         'click_rate', 'view_time_avg', 'danmaku')


def note_summary(platform: str, row: dict, source, snapshot: dict | None) -> dict:
    is_zhihu = platform == 'zhihu'
    note_id = row.get('zhihu_id') if is_zhihu else row.get('xhs_id')
    url = row.get('zhihu_url') if is_zhihu else (
        'https://www.xiaohongshu.com/explore/' + note_id if note_id else None)
    return {
        'uid': row['uid'], 'platform': platform,
        'folder': source.folder if is_zhihu else source.slug,
        'title': source.title,
        'status': row['status'], 'platform_id': note_id, 'url': url,
        'published_at': row.get('published_at'), 'modified_at': source.modified_at,
        'image_count': len(source.images), 'image_names': [p.name for p in source.images],
        'issue_count': len(source.issues), 'issues': list(source.issues),
        'fingerprint': source.fingerprint, 'format': 'markdown' if is_zhihu else 'text',
        'dirty': bool(row.get('published_hash') and row['published_hash'] != source.fingerprint),
        'stats': {k: snapshot.get(k) if snapshot else None for k in STATS},
        'last_captured_at': snapshot.get('captured_at') if snapshot else None,
        'last_error': row.get('last_error'),
    }


def note_detail(summary: dict, source, *, html: str | None = None) -> dict:
    result = dict(summary)
    uid = summary['uid']
    result['body'] = source.body
    result['images'] = [f'/api/notes/{uid}/images/{i}' for i in range(len(source.images))]
    if html is not None:
        result['html'] = html
    return result
