"""Offline and explicitly-requested read-only account diagnostics."""
from __future__ import annotations

import importlib.util
import json
import platform
import sys

from .settings import CONTENT, ZHIHU, RUNTIME, settings, setup_dirs


def check_local() -> list[dict]:
    setup_dirs()
    results = []

    def add(name, ok, details):
        results.append({'item': name, 'ok': bool(ok), 'detail': str(details)})

    add('Python >= 3.10', sys.version_info >= (3, 10), platform.python_version())
    for label, name in [
        ('FastAPI', 'fastapi'), ('Playwright', 'playwright'),
        ('Pillow', 'PIL'), ('Markdown', 'markdown'),
        ('Uvicorn', 'uvicorn'), ('HTTPX', 'httpx'),
    ]:
        installed = importlib.util.find_spec(name) is not None
        add(label, installed, 'installed' if installed else 'missing')
    available = importlib.util.find_spec('mediacloud_uploader') is not None
    add('Zhihu media uploader (optional)', True, 'installed' if available else 'not installed')
    for directory in (CONTENT, ZHIHU, RUNTIME):
        add(f'{directory.name} directory', directory.is_dir(), str(directory))
    add('Local HTTP', True, f"127.0.0.1:{settings()['port']}")
    try:
        from .platforms import xiaohongshu as xhs
        add('Chrome / Edge', True, xhs._chrome_path())
    except Exception:
        add('Chrome / Edge', False, 'not found; set chrome_path in config/config.json')
    from .platforms import zhihu
    creds = zhihu.credential_status()
    add('Zhihu Publisher credentials', True,
        'configured (access not verified)' if creds['publish_ready'] else 'not configured')
    add('Zhihu data credentials', True,
        'configured (access not verified)' if creds['metrics_ready'] else 'not configured')
    return results


def run_checks(*, xhs_read: bool = False, zhihu_read: bool = False, json_output: bool = False) -> int:
    results = check_local()
    if xhs_read:
        try:
            from .platforms import xiaohongshu as xhs
            valid = xhs.check_login()
            results.append({'item': 'XHS creator account read', 'ok': bool(valid),
                            'detail': 'Login and response verified' if valid else 'Login missing or endpoint changed'})
        except Exception as exc:
            results.append({'item': 'XHS creator account read', 'ok': False, 'detail': type(exc).__name__})
    if zhihu_read:
        try:
            from .platforms import zhihu
            notes = zhihu.list_articles(limit=1)
            results.append({'item': 'Zhihu user content read', 'ok': True,
                            'detail': f'{len(notes)} item(s) retrieved'})
        except Exception as exc:
            results.append({'item': 'Zhihu user content read', 'ok': False, 'detail': type(exc).__name__})
    if json_output:
        print(json.dumps({'checks': results}, ensure_ascii=False, indent=2))
    else:
        for item in results:
            print(('OK   ' if item['ok'] else 'FAIL ') + item['item'] + ': ' + item['detail'])
    return 2 if any(not item['ok'] for item in results) else 0


if __name__ == '__main__':
    raise SystemExit(run_checks())
