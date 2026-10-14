"""Non-destructive conversion of numbered images to PNG in runtime cache."""
from __future__ import annotations
import hashlib
from pathlib import Path
from PIL import Image, ImageOps, UnidentifiedImageError

try:
    import pillow_heif
    pillow_heif.register_heif_opener()
except ImportError:
    pass

class MediaError(ValueError):
    pass

def prepare_pngs(images: tuple[Path, ...], runtime: Path) -> list[Path]:
    cache_root = runtime / "image-cache"
    cache_root.mkdir(parents=True, exist_ok=True)
    output = []
    for item in images:
        if item.suffix.lower() == ".png":
            # PNG inputs already satisfy output contract. No mutation of originals.
            output.append(item.resolve())
            continue
        st = item.stat()
        digest = hashlib.sha256(f"{item.resolve()}:{st.st_mtime_ns}:{st.st_size}".encode()).hexdigest()[:24]
        target = cache_root / f"{digest}.png"
        if not target.is_file():
            try:
                with Image.open(item) as im:
                    im = ImageOps.exif_transpose(im)
                    if im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info):
                        im = im.convert("RGBA")
                    else:
                        im = im.convert("RGB")
                    temp = target.with_suffix(".partial")
                    im.save(temp, format="PNG", optimize=True)
                    temp.replace(target)
            except (UnidentifiedImageError, OSError, ValueError) as exc:
                raise MediaError(f"无法转换 {item.name}：{exc}") from exc
        output.append(target)
    return output
