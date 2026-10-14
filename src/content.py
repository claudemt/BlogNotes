"""Folder-is-note catalog. No Markdown or front matter is accepted."""
from __future__ import annotations
import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff", ".heic", ".heif", ".gif"}
IMAGE_RE = re.compile(r"^(\d+)\.(png|jpg|jpeg|webp|bmp|tif|tiff|heic|heif|gif)$", re.IGNORECASE)
HASHTAG_RE = re.compile(r"(?<!\S)#[^\s#]+")

@dataclass(frozen=True)
class NoteSource:
    slug: str
    title: str
    body: str
    images: tuple[Path, ...]
    modified_at: float
    fingerprint: str
    issues: tuple[str, ...]

    @property
    def tags(self) -> list[str]:
        last = self.body.strip().splitlines()[-1] if self.body.strip() else ""
        matches = HASHTAG_RE.findall(last)
        return [s.lstrip("#") for s in matches] if matches and HASHTAG_RE.sub("", last).strip() == "" else []


def load_note(folder: Path) -> NoteSource:
    """Always tolerate invalid drafts; only publication is blocked."""
    issues = []
    txt = folder / "文案.txt"
    try:
        body = txt.read_text(encoding="utf-8-sig").replace("\r\n", "\n")
    except UnicodeError:
        body = ""
        issues.append("文案.txt 必须为 UTF-8 编码")
    except OSError:
        body = ""
        issues.append("缺少文案.txt")
    if not body.strip():
        issues.append("正文不能为空")
    if len(folder.name) > 20:
        issues.append("目录名超过建议的小红书标题长度（20 字符），请缩短名称")
    images_by_index: dict[int, list[Path]] = {}
    for p in folder.iterdir():
        if not p.is_file() or p.is_symlink():
            continue
        match = IMAGE_RE.fullmatch(p.name)
        if match:
            images_by_index.setdefault(int(match.group(1)), []).append(p)
        elif p.suffix.lower() in IMAGE_EXTS:
            issues.append(f"图片文件名必须采用数字编号：{p.name}")
    for idx, files in images_by_index.items():
        if len(files) > 1:
            issues.append(f"图片编号 {idx:02d} 重复（多个格式或不同零前缀）")
    indices = sorted(images_by_index)
    if not indices:
        issues.append("至少需要一张图片")
    if indices and indices != list(range(1, len(indices) + 1)):
        issues.append("图片编号必须从 01 起连续排列，不能跳号")
    images = tuple(sorted((files[0] for _, files in sorted(images_by_index.items())), key=lambda x: int(IMAGE_RE.fullmatch(x.name).group(1))))
    h = hashlib.sha256()
    h.update(folder.name.encode("utf-8"))
    h.update(body.encode("utf-8"))
    modified_at = txt.stat().st_mtime if txt.exists() else folder.stat().st_mtime
    for p in images:
        st = p.stat()
        modified_at = max(modified_at, st.st_mtime)
        # Source signature. Includes metadata for speed; not cryptographic file-content hash.
        h.update(f"{p.name}\0{st.st_size}\0{st.st_mtime_ns}".encode("utf-8"))
    return NoteSource(folder.name, folder.name, body, images, modified_at, h.hexdigest(), tuple(issues))


def scan(content_root: Path) -> list[NoteSource]:
    if not content_root.is_dir():
        return []
    notes = []
    for p in sorted(content_root.iterdir(), key=lambda x: x.name.casefold()):
        if p.is_dir() and not p.is_symlink() and not p.name.startswith("."):
            notes.append(load_note(p))
    return notes
