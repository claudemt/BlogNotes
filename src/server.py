"""Unified local-only FastAPI service for XHS and Zhihu."""
from __future__ import annotations
import logging,os,subprocess,sys,threading,mimetypes,time,json,hashlib
from datetime import datetime,timezone
from pathlib import Path
from contextlib import asynccontextmanager
from urllib.parse import urlsplit
from fastapi import FastAPI,HTTPException,Request,UploadFile,File
from fastapi.responses import FileResponse,JSONResponse,HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from .settings import ROOT,RUNTIME,settings
from . import service,editor,batch_service,workspace,remote,archive
from .publish_queue import queue
from .platforms import xiaohongshu as xhs,zhihu

LOG=logging.getLogger(__name__)
LOGFILE=RUNTIME/'logs'/'blognotes.log';LOGFILE.parent.mkdir(parents=True,exist_ok=True)
logging.basicConfig(filename=LOGFILE,level=logging.INFO,format='%(asctime)s %(levelname)s %(name)s: %(message)s',encoding='utf-8')
STOP=threading.Event()
_BUILD_FILES=('src/server.py','src/web/index.html','src/web/app.js',
              'src/web/style.css','src/platforms/xiaohongshu.py')
def build_fingerprint():
    return '|'.join(hashlib.sha256((ROOT/file).read_bytes()).hexdigest()
                    for file in _BUILD_FILES)
# Deliberately calculated when the process starts, not per request: a server
# launched from an older source revision must never impersonate the new one.
BOOT_FINGERPRINT=build_fingerprint()


def _get(uid):
    return workspace.get(uid)

def _detail(uid):
    return workspace.detail(uid)

def _guard_mutation(uid):
    row,_=_get(uid)
    if row['status']=='pending':raise ValueError('待确认笔记不可修改，请先完成对账')

def _api_exception(exc):
    status=404 if isinstance(exc,KeyError) else 409
    return HTTPException(status_code=status,detail=str(exc))

def _job(name,func):
    try:return {'ok':True,'job':service.jobs.submit(name,func).id}
    except RuntimeError as exc:raise HTTPException(409,str(exc))

def periodic_sync():
    STOP.wait(90)
    while not STOP.is_set():
        try:
            if service.jobs.busy:
                STOP.wait(180);continue
            for manager in workspace.PLATFORMS.values():
                previous=manager.last_sync()
                due=not previous or (datetime.now(timezone.utc)-datetime.fromisoformat(previous)).total_seconds()>=3600*float(settings().get('sync_interval_hours',6))
                records=manager.store.records()
                if not due or not any(x['status']=='published' for x in records):continue
                if manager.online_ready():
                    _job('同步'+manager.label,manager.sync)
                if service.jobs.busy:break
        except Exception:LOG.exception('Auto sync')
        STOP.wait(180)

def periodic_batch():
    STOP.wait(8)
    while not STOP.is_set():
        try:batch_service.tick()
        except Exception:LOG.exception('Queue scheduler')
        STOP.wait(15)

@asynccontextmanager
async def lifespan(app):
    for manager in workspace.PLATFORMS.values():manager.operations.sources()
    STOP.clear();threading.Thread(target=periodic_sync,daemon=True).start()
    threading.Thread(target=periodic_batch,daemon=True,name="publish-queue").start()
    yield
    STOP.set()

