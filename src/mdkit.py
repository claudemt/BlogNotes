"""Markdown 渲染管线（源自 Workknacks 的“高级配方”，MIT 许可）。

提取自 UsefulTools/Workknacks/src/library/md.py 的 markdown_to_fragment：
- 以 Python-Markdown（extra/tables/fenced_code/sane_lists）为渲染引擎
- 数学公式区域保护（$..$、$$..$$、\\(..\\)、\\[..\\]），代码围栏内不误判
- 裸 ATX 标题规范化（“#标题”自动补空格）

并叠加 BlogNotes 所需的安全与适配层：
- 输出白名单净化：禁止脚本/事件属性/危险协议（drafts 永不解释原始 HTML）
- image / formula / heading_offset 回调，供知乎发布适配与编辑预览使用
"""
from __future__ import annotations

import html
import re

from bs4 import BeautifulSoup

try:
    import markdown as _md
except ImportError:  # pragma: no cover - guarded by launch.ps1 dependency check
    _md = None

_FENCE_RE = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})[ \t]*(.*)$")


# ---------------------------------------------------------------- math 保护

def _math_escaped(text: str, p: int) -> bool:
    bs = 0
    k = p - 1
    while k >= 0 and text[k] == "\\":
        bs += 1
        k -= 1
    return bs % 2 == 1


def _math_spans(text: str) -> list[tuple[int, int, str, int, str]]:
    """返回 [(start, end, tex, mode, delim)]；mode: 1=行内, 2=块级。代码围栏内跳过。"""
    n = len(text)
    mask = [False] * n
    off = 0
    in_code = False
    for ln in text.split("\n"):
        if ln.lstrip().startswith("```"):
            in_code = not in_code
            for q in range(off, off + len(ln)):
                mask[q] = True
        elif in_code:
            for q in range(off, off + len(ln)):
                mask[q] = True
        off += len(ln) + 1
    opens = ["$$", "\\[", "\\(", "$"]
    close = {"$$": "$$", "\\[": "\\]", "\\(": "\\)", "$": "$"}
    mode = {"$$": 2, "\\[": 2, "\\(": 1, "$": 1}
    spans: list = []
    i = 0
    while i < n:
        if mask[i]:
            i += 1
            continue
        od = None
        L = 0
        for d in opens:
            if text.startswith(d, i):
                od, L = d, len(d)
                break
        if od is None:
            i += 1
            continue
        if od == "$":
            if _math_escaped(text, i) or (i + 1 < n and text[i + 1] == "$"):
                i += 1
                continue
        cl = close[od]
        s = i
        j = i + L
        found = None
        while j + len(cl) <= n:
            if mask[j]:
                j += 1
                continue
            if od == "$":
                if text[j] == "$" and (j + 1 >= n or text[j + 1] != "$") and not _math_escaped(text, j):
                    found = j
                    break
                j += 1
            else:
                if text.startswith(cl, j) and not _math_escaped(text, j):
                    found = j
                    break
                j += 1
        if found is None:
            i += L
            continue
        e = found + len(cl)
        if od == "$" and e - 2 > s and (text[s + 1].isspace() or text[e - 2].isspace()):
            i += 1
            continue
        spans.append((s, e, text[s + L:found], mode[od], od))
        i = e
    return spans


def _protect_math_regions(text: str) -> tuple[str, dict]:
    text = str(text)
    store: dict = {}
    out: list = []
    last = 0
    for k, (s, e, tex, mode, od) in enumerate(_math_spans(text)):
        out.append(text[last:s])
        tok = "MJXMATH%dZ" % k
        store[tok] = (od, tex, mode)
        out.append(tok)
        last = e
    out.append(text[last:])
    return "".join(out), store


def _restore_math(frag: str, store: dict, formula=None) -> str:
    if not store:
        return frag

    def sub(m):
        item = store.get(m.group(0))
        if item is None:
            return m.group(0)
        od, tex, mode = item
        if formula:
            return formula(tex, mode)
        return html.escape(od + tex + close_of(od))

    return re.sub(r"MJXMATH\d+Z", sub, frag)


def close_of(od: str) -> str:
    return {"$$": "$$", "\\[": "\\]", "\\(": "\\)", "$": "$"}[od]


# ------------------------------------------------------------ 标题规范化

