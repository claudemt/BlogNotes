"""Explicit loss-aware reverse export; never overwrite authored local content."""
from __future__ import annotations
import io,re,shutil,tempfile
from pathlib import Path
from urllib.parse import urlsplit
import httpx
from PIL import Image,ImageOps,UnidentifiedImageError
from bs4 import BeautifulSoup
from markdownify import markdownify as to_markdown
from .settings import RUNTIME, CONTENT, ZHIHU
from . import remote, service, zhihu_service
from .platforms import xiaohongshu as xhs

class ImportError_(ValueError):pass

ALLOWED_IMAGE_DOMAINS=('xhscdn.com','zhimg.com','xiaohongshu.com')

def safe_image_host(url):
    p=urlsplit(url)
    return p.scheme=='https' and bool(p.hostname) and any(p.hostname==s or p.hostname.endswith('.'+s) for s in ALLOWED_IMAGE_DOMAINS) and not p.username and not p.password

def _download_image(url:str,session:httpx.Client)->bytes:
    if url.startswith('//'):url='https:'+url
    if not safe_image_host(url):raise ImportError_('图片域名不受信任，停止导入')
    with session.stream('GET',url,follow_redirects=False,timeout=25) as response:
        if response.is_redirect:raise ImportError_('图片发生重定向，停止导入')
        response.raise_for_status()
        if not response.headers.get('content-type','').lower().startswith('image/'):
            raise ImportError_('图片请求返回了非图片内容')
        chunks=[];total=0
        for part in response.iter_bytes(128*1024):
            total+=len(part)
            if total>24*1024*1024:raise ImportError_('图片超过 24 MB')
            chunks.append(part)
        return b''.join(chunks)

def _write_png(blob:bytes,target:Path):
    try:
        with Image.open(io.BytesIO(blob)) as im:
            if getattr(im,'n_frames',1)>1:raise ImportError_('暂不导入动图')
            im=ImageOps.exif_transpose(im)
            if im.width*im.height>60_000_000:raise ImportError_('图片分辨率过大')
            im=im.convert('RGBA' if 'A' in im.getbands() else 'RGB')
            im.save(target,'PNG',optimize=True)
    except (OSError,ValueError,UnidentifiedImageError) as exc:
        raise ImportError_('图片格式解析失败') from exc

def _folder_name(title:str,platform:str,identifier:str,root:Path):
    name=re.sub(r'[\\/:*?"<>|\x00-\x1f]','-',title).strip(' .')
    name=(name[:20] if platform=='xhs' else name[:90]).rstrip(' .') or identifier
    candidate=name
    if (root/candidate).exists():candidate=f'{name[:70]}-{identifier[:8]}'
    count=1
    while (root/candidate).exists():
        candidate=f'{name[:65]}-{identifier[:8]}-{count}';count+=1
    return candidate

def _fetch_zhihu_article(url:str,session:httpx.Client)->tuple[str,list[str]]:
    parts=urlsplit(url)
    if parts.scheme!='https' or parts.hostname!='zhuanlan.zhihu.com' or not re.fullmatch(r'/p/\d+',parts.path):
        raise ImportError_('文章链接不符合知乎专栏格式')
    response=session.get(url,follow_redirects=False,timeout=25)
    response.raise_for_status()
    if response.is_redirect:raise ImportError_('知乎文章页发生重定向，请登录后重试')
    soup=BeautifulSoup(response.text,'html.parser')
    area=soup.select_one('.Post-RichTextContainer .RichText, .Post-RichTextContainer, .RichText.ztext, article .RichText')
    if not area or len(area.get_text(' ',strip=True))<9:
        raise ImportError_('知乎文章页未提供完整正文（可能需要登录或平台已调整页面）')
    for tag in area.select('script,style,button,svg'):tag.decompose()
    images=[]
    for img in area.find_all('img'):
        if img.has_attr('eeimg'):
            latex=img.get('alt','')
            img.replace_with('$$'+latex+'$$' if str(img.get('eeimg'))=='2' else '$'+latex+'$')
            continue
        src=img.get('data-original') or img.get('data-actualsrc') or img.get('src') or ''
        if not src or src.startswith('data:'):continue
        if src.startswith('//'):src='https:'+src
        if not safe_image_host(src):raise ImportError_('正文包含不可验证的图片地址，停止导入')
        images.append(src)
        label=img.get('alt') or '图片'
        img.replace_with('![%s](%02d.png)'%(label.replace(']',''),len(images)))
    # markdownify retains the image markers embedded as text; prevent escaping
    # Markdown punctuation introduced intentionally above.
    body=to_markdown(str(area),heading_style='ATX',bullets='-',strip=['span']).strip()
    body=re.sub(r'\\(!\[)',r'\1',body)
    body=re.sub(r'\\(\])',r'\1',body)
    body=re.sub(r'\\(\()',r'\1',body)
    body=re.sub(r'\\(\))',r'\1',body)
    if len(body.strip())<9:raise ImportError_('正文转换为空，停止导入')
    return body,images