app=FastAPI(title='BlogNotes',docs_url=None,redoc_url=None,lifespan=lifespan)
@app.middleware('http')
async def local_only(request:Request,call_next):
    host=request.url.hostname
    if host not in ('127.0.0.1','localhost'):
        return JSONResponse({'detail':'仅本地访问'},status_code=403)
    # Browser origin validation prevents foreign sites from issuing POSTs to localhost.
    if request.method not in ('GET','HEAD','OPTIONS'):
        origin=request.headers.get('origin')
        if origin:
            o=urlsplit(origin)
            if (o.scheme,o.hostname,o.port)!=(request.url.scheme,host,request.url.port):
                return JSONResponse({'detail':'跨站请求已拒绝'},status_code=403)
        elif request.headers.get('sec-fetch-site') not in (None,'same-origin','none'):
            return JSONResponse({'detail':'跨站请求已拒绝'},status_code=403)
    response=await call_next(request)
    if request.url.path == '/' or request.url.path.startswith('/static/'):
        response.headers['Cache-Control']='no-store, max-age=0'
    response.headers['X-Content-Type-Options']='nosniff'
    response.headers['X-Frame-Options']='DENY'
    response.headers['Referrer-Policy']='no-referrer'
    response.headers['Content-Security-Policy']="default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    return response

@app.get('/api/health')
def health():
    return {'ok': True, 'service': 'BlogNotes', 'root': str(ROOT.resolve()),
            'build_id': BOOT_FINGERPRINT, 'ui_revision': hashlib.sha256(BOOT_FINGERPRINT.encode()).hexdigest()[:12]}

@app.get('/')
def home():
    source=(ROOT/'src'/'web'/'index.html').read_text(encoding='utf-8')
    return HTMLResponse(source.replace('__UI_REV__',hashlib.sha256(BOOT_FINGERPRINT.encode()).hexdigest()[:12]),
                        headers={'Cache-Control':'no-store, max-age=0'})
app.mount('/static',StaticFiles(directory=ROOT/'src'/'web'),name='static')

@app.get('/api/notes')
def notes(platform:str='xhs'):
    try: manager=workspace.backend(platform)
    except ValueError:raise HTTPException(400,'未知平台')
    return {'ok':True,'notes':manager.catalog(),'last_synced_at':manager.last_sync()}

@app.get('/api/notes/{uid}')
def note(uid:str):
    try:return {'ok':True,'note':_detail(uid)}
    except (KeyError,ValueError) as exc:raise _api_exception(exc)

@app.get('/api/notes/{uid}/images/{index}')
def image(uid:str,index:int):
    try:
        _,item=_get(uid)
        if index<0 or index>=len(item.images):raise KeyError('图片不存在')
        path=item.images[index]
        return FileResponse(path,media_type=mimetypes.guess_type(path.name)[0] or 'application/octet-stream')
    except (KeyError,ValueError) as exc:raise _api_exception(exc)

@app.get('/api/notes/{uid}/trend')
def trend(uid:str):
    try:return {'ok':True,'points':workspace.snapshot(uid)}
    except (KeyError,ValueError) as exc:raise _api_exception(exc)

@app.get('/api/notes/{uid}/comments')
def comments(uid:str,limit:int=xhs.COMMENT_LIMIT,replies:int=xhs.COMMENT_REPLY_LIMIT):
    # Read-only live fetch on demand, never cached: comments move, and a stale
    # copy presented as current would be worse than no copy.
    try:
        platform=workspace.resolve(uid)
        if platform.key!='xhs':raise ValueError('只有小红书笔记支持读取评论')
        row,_item=platform.get(uid)
        if row['status']!='published' or not row.get('xhs_id'):
            raise ValueError('该笔记尚未关联小红书作品，无法读取评论')
        token=''
        record=remote.get('xhs',row['xhs_id'])
        if record:token=(record.get('metadata') or {}).get('xsec_token','')
        return {'ok':True,'comments':xhs.fetch_comments(row['xhs_id'],token,limit,replies)}
    except (KeyError,ValueError) as exc:raise _api_exception(exc)
    except xhs.PlatformError as exc:raise HTTPException(409,str(exc))

class NewNote(BaseModel):
    platform:str
    title:str
@app.post('/api/notes')
def create(payload:NewNote):
    try:
        editor.new_folder(payload.platform,payload.title)
        items=workspace.backend(payload.platform).catalog()
        row=next(x for x in items if x['folder']==payload.title.strip())
        return {'ok':True,'uid':row['uid']}
    except (OSError,ValueError,StopIteration) as exc:raise _api_exception(exc)

