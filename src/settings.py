"""Paths and local-only application settings."""
from __future__ import annotations
import json
import logging
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONTENT = ROOT / "xhs"
ZHIHU = ROOT / "zhihu"
RUNTIME = ROOT / "runtime"
CONFIG = ROOT / "config" / "config.json"
DEFAULTS = json.loads((ROOT / "config" / "config.example.json").read_text(encoding="utf-8"))

def settings() -> dict:
    if CONFIG.is_file():
        try:
            supplied = json.loads(CONFIG.read_text(encoding="utf-8-sig"))
            if isinstance(supplied, dict):
                return {**DEFAULTS, **supplied}
        except (OSError, ValueError):
            pass
    return DEFAULTS.copy()

def setup_dirs() -> None:
    # Non-overwriting migration for previous TravelNotes/content/ installations.
    legacy = ROOT / "content"
    CONTENT.mkdir(parents=True, exist_ok=True)
    if legacy.is_dir():
        for item in legacy.iterdir():
            if item.is_dir() and not item.is_symlink() and not item.name.startswith('.'):
                destination = CONTENT / item.name
                if not destination.exists():
                    item.rename(destination)
                else:
                    logging.warning('旧 content/ 笔记与 xhs/ 同名，保留原目录：%s', item.name)
    for path in (CONTENT, ZHIHU, RUNTIME, RUNTIME / "image-cache", RUNTIME / "logs", RUNTIME / "browser-profile"):
        path.mkdir(parents=True, exist_ok=True)
