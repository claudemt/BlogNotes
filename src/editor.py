"""Atomic local editing, secure uploads, and one-at-a-time non-destructive conversions."""
from __future__ import annotations
import os,re,hashlib,shutil,tempfile
from pathlib import Path
from PIL import Image, ImageOps, UnidentifiedImageError
from .settings import CONTENT,ZHIHU,RUNTIME
from . import workspace
from .content import IMAGE_RE,IMAGE_EXTS

class EditorError(ValueError):pass
ROOTS={'xhs':CONTENT,'zhihu':ZHIHU}
EXTS={'.png','.jpg','.jpeg','.webp','.bmp','.tif','.tiff','.heic','.heif','.gif'}

def root(platform):
    if platform not in ROOTS:raise EditorError('平台不存在')
    return ROOTS[platform]

def path_for(uid):
    manager=workspace.resolve(uid)
    row,item=manager.get(uid)
    platform=manager.key
    target=root(platform)/item.folder
    if not target.resolve().is_relative_to(root(platform).resolve()):raise EditorError('非法目录')
    return platform,target,item

def new_folder(platform,title):
    p=root(platform);title=title.strip()
    if not title or len(title)>95 or any(c in title for c in '\\/:*?"<>|') or title.endswith(('.', ' ')) or title in ('.','..'):
        raise EditorError('笔记名称不合法')
    if platform=='xhs' and len(title)>20:raise EditorError('小红书标题最多建议 20 字')
    if any(x.name.casefold()==title.casefold() for x in p.iterdir()):raise EditorError('笔记已存在')
    dest=p/title;dest.mkdir()
    (dest/('文案.txt' if platform=='xhs' else '文案.md')).write_text('',encoding='utf-8')
    return dest

def save_text(uid,body,expected):
    platform,folder,item=path_for(uid)
    if expected!=item.fingerprint:raise EditorError('内容已在其他位置修改，请刷新后再保存')
    if len(body.encode('utf-8'))>700_000:raise EditorError('正文过长')
    target=folder/('文案.md' if platform=='zhihu' else '文案.txt')
    fd,name=tempfile.mkstemp(dir=folder,prefix='.travelnotes-')
    try:
        with os.fdopen(fd,'w',encoding='utf-8',newline='\n') as fp:fp.write(body)
        os.replace(name,target)
    finally:
        if os.path.exists(name):os.unlink(name)

def _next_index(folder):
    names=[int(m.group(1)) for p in folder.iterdir() if (m:=IMAGE_RE.fullmatch(p.name))]
    return max(names,default=0)+1

def upload(uid,filename,data:bytes):
    platform,folder,item=path_for(uid)
    ext=Path(filename).suffix.lower()
    if ext not in EXTS:raise EditorError('图片格式不支持')
    if not data or len(data)>25*1024*1024:raise EditorError('图片必须小于 25MB')
    try:
        with Image.open(__import__('io').BytesIO(data)) as img:
            img.verify()
    except (UnidentifiedImageError,OSError,ValueError) as exc:raise EditorError('图片无法识别') from exc
    index=_next_index(folder);target=folder/(f'{index:02d}'+ext)
    with target.open('xb') as f:f.write(data)
    return target.name

def convert(uid,filename):
    platform,folder,item=path_for(uid)
    if not IMAGE_RE.fullmatch(filename):raise EditorError('图片名称不合法')
    src=folder/filename
    if not src.is_file() or src.is_symlink():raise EditorError('图片不存在')
    if src.suffix.lower()=='.png':return filename
    dest=src.with_suffix('.png')
    if dest.exists():raise EditorError('同编号 PNG 已存在')
    backup_dir=RUNTIME/'originals'/platform/hashlib.sha256(str(folder).encode()).hexdigest()[:16]
    backup_dir.mkdir(parents=True,exist_ok=True)
    backup=backup_dir/(hashlib.sha256(src.read_bytes()).hexdigest()[:12]+'-'+src.name)
    with Image.open(src) as img:
        if getattr(img,'n_frames',1)>1:raise EditorError('动图不能直接转换为静态 PNG')
        img=ImageOps.exif_transpose(img)
        img=img.convert('RGBA' if img.mode=='RGBA' else 'RGB')
        fd,name=tempfile.mkstemp(suffix='.png',dir=folder)
        os.close(fd)
        try:
            img.save(name,'PNG',optimize=True)
            if dest.exists():raise EditorError('目标已存在')
            os.replace(name,dest)
        finally:
            if os.path.exists(name):os.unlink(name)
    try:shutil.move(str(src),str(backup))
    except OSError as exc:
        dest.unlink(missing_ok=True)
        raise EditorError('无法安全备份原始图片') from exc
    if platform=='zhihu':
        # Keep explicit Markdown image references consistent with a converted basename.
        article=folder/'文案.md'
        data=article.read_text(encoding='utf-8-sig')
        escaped=re.escape(src.name)
        replacement=re.sub(r'(!\[[^\]]*\]\()<?'+escaped+r'>?(\))',lambda m:m.group(1)+dest.name+m.group(2),data)
        if replacement!=data:
            fd,tmp=tempfile.mkstemp(dir=folder,prefix='.travelnotes-')
            with os.fdopen(fd,'w',encoding='utf-8') as f:f.write(replacement)
            os.replace(tmp,article)
    return dest.name

def remove_image(uid,filename):
    platform,folder,item=path_for(uid)
    if not IMAGE_RE.fullmatch(filename):raise EditorError('图片文件名不合法')
    path=folder/filename
    if not path.exists() or path.is_symlink():raise EditorError('图片不存在')
    archive=RUNTIME/'trash'/platform
    archive.mkdir(parents=True,exist_ok=True)
    shutil.move(str(path),str(archive/(hashlib.sha256(str(path).encode()).hexdigest()[:10]+'-'+str(__import__('time').time_ns())+'-'+path.name)))