class TextData(BaseModel):
    text:str
    expected_hash:str
@app.put('/api/notes/{uid}/body')
def save_body(uid:str,payload:TextData):
    try:
        _guard_mutation(uid)
        editor.save_text(uid,payload.text,payload.expected_hash)
        return {'ok':True,'note':_detail(uid)}
    except (KeyError,OSError,ValueError) as exc:raise _api_exception(exc)

class RenderData(BaseModel):
    text:str
@app.post('/api/notes/{uid}/render')
def render_preview(uid:str,payload:RenderData):
    """编辑器实时预览：完整渲染当前输入（保留首 H1、不追加未引用图片）。"""
    from html import escape as htmlesc
    from . import mdkit
    try:
        row,item=_get(uid)
        if not hasattr(item,'images'):
            raise ValueError('仅知乎笔记支持 Markdown 预览')
        allowed={p.name for p in item.images}
        names={p.name:i for i,p in enumerate(item.images)}
        def image(src,alt):
            if src not in allowed:
                raise ValueError(f'图片引用不合法：{src[:75]}')
            return f'<img src="/api/notes/{uid}/images/{names[src]}" alt="{htmlesc(alt or "",quote=True)}" loading="lazy">'
        frag=mdkit.markdown_fragment(payload.text,image=image,heading_offset=1)
        return {'ok':True,'html':frag}
    except (KeyError,ValueError) as exc:raise _api_exception(exc)

@app.post('/api/notes/{uid}/images')
async def add_image(uid:str,file:UploadFile=File(...)):
    try:
        data=await file.read(25*1024*1024+1)
        _guard_mutation(uid)
        filename=editor.upload(uid,file.filename or '',data)
        return {'ok':True,'filename':filename}
    except (KeyError,OSError,ValueError) as exc:raise _api_exception(exc)

class ImageAction(BaseModel):
    filename:str
@app.post('/api/notes/{uid}/convert')
def convert(uid:str,payload:ImageAction):
    try:_guard_mutation(uid);return {'ok':True,'filename':editor.convert(uid,payload.filename)}
    except (KeyError,OSError,ValueError) as exc:raise _api_exception(exc)
@app.post('/api/notes/{uid}/remove-image')
def remove_image(uid:str,payload:ImageAction):
    try:_guard_mutation(uid);editor.remove_image(uid,payload.filename);return {'ok':True}
    except (KeyError,OSError,ValueError) as exc:raise _api_exception(exc)

@app.post('/api/notes/{uid}/publish')
def publish(uid:str):
    try:
        rec,item=_get(uid)
        if rec['status']!='draft' or item.issues:raise ValueError('发布状态或内容校验不通过')
        manager=workspace.resolve(uid)
        return _job('发布 · '+item.title,lambda j:manager.publish(uid,j))
    except (KeyError,ValueError) as exc:raise _api_exception(exc)

@app.post('/api/notes/{uid}/reset')
def reset(uid:str):
    try:
        if service.jobs.busy:raise ValueError('后台任务运行中')
        workspace.resolve(uid).reset(uid)
        return {'ok':True}
    except (KeyError,ValueError) as exc:raise _api_exception(exc)

class LinkData(BaseModel):
    platform_id:str
@app.post('/api/notes/{uid}/link')
def link(uid:str,payload:LinkData):
    try:
        _,item=_get(uid)
        manager=workspace.resolve(uid)
        return _job('关联 · '+item.title,lambda j:manager.link(uid,payload.platform_id,j))
    except (KeyError,ValueError) as exc:raise _api_exception(exc)

class PlatformData(BaseModel):
    platform:str='xhs'