def export_one(platform:str,ident:str,report=lambda msg:None)->dict:
    """Platform listing is ownership proof; import uses a temporary directory."""
    if platform not in ('xhs','zhihu'):raise ImportError_('未知平台')
    rec=remote.get(platform,ident)
    if not rec:raise ImportError_('请先刷新，确认该文章存在于本人账号作品列表')
    root=CONTENT if platform=='xhs' else ZHIHU
    root.mkdir(exist_ok=True,parents=True)
    local=(service.store.records() if platform=='xhs' else zhihu_service.zs.records())
    known='xhs_id' if platform=='xhs' else 'zhihu_id'
    matched=next((p for p in local if p.get(known)==ident),None)
    if matched and (root/matched['folder']).is_dir():
        raise ImportError_('本地已有这篇文章，不覆盖原稿')
    name=_folder_name(rec['title'],platform,ident,root)
    temp_root=RUNTIME/'import-staging';temp_root.mkdir(parents=True,exist_ok=True)
    stage=Path(tempfile.mkdtemp(prefix=platform+'-',dir=temp_root))
    destination=None
    committed=False
    try:
        report('读取远端正文')
        if platform=='xhs':
            data=xhs.fetch_full_note(ident,rec['metadata'].get('xsec_token',''))
            if data.get('title'):
                full=data['title'].strip();short=rec['title'].strip()
                if full and short and not (full.startswith(short) or short.startswith(full)):
                    raise ImportError_('远端标题与本人作品索引不一致，停止导入')
            body=data['body']; urls=data.get('images') or []
            if not urls:raise ImportError_('未取得图片，无法完整导出图文笔记')
        else:
            with httpx.Client(headers={'User-Agent':'Mozilla/5.0 BlogNotes/1.0','Accept-Language':'zh-CN,zh;q=0.8'},timeout=25) as client:
                body,urls=_fetch_zhihu_article(rec['url'],client)
        if len(urls)>60:raise ImportError_('图片数量超过导入上限')
        with httpx.Client(headers={'User-Agent':'Mozilla/5.0 BlogNotes/1.0'},timeout=25) as client:
            for n,url in enumerate(urls,1):
                report(f'下载图片 {n}/{len(urls)}')
                blob=_download_image(url,client)
                _write_png(blob,stage/f'{n:02d}.png')
        if platform=='zhihu':
            text=f'# {rec["title"]}\n\n'+body
        else:text=body
        (stage/('文案.md' if platform=='zhihu' else '文案.txt')).write_text(text,encoding='utf-8')
        destination=root/name
        if destination.exists():raise ImportError_('本地已存在同名目录')
        stage.rename(destination)
        if platform=='xhs':
            service.sources()
            row=service.store.note_folder(name)
            if not row:raise ImportError_('导入成功，但索引创建失败，请手动刷新')
            source=service.get(row['uid'])[1]
            service.store.modify(row['uid'],status='published',xhs_id=ident,
               published_at=rec['published_at'],published_hash=source.fingerprint,last_error=None)
            service.store.snapshot(row['uid'],rec['metadata'])
            uid=row['uid']
        else:
            zhihu_service.sources()
            row=next(x for x in zhihu_service.zs.records() if x['folder']==name)
            src=zhihu_service.get(row['uid'])[1]
            zhihu_service.zs.edit(row['uid'],status='published',zhihu_id=ident,zhihu_url=rec['url'],
                 published_at=rec['published_at'],published_hash=src.fingerprint,last_error=None)
            zhihu_service.zs.snapshot(row['uid'],rec['metadata'],'official_user_contents')
            uid=row['uid']
        committed=True
        report('导入完成')
        return {'uid':uid,'folder':name,'images':len(urls)}
    finally:
        if stage.exists():shutil.rmtree(stage,ignore_errors=True)
        # On a failure after the atomic rename, delete only our newly created
        # folder. Existing original drafts are never touched or overwritten.
        if destination is not None and destination.is_dir() and not committed:
            shutil.rmtree(destination,ignore_errors=True)
