"""English-only command-line interface for the local workspace.

Invoke via ``runtime/.venv/Scripts/python.exe -m src.cli COMMAND`` on Windows.
The frontend keeps its Chinese strings; terminal messages use English.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys

from .settings import RUNTIME

CLI_LOG = logging.getLogger("blognotes.cli")
CLI_LOG.propagate = False
if not CLI_LOG.handlers:
    log_dir = RUNTIME / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    CLI_LOG.addHandler(logging.FileHandler(log_dir / "blognotes.log", encoding="utf-8"))


def _publish(args: argparse.Namespace) -> int:
    from .workspace import backend
    manager = backend(args.platform)
    row = next((r for r in manager.catalog() if r['folder'] == args.name), None)
    if row is None:
        print('ERROR: Note folder not found.', file=sys.stderr)
        return 2
    print(f"Platform: {args.platform}\nTitle: {row['title']}\nImages: {row['image_count']}")
    if not args.confirm:
        print('Preview only. Add --confirm to submit this post.')
        return 0
    # Background jobs show Chinese messages in the local UI; keep the CLI English.
    job = type('CLIProgress', (), {'report': staticmethod(lambda message: None)})()
    try:
        result = manager.publish(row['uid'], job)
        print('Publish request completed. Check the publication status in the local UI.')
        if result:
            print(json.dumps(result, ensure_ascii=False, default=str))
        return 0
    except Exception:
        CLI_LOG.exception('CLI publish operation failed')
        print('ERROR: Publication failed or requires reconciliation. Do not retry blindly.', file=sys.stderr)
        return 1


def _sync(args: argparse.Namespace) -> int:
    from .workspace import PLATFORMS
    job = type('CLIProgress', (), {'report': staticmethod(lambda message: None)})()
    failures = 0
    for name, manager in PLATFORMS.items():
        if args.platform not in (name, 'all'):
            continue
        try:
            result = manager.sync(job)
            print(f'{name}: sync finished; result={result}')
        except Exception:
            failures += 1
            CLI_LOG.exception('%s metric sync failed', name)
            print(f'{name}: sync failed. See runtime/logs/blognotes.log.', file=sys.stderr)
    return 1 if failures else 0


def _verify(args: argparse.Namespace) -> int:
    from .verify import run_checks
    return run_checks(xhs_read=args.xhs_read, zhihu_read=args.zhihu_read, json_output=args.json)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='BlogNotes local management CLI')
    sub = parser.add_subparsers(dest='command', required=True)
    pub = sub.add_parser('publish', help='Preview or publish a local note')
    pub.add_argument('name', help='Note directory name')
    pub.add_argument('--platform', choices=['xhs', 'zhihu'], default='xhs')
    pub.add_argument('--confirm', action='store_true', help='Authorize sending the post')
    pub.set_defaults(func=_publish)
    sync = sub.add_parser('sync', help='Fetch metrics for published notes')
    sync.add_argument('--platform', choices=['xhs', 'zhihu', 'all'], default='all')
    sync.set_defaults(func=_sync)
    verify = sub.add_parser('verify', help='Check installation and optional authenticated read APIs')
    verify.add_argument('--xhs-read', action='store_true', help='Read-only XHS creator login check')
    verify.add_argument('--zhihu-read', action='store_true', help='Read-only Zhihu data check')
    verify.add_argument('--json', action='store_true', help='JSON output')
    verify.set_defaults(func=_verify)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == '__main__':
    raise SystemExit(main())