@app.post('/api/sync')
@app.post('/api/refresh')
def refresh(payload:PlatformData):
    # Each platform is fully isolated: refresh means rescan its local folders,
    # fetch all owned remote notes, profile where supported, and metrics.
    if payload.platform not in workspace.PLATFORMS:
        raise HTTPException(400,'未知平台')
    def work(job):
        manager=workspace.backend(payload.platform)
        manager.catalog()
        job.report('本地目录已扫描')
        if payload.platform=='xhs':
            probe=xhs.probe_login()
            if probe['state']=='unauthenticated':
                # Only a *positive* logout stops a refresh.
                raise ValueError('小红书登录已失效：'+probe['reason'])
            if probe['state']=='unknown':
                # A busy/absent browser is not a logout; keep reading and say so.
                job.report('登录状态未确认（'+probe['reason']+'），继续读取')
            # Reading needs the browser up. This is an explicit user action, so
            # opening it without a login tab is expected (ensure_browser never
            # opens a second Chrome, and never the login page here).
            try:
                xhs.ensure_browser()
            except Exception as exc:
                raise ValueError('无法启动小红书专用浏览器：'+str(exc)) from exc
            _refresh_xhs_profile(job)
        elif not manager.online_ready():
            raise ValueError('知乎尚未配置数据授权，请查看帮助')
        result=manager.sync(job)
        job.report('刷新完成')
        return result
    return _job('刷新',work)

def _refresh_xhs_profile(job=None):
    try:
        profile=xhs.profile_snapshot()
        service.store.set_meta('xhs_profile',json.dumps(profile,ensure_ascii=False))
        if job:job.report('账号资料已更新')
        return profile
    except Exception as exc:
        if job:job.report('账号资料暂不可用：'+str(exc))
        return None

@app.post('/api/import')
def import_post(payload:dict):
    platform=payload.get('platform');ident=str(payload.get('id') or '').strip()
    if platform not in workspace.PLATFORMS or not ident or not remote.get(platform,ident):
        raise HTTPException(404,'请先刷新作品列表')
    return _job('导入 · '+platform,lambda job:archive.export_one(platform,ident,report=job.report))

@app.post('/api/import-all')
def import_all(payload:PlatformData):
    if payload.platform not in workspace.PLATFORMS:raise HTTPException(400,'未知平台')
    platform=payload.platform
    def work(job):
        rows=remote.records(platform)
        linked={r.get('zhihu_id' if platform=='zhihu' else 'xhs_id') for r in workspace.backend(platform).store.records()}
        remaining=[r for r in rows if r['remote_id'] not in linked]
        total=0;errors=[]
        for r in remaining:
            try:
                archive.export_one(platform,r['remote_id'],report=job.report)
                total+=1
            except Exception as exc:
                errors.append(r['title']+': '+str(exc))
                job.report('导入失败：'+r['title']+' · '+str(exc))
        job.report(f'导入完成 {total}/{len(remaining)}；失败 {len(errors)}')
        if errors:
            raise ValueError('部分导入失败（成功 %d 篇，失败 %d 篇）：%s' % (total,len(errors),'；'.join(errors[:3])))
        return {'total':total}
    return _job('导入全部 · '+platform,work)

@app.post('/api/notes/{uid}/sync')
def sync_note(uid:str):
    try:
        if uid.startswith('remote:'):
            parts=uid.split(':',2)
            if len(parts)!=3 or parts[1] not in workspace.PLATFORMS or not remote.get(parts[1],parts[2]):raise KeyError('笔记不存在')
            manager=workspace.backend(parts[1])
            return _job('刷新作品',lambda job:manager.sync(job))
        manager=workspace.resolve(uid)
        manager.get(uid)
        return _job('刷新 · '+manager.label,lambda job:manager.sync_one(uid,job))
    except (KeyError,ValueError) as exc:raise _api_exception(exc)

class BatchCreate(BaseModel):
    platform:str
    uids:list[str]

