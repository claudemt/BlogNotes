"""Zhihu folder-backed Markdown articles. Never interpret raw HTML from drafts."""
from __future__ import annotations
import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit
from html import escape

from .content import IMAGE_RE, IMAGE_EXTS

@dataclass(frozen=True)
class Article:
    folder: str
    title: str
    body: str
    images: tuple[Path,...]
    modified_at: float
    fingerprint: str
    issues: tuple[str,...]
    @property
    def slug(self): return self.folder


def scan(root: Path) -> list[Article]:
    if not root.exists(): return []
    return [load(folder) for folder in sorted(root.iterdir()) if folder.is_dir() and not folder.is_symlink() and not folder.name.startswith('.')]


def load(folder: Path) -> Article:
    issues=[]
    p=folder/'文案.md'
    try: body=p.read_text(encoding='utf-8-sig').replace('\r\n','\n')
    except OSError: body='';issues.append('缺少文案.md')
    except UnicodeError: body='';issues.append('文案.md 编码需为 UTF-8')
    candidates={}
    for file in folder.iterdir():
        if file.is_symlink() or not file.is_file():continue
        m=IMAGE_RE.fullmatch(file.name)
        if m: candidates.setdefault(int(m.group(1)),[]).append(file)
        elif file.suffix.lower() in IMAGE_EXTS:issues.append(f'图片需使用数字编号：{file.name}')
    if any(len(v)>1 for v in candidates.values()): issues.append('图片编号重复')
    if candidates and sorted(candidates)!=list(range(1,len(candidates)+1)):
        issues.append('图片编号需从 01 连续排列')
    images=tuple(sorted((v[0] for v in candidates.values()),key=lambda x:int(IMAGE_RE.fullmatch(x.name).group(1))))
    if not body.strip():issues.append('正文不能为空')
    header=re.match(r'^\s*#\s+(.+)\s*$',body.splitlines()[0]) if body.splitlines() else None
    title=header.group(1).strip() if header else folder.name
    if len(title)>100:issues.append('标题过长')
    # Enforce locally referenced pictures are exactly the allowed flattened filenames.
    for raw in re.findall(r'!\[[^\]]*\]\(([^)]+)\)',body):
        name=unquote(raw.split(' ',1)[0].strip('<>'))
        if not (IMAGE_RE.fullmatch(name) and name in {p.name for p in images}):
            issues.append('图片引用无效：'+name[:75])
    h=hashlib.sha256((folder.name+'\0'+body).encode('utf-8'))
    updated=p.stat().st_mtime if p.is_file() else folder.stat().st_mtime
    for file in images:
        st=file.stat();updated=max(updated,st.st_mtime)
        h.update(f'{file.name}\0{st.st_size}\0{st.st_mtime_ns}'.encode())
    return Article(folder.name,title,body,images,updated,h.hexdigest(),tuple(issues))


def markdown_with_images(note: Article) -> str:
    """Include unreferenced numbered images at the end, without mutating the source Markdown."""
    text=note.body
    # First H1 is the article title, not an extra heading in the body.
    text=re.sub(r'^(\s*#\s+[^\n]+)(?:\n|$)', '', text, count=1)
    used={unquote(x.strip('<>').split(' ',1)[0]) for x in re.findall(r'!\[[^\]]*\]\(([^)]+)\)',text)}
    unused=[p.name for p in note.images if p.name not in used]
    if unused: text+='\n\n'+'\n\n'.join(f'![{p}](<{p}>)' for p in unused)+'\n'
    return text


def render(note: Article, image_url=lambda name:name, publisher=False, uploaded=None) -> str:
    """基于 mdkit 渲染管线（Workknacks 配方），叠加知乎发布适配。

    图片白名单严格限定为当前文章文件夹内的编号图片；不解释草稿中的原始 HTML。
    """
    from . import mdkit
    allowed = {p.name for p in note.images}
    def image(src, alt):
        if src not in allowed:
            raise ValueError(f'图片引用不合法：{src[:75]}')
        if publisher:
            media=(uploaded or {}).get(src)
            if not media: raise ValueError(f'图片尚未上传：{src}')
            return uploaded_tag(media, escape(alt or '', quote=True))
        return f'<img src="{escape(image_url(src),quote=True)}" alt="{escape(alt or "",quote=True)}" loading="lazy">'
    html=mdkit.markdown_fragment(
        markdown_with_images(note),
        image=image,
        formula=(lambda tex, mode: formula(tex, mode)) if publisher else None,
        heading_offset=1,
    )
    if publisher:
        html=html.replace('<strong>','<b>').replace('</strong>','</b>').replace('<em>','<i>').replace('</em>','</i>')
        # Zhihu table structure requires tbody without thead, and attrs on table.
        def table_adapter(match):
            inner=re.sub(r'</?(?:thead|tbody)>\s*', '', match.group(1))
            return '<table data-draft-node="block" data-draft-type="table" data-size="normal" data-row-style="normal"><tbody>'+inner+'</tbody></table>'
        html=re.sub(r'<table>(.*?)</table>',table_adapter,html,flags=re.S)
    return html


def formula(tex: str, mode: int) -> str:
    from urllib.parse import quote
    return '<img eeimg="{}" src="//www.zhihu.com/equation?tex={}" alt="{}" />'.format(mode,quote(tex,safe=''),escape(tex,quote=True))


def uploaded_tag(data:dict,caption:str='') -> str:
    url=data.get('media_url',{}).get('primary','')
    if not url.startswith('https://') or not urlsplit(url).hostname.endswith(('.zhimg.com','.zhihu.com')):
        raise ValueError('未取得知乎媒体云图片 URL')
    extra=data.get('extra') or {}
    watermark=(extra.get('watermark_image_url') or {}).get('primary','')
    meta=data.get('media_meta') or {}
    w=int(meta.get('width') or 0);h=int(meta.get('height') or 0)
    attrs={'src':url,'data-caption':caption,'data-size':'normal' if w>=343 else 'small',
           'data-original-src':url,'data-watermark-src':watermark or ''}
    if w and h:
        attrs.update({'data-rawwidth':str(w),'data-rawheight':str(h)})
    if extra.get('watermark_image_key') and data.get('media_key'):
        attrs['data-watermark']='watermark' if extra['watermark_image_key'] != data['media_key'] else 'original'
    return '<img '+' '.join(f'{k}="{escape(str(v),quote=True)}"' for k,v in attrs.items())+' />'
