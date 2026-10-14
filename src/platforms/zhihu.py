"""Official Zhihu Publisher OpenAPI adapter, fail-closed on unverified responses.

Publishing: https://github.com/zhihu/ZhihuPublisher (OpenAPI spec)
Media: https://github.com/zhihu/zhihu-mediacloud-uploader (optional dependency)
Read-only metrics: https://developer.zhihu.com/api/v1/user/contents (Bearer Access Secret)
This code does not use undocumented write APIs or CAPTCHA bypasses.
"""
from __future__ import annotations
import base64,hashlib,hmac,json,os,time,uuid
from pathlib import Path
from urllib.parse import urlsplit
import httpx
from ..settings import RUNTIME

PUBLISH_URL='https://openapi.zhihu.com/openapi/publish'
CONTENTS_URL='https://developer.zhihu.com/api/v1/user/contents'
class ZhihuError(RuntimeError):pass
class KnownReject(ZhihuError):pass

def credfile():return RUNTIME/'zhihu-credentials.json'
def credentials():
    data={}
    shared=Path.home()/'.zhihu'/'openapi-credentials.json'
    for p in (shared,credfile()):
        if p.is_file():
            try:data.update(json.loads(p.read_text(encoding='utf-8')))
            except (OSError,ValueError):pass
    for k in ('ZHIHU_OPENAPI_APP_KEY','ZHIHU_OPENAPI_APP_SECRET','ZHIHU_ACCESS_SECRET'):
        if os.getenv(k):data[k]=os.environ[k]
    return data

def credential_status():
    d=credentials()
    return {'publish_ready':bool(d.get('ZHIHU_OPENAPI_APP_KEY') and d.get('ZHIHU_OPENAPI_APP_SECRET')),
            'metrics_ready':bool(d.get('ZHIHU_ACCESS_SECRET')),
            'user':d.get('ZHIHU_OPENAPI_APP_KEY') or ''}

def save_credentials(app_key:str,app_secret:str,access_secret:str):
    current={k:v for k,v in credentials().items() if k in ('ZHIHU_OPENAPI_APP_KEY','ZHIHU_OPENAPI_APP_SECRET','ZHIHU_ACCESS_SECRET')}
    for k,v in [('ZHIHU_OPENAPI_APP_KEY',app_key),('ZHIHU_OPENAPI_APP_SECRET',app_secret),('ZHIHU_ACCESS_SECRET',access_secret)]:
        if v.strip():current[k]=v.strip()
    p=credfile();p.parent.mkdir(parents=True,exist_ok=True)
    fd=os.open(p,os.O_CREAT|os.O_WRONLY|os.O_TRUNC,0o600)
    with os.fdopen(fd,'w',encoding='utf-8') as fp:json.dump(current,fp,ensure_ascii=False)
    if os.name!='nt':os.chmod(p,0o600)
    return credential_status()

def signature(key:str,secret:str,timestamp:str,logid:str,extra:str='') -> str:
    source=f'app_key:{key}|ts:{timestamp}|logid:{logid}|extra_info:{extra}'
    return base64.b64encode(hmac.new(secret.encode(),source.encode(),hashlib.sha256).digest()).decode('ascii')

def _upload_image(path:Path) -> dict:
    try:
        from mediacloud_uploader.client import MediaCloudUploader
    except ImportError as exc:
        raise ZhihuError('缺少知乎官方媒体上传组件；按 README 安装 config/zhihu-media-requirements.txt') from exc
    data=credentials()
    u=MediaCloudUploader(app_key=data['ZHIHU_OPENAPI_APP_KEY'],app_secret=data['ZHIHU_OPENAPI_APP_SECRET'])
    response=u.upload_image(file_path=str(path.resolve()),scene_name='article')
    if not response.is_success or not response.media_url or not response.media_url.primary:
        raise ZhihuError(f'媒体云没有确认图片上传成功：{path.name}')
    return {'media_key':response.media_key,'media_meta':{'width':getattr(response.media_meta,'width',0),'height':getattr(response.media_meta,'height',0)},
            'media_url':{'primary':response.media_url.primary},
            'extra':{'watermark_image_key':getattr(response.extra,'watermark_image_key','') if response.extra else '',
                     'watermark_image_url':{'primary':getattr(getattr(response.extra,'watermark_image_url',None),'primary','') if response.extra else ''}}}