@app.get('/api/publish-policy')
def publish_policy(platform:str='xhs'):
    if platform not in ('xhs','zhihu'):raise HTTPException(400,'未知平台')
    until,used,limit,gap=queue.next_allowed(platform)
    return {'ok':True,'quota':{'used':used,'limit':limit,'gap_minutes':gap,'next_allowed_at':until.isoformat(timespec='seconds')}}

@app.get('/api/batches')
def batches(platform:str='xhs'):
    if platform not in ('xhs','zhihu'):raise HTTPException(400,'未知平台')
    return {'ok':True,'batches':batch_service.queue_view(platform)}

@app.post('/api/batches')
def batch_create(payload:BatchCreate):
    try:
        manager=workspace.backend(payload.platform)
        if not payload.uids or len(set(payload.uids))>30:raise ValueError('每批最多30篇')
        for uid in payload.uids:
            if workspace.resolve(uid).key!=manager.key:raise ValueError('平台不匹配')
            row,item=_get(uid)
            if row['status']!='draft' or item.issues:raise ValueError('包含非草稿或校验失败的笔记：'+item.title)
        ident=queue.create(payload.platform,payload.uids)
        batch_service.tick()
        return {'ok':True,'batch_id':ident}
    except (KeyError,ValueError) as exc:raise _api_exception(exc)

@app.post('/api/batches/{batch_id}/{action}')
def batch_action(batch_id:str,action:str):
    try:
        state=queue.set_state(batch_id,action)
        if action=='resume':batch_service.tick()
        return {'ok':True,'status':state}
    except (KeyError,ValueError) as exc:raise _api_exception(exc)

class SkipData(BaseModel):
    uid:str
@app.post('/api/batches/{batch_id}/skip')
def batch_skip(batch_id:str,payload:SkipData):
    try:
        queue.skip_attention(batch_id,payload.uid)
        return {'ok':True}
    except (KeyError,ValueError) as exc:raise _api_exception(exc)

@app.get('/api/tasks/{job_id}')
def job(job_id:str):
    j=service.jobs.get(job_id)
    if not j:raise HTTPException(404,'任务不存在')
    return {'ok':True,'job':j.as_dict()}

@app.get('/api/account')
def account(platform:str='xhs'):
    if platform=='zhihu':return {'ok':True,**zhihu.credential_status(), 'sync_hours':float(settings().get('sync_interval_hours',6))}
    if platform=='xhs':
        raw=service.store.get_meta('xhs_profile')
        try:profile=json.loads(raw) if raw else None
        except ValueError:profile=None
        inv_raw=service.store.get_meta('xhs_inventory')
        try:inventory=json.loads(inv_raw) if inv_raw else None
        except ValueError:inventory=None
        return {'ok':True,'browser_open':xhs.is_browser_open(),
            'last_verified_login':service.store.get_meta('account_login_verified_at') if xhs.is_browser_open() else None,
            'login':{'cached':xhs.login_cached(),'pending':xhs.login_pending()},
            'inventory':inventory,
            'profile':profile}
    raise HTTPException(400,'未知平台')

class Credentials(BaseModel):
    app_key:str=''
    app_secret:str=''
    access_secret:str=''
@app.post('/api/account/zhihu/credentials')
def credentials(payload:Credentials):
    try:return {'ok':True,**zhihu.save_credentials(payload.app_key,payload.app_secret,payload.access_secret)}
    except (ValueError,OSError) as exc:raise _api_exception(exc)

def _mark_xhs_login(job):
    # Record the verified timestamp. This no longer skips the data pull: the
    # old comment warned that profile_snapshot() opened a second tab to www and
    # could bounce to a login page. profile_snapshot() now reads the account id
    # from the creator cookie first and uses a throwaway tab, so syncing inside
    # the same task is safe and is what the user asked for.
    service.store.set_meta('account_login_verified_at', service.utcnow())
    job.report('登录已确认')