def _normalize_atx_headings(text: str) -> str:
    """兼容“裸 #”与“#标题（无空格）”的 ATX 标题规范化。

    仅处理行首 0~3 个空格 + 1~6 个 # 的行；跳过代码围栏；7 个及以上 # 不是标题。
    """
    lines = text.split("\n")
    out: list[str] = []
    fence: tuple[str, int] | None = None
    for ln in lines:
        m = _FENCE_RE.match(ln)
        if fence:
            if m and m.group(1)[0] == fence[0] and len(m.group(1)) >= fence[1] and not m.group(2).strip():
                fence = None
            out.append(ln)
            continue
        if m:
            fence = (m.group(1)[0], len(m.group(1)))
            out.append(ln)
            continue
        hm = re.match(r"^( {0,3})(#{1,6})(?!#)([^\n]*)$", ln)
        if hm:
            lead, hashes, rest = hm.group(1), hm.group(2), hm.group(3)
            stripped = rest.rstrip(" \t")
            tail = rest[len(stripped):]
            if not stripped:
                out.append(lead + hashes + " " + tail)
            elif stripped[0].isspace():
                out.append(ln)
            else:
                out.append(lead + hashes + " " + rest)
            continue
        out.append(ln)
    return "\n".join(out)


# ---------------------------------------------------------------- 白名单净化

_ALLOWED_TAGS = {
    "p", "br", "hr", "strong", "em", "b", "i", "code", "pre", "blockquote",
    "ul", "ol", "li", "h1", "h2", "h3", "h4", "h5", "h6", "a", "img",
    "table", "thead", "tbody", "tr", "th", "td", "div", "span",
    "figure", "figcaption", "del", "ins", "mark", "sub", "sup", "dl", "dt", "dd",
}
_IMG_DATA_ATTRS = {
    "data-caption", "data-original-src", "data-watermark-src", "data-rawwidth",
    "data-rawheight", "data-size", "data-watermark", "eeimg",
}
_DROP_ALWAYS = {"script", "style", "iframe", "object", "embed", "form", "input",
                "button", "link", "meta", "base", "template", "textarea", "select"}


def sanitize_html(frag: str) -> str:
    """白名单净化渲染结果：不信任草稿中的原始 HTML。"""
    soup = BeautifulSoup(frag or "", "html.parser")
    for tag in soup.find_all(True):
        name = tag.name.lower()
        if name in _DROP_ALWAYS:
            tag.decompose()
            continue
        if name not in _ALLOWED_TAGS:
            tag.unwrap()
            continue
        for attr in list(tag.attrs):
            low = attr.lower()
            if low.startswith("on"):
                del tag.attrs[attr]
                continue
            if low == "style" or low in ("class", "id"):
                del tag.attrs[attr]
                continue
            if low.startswith("data-") and low not in _IMG_DATA_ATTRS:
                del tag.attrs[attr]
                continue
            if low in ("href", "src"):
                value = str(tag.attrs[attr]).strip().lower()
                if value.startswith(("javascript:", "vbscript:", "data:text/html")):
                    del tag.attrs[attr]
                    continue
    return str(soup)


# ------------------------------------------------------------------ 渲染入口

_IMG_RE = re.compile(r"<img\b[^>]*>", re.I)
_ATTR_RE = re.compile(r"""([a-zA-Z_:][-a-zA-Z0-9_:.]*)\s*=\s*("([^"]*)"|'([^']*)')""")
_HEADING_RE = re.compile(r"</?h([1-6])>")


def markdown_fragment(text: str, *, image=None, formula=None, heading_offset: int = 0) -> str:
    """渲染一段受控 Markdown。

    image: callable(src, alt) -> str | None，替换 <img> 输出（可校验并抛 ValueError）。
    formula: callable(tex, mode) -> str，替换数学区域输出；None 则转义原样保留（供前端 MathJax）。
    heading_offset: 标题级别整体偏移（0 不变）。
    """
    text = str(text or "").lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    toks, store = _protect_math_regions(text)
    toks = _normalize_atx_headings(toks)
    if _md is None:
        raise RuntimeError("Python-Markdown 未安装")
    frag = _md.markdown(toks, extensions=["extra", "fenced_code", "tables", "sane_lists"],
                        output_format="html5")
    if image:
        frag = _map_images(frag, image)
    if heading_offset:
        def shift(m):
            level = min(int(m.group(1)) + heading_offset, 6)
            if m.group(0).startswith("<h"):
                return "<h%d>" % level
            return "</h%d>" % level
        frag = re.sub(r"</?h([1-6])>", shift, frag)
    frag = sanitize_html(frag)
    return _restore_math(frag, store, formula)


def _map_images(frag: str, image) -> str:
    def replace(match):
        tag = match.group(0)
        attrs = {}
        for a in _ATTR_RE.finditer(tag):
            attrs[a.group(1).lower()] = (a.group(3) or a.group(4) or "").strip()
        src = attrs.get("src", "")
        if not src:
            return tag
        alt = attrs.get("alt", "")
        return image(src, alt) or tag
    return _IMG_RE.sub(replace, frag)