def publish(article,images:list[Path],confirmed:bool,report=lambda s:None,transport=None,before_submit=lambda:None):
    """Prepare media first; only dispatch publish after explicit local user confirmation.

    Return an exact official article identifier; 429/nonzero statuses are definitive rejects.
    A timeout/unknown response raises ZhihuError: caller must preserve 'pending'.
    """
    if not confirmed:raise KnownReject('需要明确确认发布')
    d=credentials()
    key=d.get('ZHIHU_OPENAPI_APP_KEY');secret=d.get('ZHIHU_OPENAPI_APP_SECRET')
    if not key or not secret:raise KnownReject('尚未配置知乎 Publisher 授权')
    from ..zhihu_content import render
    uploaded={}
    for p in images:
        report('上传图片 '+p.name)
        uploaded[p.name]=_upload_image(p)
    html=render(article,publisher=True,uploaded=uploaded)
    if not html.strip():raise KnownReject('文章正文为空')
    if len(article.body.strip())<9:raise KnownReject('文章正文不足 9 字')
    body={'type':'article','confirmed':True,'confirm_note':'confirmed by user after local preview',
          'content':{'title':article.title,'html':html,'comment_permission':'all','table_of_contents_enabled':False}}
    ts=str(int(time.time()));logid='travelnotes-'+uuid.uuid4().hex
    headers={'Content-Type':'application/json','X-App-Key':key,'X-Timestamp':ts,'X-Log-Id':logid,
             'X-Extra-Info':'','X-Sign':signature(key,secret,ts,logid,'')}
    # The callback persists the local pending state directly before the non-idempotent request.
    # Never log headers, signature, token, or raw response carrying credentials.
    before_submit()
    try:
        if transport is None:
            with httpx.Client(timeout=35,trust_env=True,follow_redirects=False) as client:
                r=client.post(PUBLISH_URL,headers=headers,json=body)
        else:r=transport.post(PUBLISH_URL,headers=headers,json=body)
    except httpx.HTTPError as exc:raise ZhihuError('知乎发布请求状态未知，请先核实创作列表，禁止重复提交') from exc
    try:payload=r.json()
    except ValueError as exc:raise ZhihuError('知乎返回非 JSON，发布状态未知') from exc
    if not isinstance(payload,dict):raise ZhihuError('知乎响应结构异常，发布状态未知')
    status=payload.get('status');message=str(payload.get('msg') or '')[:240]
    if isinstance(status,bool) or not isinstance(status,(int,float)):
        raise ZhihuError('知乎发布响应缺少有效 status，发布状态未知')
    if status!=0:raise KnownReject(f'知乎拒绝发布（{status}）：{message}')
    if not 200<=r.status_code<300:raise ZhihuError('接口成功状态与 HTTP 状态矛盾，需人工核对')
    result=payload.get('data') or {}
    token=str(result.get('content_token') or '').strip();url=str(result.get('url') or '').strip()
    if not token or urlsplit(url).scheme!='https' or urlsplit(url).hostname!='zhuanlan.zhihu.com' or not urlsplit(url).path.startswith('/p/') or urlsplit(url).path.rsplit('/',1)[-1]!=token:
        raise ZhihuError('发布返回缺少可验证的文章 ID/URL，需人工核对')
    return token,url

def _extract_items(payload):
    if not isinstance(payload,dict):raise ZhihuError('知乎用户内容 API 返回格式异常')
    # Strictly expect published documented PascalCase envelope, do not guess unknown fields.
    if payload.get('Code') not in (None,0) and payload.get('code') not in (None,0):
        raise ZhihuError('知乎内容 API 错误：'+str(payload.get('Message') or payload.get('message') or payload.get('Code')))
    data=payload.get('Data')
    if not isinstance(data,dict) or not isinstance(data.get('Items'),list):
        raise ZhihuError('知乎用户内容 API 缺少 Data.Items')
    return data['Items'],data.get('Paging') or {}

def list_articles(limit=300,transport=None):
    d=credentials();access=d.get('ZHIHU_ACCESS_SECRET')
    if not access:raise ZhihuError('尚未配置知乎数据 Access Secret')
    out=[];offset=0
    def call(client):
        nonlocal offset
        for _ in range((limit+49)//50):
            headers={'Authorization':'Bearer '+access,'X-Request-Timestamp':str(int(time.time())),'Content-Type':'application/json'}
            r=client.get(CONTENTS_URL,headers=headers,params={'ContentType':'article','Limit':min(50,limit-len(out)),'Offset':offset,'SortField':'ts','SortOrder':'desc'})
            r.raise_for_status()
            items,paging=_extract_items(r.json())
            for x in items:
                if x.get('ContentType')!='article':continue
                link=str(x.get('Url') or '')
                if urlsplit(link).scheme!='https' or urlsplit(link).hostname!='zhuanlan.zhihu.com' or not urlsplit(link).path.startswith('/p/'):continue
                token=urlsplit(link).path.rsplit('/',1)[-1]
                out.append({'zhihu_id':token,'zhihu_url':link,'title':x.get('Title'),'published_at':x.get('CreatedAt'),
                            'likes':x.get('LikeCount'),'comments':x.get('CommentCount'),'favorites':x.get('FavoriteCount'),
                            'views':None,'shares':None})
            if paging.get('IsEnd') is True or not items or len(out)>=limit:break
            nxt=paging.get('NextOffset')
            if nxt is None or str(nxt)==str(offset):break
            offset=nxt
    if transport is None:
        with httpx.Client(timeout=25,follow_redirects=False) as client:call(client)
    else:call(transport)
    return out