def _sync_after_login(job):
    """Pull profile + works + metrics in the same task as the login."""
    _refresh_xhs_profile(job)
    try:
        workspace.backend('xhs').sync(job)
        job.report('账号资料与作品已同步')
    except Exception as exc:
        # Login itself succeeded; a data problem must not be reported as a
        # failed login.
        job.report('登录成功，但数据同步未完成：'+str(exc))


@app.post('/api/account/xhs/login')
def login():
    # A repeated click joins the login already in flight. Starting a second
    # task would launch another browser window and fight over the same profile.
    running=service.jobs.find_running('小红书登录')
    if running is not None:
        return {'ok':True,'job':running.id,'attached':True}
    def work(job):
        probe=xhs.probe_login()
        LOG.info('xhs login: initial probe=%s', probe)
        if probe['state']=='authenticated':
            job.report('当前账号已经登录')
            _mark_xhs_login(job)
            _sync_after_login(job)
            return
        try:
            xhs.ensure_browser(open_login=True)
        except Exception as exc:
            LOG.exception('ensure_browser failed')
            raise ValueError('无法启动小红书专用浏览器：'+str(exc)) from exc
        seq,cancel=xhs.begin_login_session()
        job.report('请在 Chrome 窗口扫码登录')
        last_reason=''
        try:
            # Reference: xiaohongshu-mcp waits on a short ticker for up to 4
            # minutes. 'unknown' (browser busy / SPA still booting) and
            # 'unauthenticated' both simply mean "keep waiting" — only a real
            # timeout ends the loop, so a good scan can never be undone by a
            # transient probe result.
            for i in range(120):
                time.sleep(2)
                if cancel.is_set():
                    job.report('已由新请求接管')
                    return
                state=xhs.probe_login()
                LOG.info('xhs login poll[%d]: %s', i, state)
                if state['state']=='authenticated':
                    nick = state.get('nickname') or '账号'
                    job.report(f'已登录：{nick}')
                    _mark_xhs_login(job)
                    _sync_after_login(job)
                    return
                last_reason = state.get('reason','')
        finally:
            xhs.end_login_session(seq)
        raise ValueError('扫码超时（'+last_reason+'），可重新点「登录」')
    return _job('小红书登录',work)


@app.post('/api/account/xhs/check')
def check():
    def verify(job):
        probe=xhs.probe_login(deep=True)
        if probe['state']=='authenticated':
            _mark_xhs_login(job)
            _sync_after_login(job)
            return
        if probe['state']=='unknown':
            # Cannot tell — do NOT tell the user to scan again. That is what
            # turned a busy browser into an endless re-login loop.
            job.report('状态未知：'+probe['reason']+'，可稍后重试')
            return
        raise ValueError('未登录：'+probe['reason'])
    return _job('检查登录',verify)

@app.post('/api/notes/{uid}/open-folder')
def open_folder(uid:str):
    try:
        platform,folder,_=editor.path_for(uid)
        return _open(folder)
    except (KeyError,ValueError) as exc:raise _api_exception(exc)

@app.post('/api/open-content')
def open_content(payload:PlatformData):
    if payload.platform not in ('xhs','zhihu'):raise HTTPException(400,'未知平台')
    return _open(workspace.backend(payload.platform).folder)

def _open(path:Path):
    if os.name=='nt':os.startfile(str(path.resolve()))
    elif sys.platform=='darwin':subprocess.Popen(['open',str(path.resolve())])
    else:subprocess.Popen(['xdg-open',str(path.resolve())])
    return {'ok':True}

@app.get('/api/diagnostics')
def diagnostics():
    return {'ok':True,'busy':service.jobs.busy,'recent':[j.as_dict() for j in list(service.jobs.jobs.values())[-5:]],'log_path':str(LOGFILE)}

if __name__=='__main__':
    import uvicorn
    uvicorn.run('src.server:app',host='127.0.0.1',port=int(settings()['port']),reload=False)
