"""Local Chrome CDP adapter for the Xiaohongshu creator site.

Selector and endpoint strategy informed by white0dew/XiaohongshuSkills (MIT)
and SingularGuyLeBorn/xhs-analytics (MIT); implementation is independent.
No third-party SaaS; all requests originate from user's signed-in browser.

IMPORTANT: Creator site is an undocumented, changing interface. This module
has never been verified against the user's live authenticated account.
"""
from __future__ import annotations
import json
import logging
import os
import random
import re
import shutil
import socket
import subprocess
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

from ..settings import RUNTIME, settings

LOG = logging.getLogger(__name__)

CREATOR = "https://creator.xiaohongshu.com"
# Set by the inventory collectors on each pass so service.synchronize can tell
# "the account really has nothing" apart from "the API refused to answer".
_INVENTORY_STATUS: dict = {}


def inventory_status() -> dict:
    return dict(_INVENTORY_STATUS)


def _reset_inventory_status() -> None:
    _INVENTORY_STATUS.clear()
    _INVENTORY_STATUS.update({'metrics_ok': False, 'works_ok': False, 'partial': False,
                              'empty': False, 'sources': [], 'reason': ''})


def _note_inventory(*, source: str = '', metrics_ok: bool | None = None,
                    works_ok: bool | None = None, empty: bool | None = None,
                    reason: str = '') -> None:
    _INVENTORY_STATUS.setdefault('metrics_ok', False)
    _INVENTORY_STATUS.setdefault('works_ok', False)
    _INVENTORY_STATUS.setdefault('empty', False)
    _INVENTORY_STATUS.setdefault('sources', [])
    if metrics_ok is not None:
        _INVENTORY_STATUS['metrics_ok'] = metrics_ok
    if works_ok is not None:
        _INVENTORY_STATUS['works_ok'] = works_ok
    if empty is not None:
        _INVENTORY_STATUS['empty'] = empty
    if source and source not in _INVENTORY_STATUS['sources']:
        _INVENTORY_STATUS['sources'].append(source)
    if reason:
        _INVENTORY_STATUS['reason'] = reason
    # "Partial" means: the work list was read, but the interaction numbers were
    # not — e.g. the creator statistics API was refused and we fell back to the
    # www profile page. Never claim a complete dataset in that case.
    #
    # A definitively empty account is the opposite situation: the platform
    # answered "0 works", so there are no interaction numbers to be missing.
    # Reporting that as partial told users with a brand-new account that their
    # metrics had failed to load, when nothing had failed at all.
    _INVENTORY_STATUS['partial'] = bool(
        _INVENTORY_STATUS['works_ok'] and not _INVENTORY_STATUS['metrics_ok']
        and not _INVENTORY_STATUS['empty'])

PUBLISH = CREATOR + "/publish/publish?source=official"
ANALYTICS = CREATOR + "/statistics/data-analysis"
API = "/api/galaxy/creator/datacenter/note/analyze/list"

# www.xiaohongshu.com is where QR login and profile reading happen.
# Reference: xiaohongshu-mcp login.go — navigate to /explore, check the
# .main-container .user .link-wrapper .channel DOM element, and read
# window.__INITIAL_STATE__.user.userInfo for the current user.
WWW_HOME = "https://www.xiaohongshu.com"
WWW_EXPLORE = WWW_HOME + "/explore"
# creator center uses a separate session cookie (galaxy_creator_session_id);
# web_session is the www-site session.
CREATOR_SESSION_COOKIE = "galaxy_creator_session_id"
# The creator cookie jar carries the signed-in account id. Observed in a live
# session (runtime/logs/blognotes.log): x-user-id-creator.xiaohongshu.com =
# 678c92de000000000d00a939. Reading it avoids a navigation just to learn the id.
CREATOR_USER_ID_COOKIE = "x-user-id-creator.xiaohongshu.com"

class PlatformError(RuntimeError):
    pass


# ----- Login-state cache (positive results only) --------------------------
# Reference: white0dew/XiaohongshuSkills tmp/login_status_cache.json — a
# successful check is remembered for a TTL so the UI, a background refresh and
# a second click do not each re-open the browser. A *negative* answer is never
# written: only an explicit logout deletes the entry. Kept in its own JSON file
# rather than service.store because service.py imports this module at module
# scope and importing it back would be circular.
CACHE_FILE = RUNTIME / 'xhs-login-cache.json'
LOGIN_CACHE_SCOPES = ('www', 'creator')
DEFAULT_LOGIN_CACHE_TTL_SECONDS = 12 * 3600

_CACHE_LOCK = threading.Lock()


def _cache_ttl_seconds() -> float:
    raw = settings().get('xhs_login_cache_ttl_hours', DEFAULT_LOGIN_CACHE_TTL_SECONDS / 3600)
    try:
        return max(0.0, float(raw) * 3600)
    except (TypeError, ValueError):
        return float(DEFAULT_LOGIN_CACHE_TTL_SECONDS)


def _cache_key(scope: str) -> str:
    return f'{int(settings()["cdp_port"])}:{scope}'


def _load_cache() -> dict:
    try:
        raw = json.loads(CACHE_FILE.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    entries = raw.get('entries') if isinstance(raw, dict) else None
    return entries if isinstance(entries, dict) else {}


def _save_cache(entries: dict) -> None:
    """Persist the entries, or drop the file when nothing is left to store.

    Removing the file rather than writing ``{"entries": {}}`` keeps one
    invariant crisp: the file exists exactly when a positive result is cached.
    """
    tmp = CACHE_FILE.with_suffix('.tmp')
    try:
        if not entries:
            if CACHE_FILE.exists():
                CACHE_FILE.unlink()
            return
        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps({'entries': entries}, ensure_ascii=False), encoding='utf-8')
        os.replace(tmp, CACHE_FILE)
    except OSError as exc:
        LOG.debug('login cache write skipped: %s', exc)


def _cached_login(scope: str) -> bool | None:
    """True when a *positive* check is still within its TTL; otherwise None.

    None means "recheck" — either nothing is cached or it has expired. A
    negative result is never cached, so this never returns False.
    """
    if _cache_ttl_seconds() <= 0:
        return None
    with _CACHE_LOCK:
        entry = _load_cache().get(_cache_key(scope))
    if not isinstance(entry, dict) or entry.get('logged_in') is not True:
        return None
    checked_at = entry.get('checked_at')
    if not isinstance(checked_at, (int, float)):
        return None
    if time.time() - checked_at > _cache_ttl_seconds():
        return None
    return True


def _set_login_cache(scope: str, logged_in: bool) -> None:
    """Record a positive result; a negative one removes the entry instead."""
    if _cache_ttl_seconds() <= 0:
        return
    key = _cache_key(scope)
    with _CACHE_LOCK:
        entries = _load_cache()
        if logged_in:
            entries[key] = {'logged_in': True, 'checked_at': time.time()}
        else:
            entries.pop(key, None)
        _save_cache(entries)


def _clear_login_cache(scope: str | None = None) -> None:
    with _CACHE_LOCK:
        entries = _load_cache()
        if scope is None:
            entries = {}
        else:
            entries.pop(_cache_key(scope), None)
        _save_cache(entries)


def _sleep(base: float, minimum: float = 0.05) -> None:
    """Jittered sleep so repeated reads never look machine-timed."""
    time.sleep(max(minimum, base * random.uniform(0.75, 1.25)))


# ----- Single-flight pending login session --------------------------------
# Reference: xiaohongshu-mcp login_session.go loginSessions — only one "waiting
# for the user to scan" flow may exist; a newer request cancels the older one.
_LOGIN_LOCK = threading.Lock()
_LOGIN_SESSION: dict = {'seq': 0, 'cancel': None}


def begin_login_session() -> tuple[int, threading.Event]:
    """Cancel any previous pending login and register a new one."""
    with _LOGIN_LOCK:
        previous = _LOGIN_SESSION['cancel']
        _LOGIN_SESSION['seq'] += 1
        seq = _LOGIN_SESSION['seq']
        cancel = threading.Event()
        _LOGIN_SESSION['cancel'] = cancel
    if previous is not None:
        # Set outside the lock so the older waiter can unwind freely.
        previous.set()
    return seq, cancel


def end_login_session(seq: int) -> None:
    with _LOGIN_LOCK:
        if _LOGIN_SESSION['seq'] == seq:
            _LOGIN_SESSION['cancel'] = None


def login_pending() -> bool:
    with _LOGIN_LOCK:
        return _LOGIN_SESSION['cancel'] is not None


def login_cached() -> bool:
    """Whether a recent positive check is still within its TTL."""
    return any(_cached_login(scope) for scope in LOGIN_CACHE_SCOPES)


def _chrome_path() -> str:
    cfg = settings()
    p = cfg.get("chrome_path", "")
    if p and Path(p).is_file():
        return str(p)
    if os.name == "nt":
        roots = [os.environ.get("PROGRAMFILES", ""), os.environ.get("PROGRAMFILES(X86)", ""), os.environ.get("LOCALAPPDATA", "")]
        names = ["Google/Chrome/Application/chrome.exe", "Microsoft/Edge/Application/msedge.exe"]
        for root in roots:
            for name in names:
                path = Path(root) / name
                if path.is_file():
                    return str(path)
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "msedge", "chrome"):
        found = shutil.which(name)
        if found:
            return found
    raise PlatformError("找不到 Chrome 或 Edge。请在 config/config.json 设置 chrome_path。")


def is_browser_open() -> bool:
    with socket.socket() as sock:
        sock.settimeout(0.3)
        return sock.connect_ex(("127.0.0.1", int(settings()["cdp_port"]))) == 0


# ----- Port ownership: never drive a browser that is not ours ----------------
# A bare port check cannot tell our dedicated profile from any other Chrome that
# happens to hold the same debug port. Reference: XiaohongshuSkills
# docs/code-review-2026-03-07.md rates exactly this ("port already open" used as
# the only singleton test, no profile-identity check) as HIGH, because the
# failure mode is not an error — it is silently operating on another account.
# Positive-only, like the login probe: we block only on proof of a foreign
# owner, never on mere inability to tell.
PROFILE_DIR = str(RUNTIME / 'browser-profile')
_OWNER_CACHE: dict = {'port': None, 'verdict': 'unknown', 'checked_at': 0.0}
_OWNER_TTL_SECONDS = 30.0


def _process_command_line(pid: int) -> str:
    """Command line of a running process, or '' when it cannot be read."""
    if os.name == 'nt':
        try:
            completed = subprocess.run(
                ['powershell', '-NoProfile', '-NonInteractive', '-Command',
                 f'(Get-CimInstance Win32_Process -Filter "ProcessId={int(pid)}").CommandLine'],
                capture_output=True, text=True, timeout=8,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        except (OSError, subprocess.SubprocessError):
            return ''
        return (completed.stdout or '').strip()
    try:
        return Path(f'/proc/{int(pid)}/cmdline').read_bytes().replace(b'\0', b' ').decode('utf-8', 'replace')
    except OSError:
        return ''


def _cdp_port_pid(port: int) -> int | None:
    """PID listening on a local TCP port, or None when it cannot be determined."""
    if os.name != 'nt':
        for probe in (['ss', '-ltnpH', f'sport = :{port}'], ['lsof', '-ti', f'TCP:{port}', '-sTCP:LISTEN']):
            try:
                out = subprocess.run(probe, capture_output=True, text=True, timeout=8).stdout
            except (OSError, subprocess.SubprocessError):
                continue
            match = re.search(r'pid=(\d+)|^(\d+)$', out.strip(), re.M)
            if match:
                return int(match.group(1) or match.group(2))
        return None
    try:
        out = subprocess.run(
            ['powershell', '-NoProfile', '-NonInteractive', '-Command',
             f'(Get-NetTCPConnection -LocalPort {int(port)} -State Listen '
             f'-ErrorAction SilentlyContinue).OwningProcess'],
            capture_output=True, text=True, timeout=8,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0)).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    for line in out.split():
        if line.strip().isdigit():
            return int(line.strip())
    return None


def cdp_port_owner(*, refresh: bool = False) -> str:
    """'ours' | 'foreign' | 'unknown' for whoever holds the CDP port.

    'unknown' means we could not read the owning process — callers must treat
    that as permission to proceed, so a locked-down machine or a missing
    PowerShell never bricks the app.
    """
    if not is_browser_open():
        return 'unknown'
    port = int(settings()['cdp_port'])
    now = time.monotonic()
    if (not refresh and _OWNER_CACHE['port'] == port
            and now - _OWNER_CACHE['checked_at'] < _OWNER_TTL_SECONDS):
        return _OWNER_CACHE['verdict']

    verdict = 'unknown'
    pid = _cdp_port_pid(port)
    if pid:
        command_line = _process_command_line(pid)
        if command_line:
            normalized = command_line.replace('\\', '/').lower()
            marker = '--user-data-dir='
            if marker in normalized:
                ours = PROFILE_DIR.replace('\\', '/').lower()
                verdict = 'ours' if ours in normalized else 'foreign'
            LOG.info('cdp_port_owner: port=%s pid=%s verdict=%s', port, pid, verdict)
        else:
            LOG.info('cdp_port_owner: port=%s pid=%s command line unreadable', port, pid)
    _OWNER_CACHE.update(port=port, verdict=verdict, checked_at=now)
    return verdict


def _assert_port_is_ours(action: str) -> None:
    """Refuse to drive a foreign browser rather than acting on the wrong account."""
    if cdp_port_owner() == 'foreign':
        raise PlatformError(
            f'端口 {int(settings()["cdp_port"])} 上运行的不是本程序的浏览器，'
            f'为避免操作到其它账号，已停止{action}。'
            f'请关闭占用该端口的程序，或在 config/config.json 改一个 cdp_port。')


_BROWSER_LOCK = threading.RLock()  # reentrant: ensure_browser calls the tab helper
_LAUNCH_GRACE_SECONDS = 45
_LAUNCH = {'in_flight': False, 'last_attempt': 0.0, 'pid': None}
_LAST_TAB_OPEN = {'at': 0.0}


def _await_cdp_port(proc, timeout: float, launch_started: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if is_browser_open():
            LOG.info('ensure_browser: CDP port up after %.1fs', time.monotonic() - launch_started)
            return True
        if proc is not None and proc.poll() is not None:
            raise PlatformError(
                f'Chrome/Edge 启动后立即退出（exit code={proc.returncode}）。'
                f'请确认 Chrome 已正确安装，或在 config/config.json 中设置 chrome_path。')
        time.sleep(0.3)
    return False


def ensure_browser(open_login: bool = False):
    """Use one persistent, isolated Chrome profile; never launch a second one.

    When open_login=True, focus/open a www.xiaohongshu.com tab — the www site
    shows the QR login modal and, once scanned, the session activates across
    both www and creator via SSO.

    Reference: chrome_launcher.py — a port check is the singleton test, and the
    whole check-then-launch pair is under a process lock. If a launch is already
    in flight we wait for its port instead of starting another Chrome over the
    same --user-data-dir (which used to produce three windows in a row).
    """
    with _BROWSER_LOCK:
        if is_browser_open():
            _assert_port_is_ours('复用该浏览器')
            LOG.info('ensure_browser: CDP already up, reusing existing Chrome (open_login=%s)', open_login)
            if open_login:
                _open_login_tab_if_needed()
            return

        elapsed = time.monotonic() - _LAUNCH['last_attempt']
        if _LAUNCH['in_flight'] and elapsed < _LAUNCH_GRACE_SECONDS:
            LOG.info('ensure_browser: launch already in flight (%.1fs ago); waiting for CDP', elapsed)
            if _await_cdp_port(None, _LAUNCH_GRACE_SECONDS - elapsed, time.monotonic()):
                if open_login:
                    _open_login_tab_if_needed()
                return
            LOG.warning('ensure_browser: in-flight launch never came up; starting a fresh one')

        LOG.info('ensure_browser: launching Chrome (open_login=%s)', open_login)
        startup_url = WWW_EXPLORE if open_login else 'about:blank'
        command = [
            _chrome_path(),
            f'--remote-debugging-port={int(settings()["cdp_port"])}',
            '--remote-debugging-address=127.0.0.1',
            f'--user-data-dir={str(RUNTIME / "browser-profile")}',
            '--no-first-run', '--no-default-browser-check',
            '--start-maximized',
            # Reference: chrome_launcher.py flags to avoid background throttling.
            '--disable-background-timer-throttling',
            '--disable-renderer-backgrounding',
            '--disable-backgrounding-occluded-windows',
            '--disable-background-media-suspend',
            startup_url,
        ]
        LOG.info('ensure_browser: cmd=%s', ' '.join(command))
        kwargs = {'stdin': subprocess.DEVNULL, 'stdout': subprocess.DEVNULL,
                  'stderr': subprocess.DEVNULL, 'close_fds': True}
        if os.name == 'nt':
            kwargs['creationflags'] = (
                getattr(subprocess, 'DETACHED_PROCESS', 0)
                | getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0)
            )
        started = time.monotonic()
        proc = subprocess.Popen(command, **kwargs)
        _LAUNCH.update(in_flight=True, last_attempt=started, pid=proc.pid)
        LOG.info('ensure_browser: Chrome pid=%s', proc.pid)
        try:
            if not _await_cdp_port(proc, 20, started):
                raise PlatformError('Chrome/Edge CDP is not reachable on the configured port.')
        finally:
            _LAUNCH['in_flight'] = False
        try:
            _bring_xhs_tab_to_front()
        except Exception as exc:
            LOG.warning('bring_to_front failed: %s', exc)


def _bring_xhs_tab_to_front():
    """Connect over CDP and activate any Xiaohongshu tab, raising the window."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp(
            f'http://127.0.0.1:{int(settings()["cdp_port"])}', timeout=8000)
        if not browser.contexts:
            return
        context = browser.contexts[0]
        # Prefer a www tab (login), then creator, then anything.
        target = (next((t for t in context.pages if 'www.xiaohongshu.com' in (t.url or '')), None)
                  or next((t for t in context.pages if 'creator.xiaohongshu.com' in (t.url or '')), None)
                  or next((t for t in context.pages if 'xiaohongshu.com' in (t.url or '')), None)
                  or (context.pages[0] if context.pages else None))
        if target is not None:
            try:
                target.bring_to_front()
            except Exception:
                pass


def _open_login_tab_if_needed():
    """Ensure a www.xiaohongshu.com login tab exists and is focused.

    If a www tab is already open, just focus it — NEVER reload, that would
    interrupt an in-progress QR scan. Only when no www tab exists at all do we
    navigate one (reusing an about:blank tab if present). A short debounce
    stops two rapid clicks from opening two login tabs.
    """
    now = time.monotonic()
    if now - _LAST_TAB_OPEN['at'] < 2.0:
        LOG.info('_open_login_tab_if_needed: debounced')
        return
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp(
            f'http://127.0.0.1:{int(settings()["cdp_port"])}', timeout=8000)
        if not browser.contexts:
            return
        context = browser.contexts[0]
        www_tab = next((t for t in context.pages if 'www.xiaohongshu.com' in (t.url or '')), None)
        if www_tab is not None:
            try:
                www_tab.bring_to_front()
            except Exception:
                pass
            _LAST_TAB_OPEN['at'] = now
            return
        # No www tab — open one (reuse an about:blank tab if present).
        blank = next((t for t in context.pages
                      if (t.url or '') in ('about:blank', 'chrome://newtab/', '')), None)
        page = blank or context.new_page()
        _LAST_TAB_OPEN['at'] = now
        try:
            page.goto(WWW_EXPLORE, wait_until='domcontentloaded', timeout=30000)
            page.bring_to_front()
        except Exception as exc:
            LOG.warning('open login tab failed: %s', exc)


def _is_login_url(url: str) -> bool:
    from urllib.parse import urlsplit
    p = urlsplit(url).path.lower()
    return p == '/login' or p.startswith('/login/') or p == '/signin' or p.startswith('/signin/')


# web_session is the primary session cookie set after a successful QR / phone
# login. a1 is a *device identifier* cookie that XHS sets on first visit even
# when NOT logged in — it must NOT be treated as an auth signal. We keep
# reading it only for diagnostics.
_AUTH_COOKIE_NAMES = ('web_session',)
_DEVICE_COOKIE_NAMES = ('a1',)


def _cookies_for(context, url: str) -> dict:
    """Return {name: value} cookies visible to `url` from this context."""
    try:
        items = context.cookies(url)
    except Exception:
        return {}
    out: dict[str, str] = {}
    for c in items or []:
        name = c.get('name')
        value = c.get('value')
        if name and value:
            out[name] = value
    return out


def _auth_cookie_presence(context) -> dict:
    """Probe both creator and www hosts for login cookies without navigating."""
    jar: dict[str, str] = {}
    for origin in ('https://creator.xiaohongshu.com', 'https://www.xiaohongshu.com'):
        try:
            jar.update(_cookies_for(context, origin))
        except Exception:
            continue
    result = {name: bool(jar.get(name)) for name in _AUTH_COOKIE_NAMES}
    result['_all_names'] = sorted(jar.keys())
    # Additive: the creator center has its own session cookie and the creator
    # cookie jar carries the account id. Cookie names contain dots (e.g.
    # x-user-id-creator.xiaohongshu.com) — never filter them with \w.
    result['creator_session'] = bool(jar.get(CREATOR_SESSION_COOKIE))
    result['creator_user_id'] = jar.get(CREATOR_USER_ID_COOKIE, '')
    return result


@contextmanager
def session(*, launch: bool = False):
    """Open a throwaway tab on the dedicated browser.

    Library code never starts Chrome on its own: a background refresh, a unit
    test or a stray poll must not pop a window. Only the user-facing endpoints
    (which call ensure_browser() explicitly) and publish() ever get here with a
    browser already running. publish() passes launch=True because submitting is
    an explicit action that may need the browser up.
    """
    # Import lazily so offline previews work without a browser.
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise PlatformError("缺少 playwright 依赖；请运行 run.bat 安装依赖。") from exc
    if launch:
        ensure_browser()
    elif not is_browser_open():
        raise PlatformError(
            "小红书专用浏览器未运行。请先在「账号 → 登录」打开登录窗口并完成登录。")
    else:
        _assert_port_is_ours('读取数据')
    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp(f'http://127.0.0.1:{int(settings()["cdp_port"])}', timeout=20000)
        context = browser.contexts[0]
        page = context.new_page()
        try:
            yield page
        finally:
            try:
                page.close()
            except Exception:
                pass
            # The Playwright connection ends here; the dedicated Chrome profile persists.


def _ensure_authenticated(page):
    """Ensure creator center is accessible before creator-only operations.

    Reference: xiaohongshu-mcp/xiaohongshu/creator_session.go — the creator
    center uses galaxy_creator_session_id (not web_session). Navigate to the
    creator root; if redirected to /login, prompt re-scan.
    """
    page.goto(CREATOR, wait_until="domcontentloaded", timeout=45000)
    page.wait_for_timeout(2500)
    if _is_login_url(page.url):
        raise PlatformError("小红书登录已失效，请在账号设置中重新扫码。")
    jar = _cookies_for(page.context, CREATOR)
    if not jar.get(CREATOR_SESSION_COOKIE) and not jar.get('web_session'):
        raise PlatformError(
            "小红书登录态缺失（未检测到 galaxy_creator_session_id），请重新扫码。")


def _judge_from_url_and_cookies(url: str, presence: dict) -> dict:
    """Shared three-state decision from a final URL and cookie snapshot."""
    has_session = bool(presence.get('web_session'))
    if _is_login_url(url) and not has_session:
        return {'state': 'unauthenticated',
                'reason': '创作者中心停留在登录页，请在弹出窗口用小红书 App 扫码'}
    if has_session:
        return {'state': 'authenticated',
                'reason': f'已检测到登录 Cookie（web_session={has_session}）'}
    return {'state': 'unknown',
            'reason': f'未检测到 web_session 登录 Cookie（url={url}, cookies={presence.get("_all_names", [])}）'}


def _probe_creator_page(page) -> dict:
    """Read-only three-state auth probe driven by URL redirect + cookies.

    Historically this function fetched the statistics JSON and treated any
    error as "unknown". That endpoint requires x-s/x-t signatures generated
    by the page's own JS; a bare fetch from evaluate() gets rejected by
    risk control and was being misread as "not logged in" right after the
    user finished scanning the QR. We now navigate the *root* creator URL
    (which 302s to /login when unauthenticated), wait for the SPA to settle,
    and confirm with the cookies the browser context actually holds.
    """
    try:
        page.goto(CREATOR, wait_until='domcontentloaded', timeout=30000)
    except Exception as exc:
        return {'state': 'unknown', 'reason': f'导航创作者中心失败：{type(exc).__name__}: {exc}'}
    page.wait_for_timeout(2500)
    presence = _auth_cookie_presence(page.context)
    return _judge_from_url_and_cookies(page.url or '', presence)


def _www_login_state(page) -> dict:
    """Read login state from a www.xiaohongshu.com tab WITHOUT navigating.

    Reference: xiaohongshu-mcp/xiaohongshu/login.go — after QR scan the element
    `.main-container .user .link-wrapper .channel` appears and
    __INITIAL_STATE__.user.userInfo flips from guest to a real account; the
    login modal keyword is from white0dew/XiaohongshuSkills.

    Returns a *tri-state*: {'state': 'authenticated'|'unauthenticated'|'unknown',
    'nickname', 'user_id', 'ready'}. A still-booting SPA reports 'unknown', not
    'unauthenticated' — reading a half-loaded page as "logged out" is what made
    the UI demand a fresh QR scan right after a successful one.
    """
    try:
        signals = page.evaluate("""() => {
            const u = window.__INITIAL_STATE__ && window.__INITIAL_STATE__.user;
            const raw = u && u.userInfo;
            // Vue reactive wrappers: .value OR ._value.
            const info = raw && raw.value !== undefined ? raw.value
                       : (raw && raw._value !== undefined ? raw._value : raw);
            let identity = null;
            if (info && info.guest !== true) {
                const uid = info.userId || info.user_id || info.userid;
                if (uid) identity = {userId: String(uid), nickname: info.nickname || info.nickName || ''};
            }
            const modal = !!document.querySelector(
                '[class*="login-container"], [class*="login-modal"], [class*="loginDialog"]')
                || (document.body && document.body.innerText.includes('登录后推荐更懂你的笔记'));
            return {
                identity,
                channel: !!document.querySelector('.main-container .user .link-wrapper .channel'),
                modal,
                state_defined: !!u,
            };
        }""") or {}
    except Exception as exc:
        LOG.debug('_www_login_state evaluate failed: %s', exc)
        return {'state': 'unknown', 'nickname': '', 'user_id': '', 'ready': False,
                'reason': f'页面状态读取失败：{type(exc).__name__}'}

    identity = signals.get('identity')
    if identity:
        return {'state': 'authenticated', 'nickname': identity.get('nickname', ''),
                'user_id': identity.get('userId', ''), 'ready': True}
    if signals.get('channel'):
        return {'state': 'authenticated', 'nickname': '', 'user_id': '', 'ready': True}
    if signals.get('modal'):
        return {'state': 'unauthenticated', 'nickname': '', 'user_id': '', 'ready': True}
    # No identity, no channel, no modal: the SPA has not settled yet.
    return {'state': 'unknown', 'nickname': '', 'user_id': '', 'ready': bool(signals.get('state_defined')),
            'reason': '页面尚未加载出登录状态'}


def _cached_result(scope_hint: str) -> dict | None:
    """A recently verified positive check, when one is still within its TTL."""
    for scope in (scope_hint, *LOGIN_CACHE_SCOPES):
        if _cached_login(scope):
            return {'state': 'authenticated', 'cached': True, 'scopes': {scope: 'authenticated'},
                    'nickname': '', 'user_id': '',
                    'reason': '使用近期校验过的登录状态（专用浏览器当前未运行）'}
    return None


def probe_login(page=None, *, use_cache: bool = True, deep: bool = False) -> dict:
    """Check login status without disturbing the user's QR-scanning tab.

    Connects to the running Chrome over CDP, finds the www.xiaohongshu.com tab
    and evaluates DOM/state in place. Never navigates, never opens a new tab —
    this is called every few seconds by the login-poll loop.

    A transport problem (port momentarily down, CDP refused) yields 'unknown',
    never 'unauthenticated': the UI must not demand a re-scan because the
    browser was busy. A recently verified positive result short-circuits the
    whole check.
    """
    if page is not None:
        # Legacy call site with an explicit page: navigate to creator root.
        return _probe_creator_page(page)

    if not is_browser_open():
        if use_cache:
            cached = _cached_result('www')
            if cached:
                LOG.info('probe_login: CDP down, using cached positive result')
                return cached
        return {'state': 'unknown', 'reason': '小红书专用浏览器尚未启动',
                'nickname': '', 'user_id': '', 'scopes': {}}

    if cdp_port_owner() == 'foreign':
        # Reading cookies from a stranger's browser would report their account
        # as ours. Refuse, and say why rather than claiming "logged out".
        return {'state': 'unknown',
                'reason': f'端口 {int(settings()["cdp_port"])} 被其它程序占用，无法确认登录状态',
                'nickname': '', 'user_id': '', 'scopes': {}}

    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(
                f'http://127.0.0.1:{int(settings()["cdp_port"])}', timeout=8000)
            if not browser.contexts:
                return {'state': 'unknown', 'reason': '浏览器无可用上下文',
                        'nickname': '', 'user_id': '', 'scopes': {}}
            context = browser.contexts[0]
            pages = list(context.pages)
            www_tab = next((p for p in pages if 'www.xiaohongshu.com' in (p.url or '')), None)
            creator_tab = next((p for p in pages if 'creator.xiaohongshu.com' in (p.url or '')), None)
            LOG.info('probe_login: pages=%d www=%s creator=%s',
                     len(pages),
                     www_tab.url if www_tab else None,
                     creator_tab.url if creator_tab else None)

            presence = _auth_cookie_presence(context)
            scopes: dict[str, str] = {}

            # ---- www scope: DOM/state in place, no navigation -------------
            www_state = {'state': 'unknown', 'nickname': '', 'user_id': ''}
            if www_tab is not None:
                www_state = _www_login_state(www_tab)
                LOG.info('probe_login: www state=%s', www_state)
                scopes['www'] = www_state['state']
                if www_state['state'] == 'authenticated':
                    _set_login_cache('www', True)
                elif www_state['state'] == 'unauthenticated':
                    _clear_login_cache('www')
                # 'unknown' (still booting) deliberately leaves the cache alone.
            elif presence.get('web_session'):
                # www tab missing but its session cookie is held by the context.
                scopes['www'] = 'authenticated'
                _set_login_cache('www', True)

            # ---- creator scope: its own session cookie ---------------------
            if presence.get('creator_session'):
                scopes['creator'] = 'authenticated'
                _set_login_cache('creator', True)
            elif creator_tab is not None and _is_login_url(creator_tab.url or ''):
                scopes['creator'] = 'unauthenticated'
                _clear_login_cache('creator')

            if 'authenticated' in scopes.values():
                nickname = www_state.get('nickname', '')
                user_id = www_state.get('user_id', '') or presence.get('creator_user_id', '')
                return {'state': 'authenticated',
                        'reason': f'已登录（{nickname or user_id or "小红书账号"}）',
                        'nickname': nickname, 'user_id': user_id, 'scopes': scopes}

            if scopes.get('www') == 'unauthenticated' and 'authenticated' not in scopes.values():
                if use_cache:
                    cached = _cached_result('creator')
                    if cached:
                        cached['scopes'] = scopes
                        return cached
                return {'state': 'unauthenticated',
                        'reason': '请在弹出的 Chrome 窗口中用小红书 App 扫码登录',
                        'nickname': '', 'user_id': '', 'scopes': scopes}

            # Everything ambiguous. Only an explicit 校验 asks for the deep probe,
            # and it uses a throwaway tab so the user's QR tab is never navigated.
            if deep:
                probe_page = context.new_page()
                try:
                    deep_state = _probe_creator_page(probe_page)
                finally:
                    try:
                        probe_page.close()
                    except Exception:
                        pass
                deep_state['scopes'] = scopes
                if deep_state.get('state') == 'authenticated':
                    _set_login_cache('creator', True)
                return deep_state

            if use_cache:
                cached = _cached_result('creator')
                if cached:
                    cached['scopes'] = scopes
                    return cached
            return {'state': 'unknown',
                    'reason': www_state.get('reason') or '尚未打开小红书登录页',
                    'nickname': '', 'user_id': '', 'scopes': scopes}
    except Exception as exc:
        LOG.warning('probe_login: CDP problem, treating as unknown: %s', exc)
        if use_cache:
            cached = _cached_result('creator')
            if cached:
                return cached
        return {'state': 'unknown', 'reason': f'{type(exc).__name__}: {exc}',
                'nickname': '', 'user_id': '', 'scopes': {}}


def check_login() -> bool:
    """Compatibility interface used by existing read-only call sites.

    Deliberately ignores the login cache: a read-only CLI check must report what
    the live browser says right now, not a stored guess.
    """
    return probe_login(use_cache=False)['state'] == 'authenticated'


def _select_first(page, selectors: list[str], timeout: int = 12000):
    deadline = time.monotonic() + timeout / 1000
    while time.monotonic() < deadline:
        for css in selectors:
            loc = page.locator(css).first
            try:
                if loc.count() and loc.is_visible():
                    return loc
            except Exception:
                continue
        page.wait_for_timeout(300)
    raise PlatformError("未找到页面输入控件，小红书可能已调整网页结构。")


def _upload(page, paths: list[Path], report):
    try:
        page.locator("div.creator-tab").filter(has_text="上传图文").first.click(timeout=8000)
    except Exception:
        try:
            page.get_by_text("上传图文", exact=True).first.click(timeout=4000)
        except Exception:
            if not page.locator('input[type="file"]').count():
                raise PlatformError("无法打开图文发布页，网页布局可能已改变。")
    page.wait_for_timeout(1000)
    for idx, p in enumerate(paths, start=1):
        loc = page.locator('input[type="file"]').first
        try:
            loc.set_input_files(str(p), timeout=15000)
        except Exception as exc:
            raise PlatformError(f"上传图片 {p.name} 失败：{exc}") from exc
        # Creator currently adds one preview card per uploaded image.
        try:
            page.wait_for_function(
                "n => document.querySelectorAll('.img-preview-area .pr').length >= n",
                arg=idx, timeout=60000
            )
        except Exception as exc:
            raise PlatformError(f"第 {idx} 张图片上传后未检测到预览，不执行发布：{exc}") from exc
        report(f"图片已上传 {idx}/{len(paths)}")
        page.wait_for_timeout(450)


_HASHTAG_RE = re.compile(r'#[^#\s]+')
# The creator editor only turns "#tag" into a real topic when it is typed and
# accepted from a suggestion list; the same characters pasted as body text stay
# inert. Selectors mirror the container the reference implementation targets.
_TOPIC_OPTION_SELECTORS = (
    '#creator-editor-topic-container .item',
    '#creator-editor-topic-container li',
    '[id*="topic-container"] .item',
    '.mention-list .item',
    '.topic-item',
)


def _strip_trailing_topics(body: str) -> str:
    """Drop a trailing line that is nothing but hashtags.

    That line is re-added as real topic chips, so leaving it in the filled text
    would duplicate it. A tag written inside real prose is left alone.
    """
    lines = body.rstrip().splitlines()
    if lines and _HASHTAG_RE.search(lines[-1]) and _HASHTAG_RE.sub('', lines[-1]).strip() == '':
        lines.pop()
    while lines and not lines[-1].strip():
        lines.pop()
    return '\n'.join(lines)


def _caret_to_end(editor) -> None:
    """Focus the editor and collapse the caret after its last character."""
    editor.click()
    editor.evaluate("""el => {
        const range = document.createRange();
        range.selectNodeContents(el);
        range.collapse(false);
        const sel = window.getSelection();
        sel.removeAllRanges();
        sel.addRange(range);
        el.focus();
    }""")


def _wait_topic_option(page, timeout_ms: int = 2500):
    """Return the first visible topic suggestion, or None if none appears."""
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        for css in _TOPIC_OPTION_SELECTORS:
            loc = page.locator(css).first
            try:
                if loc.count() and loc.is_visible():
                    return loc
            except Exception:
                continue
        page.wait_for_timeout(200)
    return None


def _apply_topics(page, editor, topics: list[str], report=lambda msg: None) -> dict:
    """Turn the trailing "#tag" line into real, clickable topics.

    Best effort by design. A tag whose suggestion never appears is left in the
    body as literal text rather than removed or retyped, so a tag is never
    silently lost and a topic failure never sinks the whole publish.
    """
    result = {'attached': [], 'literal': [], 'untouched': []}
    typed_any = False
    for tag in topics:
        try:
            _caret_to_end(editor)
            page.keyboard.type((' ' if typed_any else '\n') + '#' + tag, delay=60)
            typed_any = True
        except Exception as exc:
            LOG.warning('publish: topic %r could not be typed: %s', tag, exc)
            result['untouched'].append(tag)
            continue
        try:
            option = _wait_topic_option(page)
            if option is None:
                # The "#tag" is already sitting in the body; leave it there.
                LOG.info('publish: topic %r had no suggestion, kept as text', tag)
                result['literal'].append(tag)
                continue
            option.click(timeout=4000)
            page.wait_for_timeout(300)
            result['attached'].append(tag)
        except Exception as exc:
            LOG.warning('publish: topic %r not attached: %s', tag, exc)
            result['literal'].append(tag)
    if result['untouched']:
        try:
            _caret_to_end(editor)
            page.keyboard.type((' ' if typed_any else '\n')
                               + ' '.join('#' + t for t in result['untouched']), delay=40)
        except Exception as exc:
            # Leave them recorded as untouched: the report is the only place the
            # caller learns these tags did not make it into the note.
            LOG.warning('publish: fallback text for %s failed: %s', result['untouched'], exc)
        else:
            result['literal'].extend(result['untouched'])
            result['untouched'] = []
    if result['attached'] and result['literal']:
        report(f"已添加 {len(result['attached'])} 个话题，"
               f"{len(result['literal'])} 个未出现在联想列表，已按文字保留")
    elif result['attached']:
        report(f"已添加 {len(result['attached'])} 个话题")
    elif result['literal']:
        report(f"{len(result['literal'])} 个话题未出现在联想列表，已按文字保留")
    if result['untouched']:
        report(f"{len(result['untouched'])} 个话题未能写入，已跳过：{'、'.join(result['untouched'])}")
    return result


def publish(title: str, body: str, paths: list[Path], report=lambda msg: None, before_submit=lambda: None,
            topics: list[str] | None = None) -> str | None:
    """Click publish once; return a possible note ID, *not* a success assertion.

    On exceptions thrown after click the caller MUST treat outcome as unknown.

    `topics` are attached as real topic chips: a "#tag" that is merely part of
    the pasted text is inert, and only becomes a clickable topic when typed so
    the editor's suggestion list offers it. Tags given here are expected to be
    the trailing hashtag line of the body, which is removed before filling and
    re-attached properly; anything that fails to attach is appended as plain
    text so a tag is never silently dropped.
    """
    topics = [t.strip().lstrip('#') for t in (topics or []) if t and t.strip().lstrip('#')]
    with session(launch=True) as page:
        _ensure_authenticated(page)
        report("打开创作者发布页")
        page.goto(PUBLISH, wait_until="domcontentloaded", timeout=45000)
        if _is_login_url(page.url):
            raise PlatformError("创作者中心需要重新登录")
        _upload(page, paths, report)
        _select_first(page, ['div.d-input input', 'input[placeholder*="填写标题"]', 'input[placeholder*="标题"]']).fill(title)
        editor = _select_first(page, ['div.tiptap.ProseMirror', 'div.ProseMirror[contenteditable="true"]', 'div.ql-editor', '[contenteditable="true"]'])
        editor.fill(_strip_trailing_topics(body) if topics else body)
        if topics:
            _apply_topics(page, editor, topics, report)
        report("正文填写完成，检查发布按钮")
        try:
            button = _select_first(page, ['.publish-page-publish-btn button.bg-red', 'button.publishBtn'], timeout=5000)
        except PlatformError:
            button = page.get_by_role('button', name='发布', exact=True).first
            if not button.count() or not button.is_visible():
                raise PlatformError('无法找到发布按钮；网站可能已经更改发布界面')
        if not button.is_enabled():
            raise PlatformError("发布按钮不可用，可能需要验证或者内容未通过页面校验")
        # The callback reserves the quota and persists pending only when the
        # actual non-idempotent click is imminent. Login/form/upload failures
        # are safe preparation failures, not ambiguous submissions.
        before_submit()
        report("提交发布请求（此后任何中断都需要先对账）")
        button.click(timeout=10000)
        page.wait_for_timeout(4800)
        # Matches upstream's link extraction strategy; never infer success from it.
        href = page.evaluate("""() => {
          let a = document.querySelector('a[href*="xiaohongshu.com/explore/"]');
          return a ? a.href : null;
        }""")
        match = re.search(r'/explore/([a-zA-Z0-9]{12,36})', href or '')
        return match.group(1) if match else None


def _epoch_to_iso(x: Any) -> str | None:
    if isinstance(x, (int, float)) and x > 0:
        try:
            val = x / 1000 if x > 10**11 else x
            return datetime.fromtimestamp(val, tz=timezone.utc).isoformat(timespec='seconds')
        except (OverflowError, ValueError, OSError):
            return None
    if isinstance(x, str) and x:
        if x.isdigit():
            return _epoch_to_iso(int(x))
        try:
            dt = datetime.fromisoformat(x.replace('Z', '+00:00'))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone(timedelta(hours=8)))
            return dt.astimezone(timezone.utc).isoformat(timespec='seconds')
        except ValueError:
            return None
    return None


def _safe_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        s = value.replace(',', '').strip()
        if not s or s in ('-', '—'):
            return None
        factor = 10000 if s.endswith('万') else 1
        if factor != 1:
            s = s[:-1]
        try:
            return int(float(s) * factor)
        except ValueError:
            return None
    return None


def _parse_note(row: dict) -> dict:
    rate = row.get('coverClickRate')
    return {
        'xhs_id': str(row.get('id') or ''),
        'title': str(row.get('title') or row.get('display_title') or ''),
        'published_at': _epoch_to_iso(row.get('post_time') or row.get('time')),
        'impressions': _safe_int(row.get('imp_count')),
        'views': _safe_int(row.get('read_count', row.get('view_count'))),
        'likes': _safe_int(row.get('like_count', row.get('likes'))),
        'favorites': _safe_int(row.get('fav_count', row.get('collected_count'))),
        'comments': _safe_int(row.get('comment_count', row.get('comments_count'))),
        'shares': _safe_int(row.get('share_count', row.get('shared_count'))),
        'followers': _safe_int(row.get('increase_fans_count')),
        'click_rate': rate,
        # Reference: XiaohongshuSkills _map_note_infos_to_content_rows reads
        # these from the same analyze/list response we already fetch.
        'view_time_avg': _safe_int(row.get('view_time_avg', row.get('avg_view_time'))),
        'danmaku': _safe_int(row.get('danmaku_count', row.get('danmaku'))),
        '_source': 'creator/content-data',
    }


def _fetch_current(page, number: int, page_size: int) -> dict:
    url = f'{API}?page_num={number}&page_size={page_size}&type=0'
    result = page.evaluate("""async url => {
        try {
          const response = await fetch(url, {credentials:'include', cache:'no-store', headers:{'Accept':'application/json'}});
          return {ok:response.ok, status:response.status, payload:await response.json()};
        } catch(e) { return {ok:false, error:String(e)}; }
    }""", url)
    if not result or not result.get('ok'):
        raise PlatformError(f"统计接口返回错误：{result}")
    payload = result.get('payload', {})
    if isinstance(payload, dict) and (payload.get('success') is False or payload.get('code') not in (None, 0)):
        raise PlatformError('创作者中心拒绝返回数据：'+str(payload.get('msg') or payload.get('message') or '未授权'))
    if not isinstance(payload, dict) or not isinstance(payload.get('data'), dict):
        raise PlatformError('统计接口的数据结构发生变化，已停止同步')
    return payload['data']


def list_statistics(report=lambda msg: None, max_pages: int = 30) -> list[dict]:
    """Retrieve personal creator metrics.

    Reference: cdp_publish.py get_content_data / _capture_content_data_from_page_request.
    Two strategies:
      A. Page-context fetch of the analyze/list API (works when the page's
         signing JS has initialised and attaches x-s/x-t automatically).
      B. Network capture: navigate to the data-analysis page and intercept the
         XHR the page itself fires (always correctly signed); collect every
         paginated response seen while we scroll/trigger loads.
    """
    _reset_inventory_status()
    with session() as page:
        _ensure_authenticated(page)

        # --- Strategy B first: passively capture the page's own signed XHR ---
        captured: list[dict] = []
        capture_seen = {'done': False}

        def _on_response(response):
            try:
                if API not in response.url:
                    return
                if response.status != 200:
                    return
                payload = response.json()
                if not isinstance(payload, dict):
                    return
                data = payload.get('data')
                if not isinstance(data, dict):
                    return
                rows = data.get('note_infos')
                if isinstance(rows, list):
                    captured.append(data)
                    capture_seen['done'] = True
                    LOG.info('list_statistics: captured %d notes from %s',
                             len(rows), response.url[:120])
                else:
                    # A signed 200 that carries no note_infos means either an
                    # account with no notes (the payload then holds only a
                    # success envelope) or a response shape we no longer
                    # understand. Log it: silently dropping this is what made
                    # an empty account look like a failed scrape.
                    LOG.info('list_statistics: HTTP 200 but no note_infos; data keys=%s',
                             sorted(data.keys())[:12])
            except Exception as exc:
                LOG.debug('list_statistics: response parse skipped: %s', exc)

        page.on('response', _on_response)
        page.goto(ANALYTICS, wait_until='domcontentloaded', timeout=45000)
        if _is_login_url(page.url):
            raise PlatformError('登录失效')
        # Give the page time to fire its initial data XHR.
        page.wait_for_timeout(3500)

        result: list[dict] = []
        known: set[str] = set()
        # Several strategies run in sequence and each used to announce the same
        # running total, so an empty account printed "作品 0 篇" twice. Only
        # report when the number actually moves.
        last_reported = [-1]

        def _report_progress(tag: str = '') -> None:
            if len(result) == last_reported[0]:
                return
            last_reported[0] = len(result)
            report(f"作品 {len(result)} 篇{tag}")

        def _absorb(data_list):
            for data in data_list:
                rows = data.get('note_infos', [])
                if not isinstance(rows, list):
                    continue
                for item in rows:
                    note = _parse_note(item)
                    if note['xhs_id'] and note['xhs_id'] not in known:
                        known.add(note['xhs_id'])
                        result.append(note)

        _absorb(captured)
        _report_progress()

        # --- Strategy A: explicit page-context fetch for pagination ---------
        size = 50
        fetch_ok = False
        for number in range(1, max_pages + 1):
            try:
                data = _fetch_current(page, number, size)
                fetch_ok = True
            except Exception as exc:
                LOG.warning('list_statistics: page fetch page=%d failed: %s', number, exc)
                if number == 1 and not result:
                    # Neither capture nor fetch produced anything yet; try scrolling.
                    break
                # Further pages likely fail the same way; stop.
                break
            rows = data.get('note_infos', [])
            if not isinstance(rows, list):
                break
            for item in rows:
                note = _parse_note(item)
                if note['xhs_id'] and note['xhs_id'] not in known:
                    known.add(note['xhs_id'])
                    result.append(note)
            _report_progress()
            total = data.get('total')
            if len(rows) < size or (isinstance(total, int) and number * size >= total):
                break

        # --- Strategy B fallback: scroll to trigger more signed XHRs --------
        if not fetch_ok and not capture_seen.get('done'):
            for _ in range(8):
                page.evaluate('() => window.scrollBy(0, 900)')
                page.wait_for_timeout(1200)
            _absorb(captured)
            _report_progress("（抓包）")

        page.remove_listener('response', _on_response)
        if result:
            _note_inventory(source='creator/statistics', metrics_ok=True)
            return result
        # An HTTP 406 here means the request lacked the page's signature (risk
        # control), NOT that the user is logged out. Report it and let the
        # caller fall back to the www profile page; never raise a login error.
        _note_inventory(source='creator/statistics',
                        reason='统计接口未返回数据（风控，非登录问题）')
        return []

# ----- Read-only ownership, profile and content inventory -----
# Creator note-manager endpoint and www initial-state patterns: see docs/UPDATING.
NOTE_MANAGER = CREATOR + '/new/note-manager?source=official'
POSTED_API_FRAGMENT = '/api/galaxy/v2/creator/note/user/posted'

def _normalize_creator_post(x: dict) -> dict:
    """A listed work belongs to the signed-in creator account, not search results."""
    identifier = str(x.get('id') or '')
    def first(*keys):
        for k in keys:
            if x.get(k) is not None:return x[k]
        return None
    return {'xhs_id':identifier, 'title':str(first('display_title','title') or identifier),
            'published_at':_epoch_to_iso(first('time','post_time')),
            'views':_safe_int(first('view_count','read_count')),
            'likes':_safe_int(first('likes','like_count')),
            'favorites':_safe_int(first('collected_count','fav_count')),
            'comments':_safe_int(first('comments_count','comment_count')),
            'shares':_safe_int(first('shared_count','share_count')),
            'xsec_token':str(first('xsec_token','xsecToken') or ''),
            'content_type':str(x.get('type') or ''),
            '_source':'creator/note-manager'}

_WWW_NOTE_ID_RE = re.compile(r'/(?:explore|discovery/item)/([0-9a-zA-Z]{24})')


def _normalize_www_note(x: dict) -> dict:
    """Normalise a www profile-page note card.

    Deliberately emits NO metric keys (views/likes/favorites/...). Emitting them
    as None would let this degraded source overwrite good creator-center numbers
    in the merge. Only identity + display fields are reported.
    """
    card = x.get('noteCard') if isinstance(x.get('noteCard'), dict) else {}
    cover = card.get('cover') if isinstance(card.get('cover'), dict) else {}
    note_id = str(x.get('id') or x.get('noteId') or '')
    return {'xhs_id': note_id,
            'title': str(card.get('displayTitle') or card.get('title') or note_id),
            'xsec_token': str(x.get('xsecToken') or x.get('xsec_token') or ''),
            'cover': cover.get('urlDefault') or cover.get('urlPre') or '',
            'content_type': str(card.get('type') or ''),
            '_source': 'www/profile'}


def _list_published_from_www(page, user_id: str, max_scrolls: int = 8) -> list[dict]:
    """Fallback inventory: the signed-in user's own profile page.

    Reference: xiaohongshu-mcp/xiaohongshu/user_profile.go reads
    __INITIAL_STATE__.user.notes (an array of per-tab arrays) for the owner's
    works; white0dew/XiaohongshuSkills list_profile_notes scrapes the rendered
    /explore/ links. This path needs no request signature, which is why it works
    when the creator statistics API answers 406.
    """
    if not user_id:
        return []
    page.goto(f'{WWW_HOME}/user/profile/{user_id}', wait_until='domcontentloaded', timeout=45000)
    if _is_login_url(page.url or ''):
        raise PlatformError('小红书网页版未登录，无法读取本人作品列表')
    received: list[dict] = []
    seen: set[str] = set()

    def absorb(items):
        for item in items:
            if not isinstance(item, dict):
                continue
            if item.get('modelType') and item['modelType'] != 'note':
                continue
            note = _normalize_www_note(item)
            if note['xhs_id'] and note['xhs_id'] not in seen:
                seen.add(note['xhs_id'])
                received.append(note)

    for _ in range(12):
        page.wait_for_timeout(1000)
        state = page.evaluate("""() => {
            const u = window.__INITIAL_STATE__ && window.__INITIAL_STATE__.user;
            if (!u) return null;
            const raw = u.notes;
            const data = raw && raw.value !== undefined ? raw.value
                       : (raw && raw._value !== undefined ? raw._value : raw);
            if (!data) return null;
            const groups = data.notes;
            if (!Array.isArray(groups)) return null;
            const index = typeof data.index === 'number' ? data.index : 0;
            const group = Array.isArray(groups[index]) ? groups[index]
                        : (Array.isArray(groups[0]) ? groups[0] : []);
            return group;
        }""")
        if state:
            absorb(state)
            break
    if not received:
        # State absent: scrape the rendered cards instead.
        for _ in range(max_scrolls):
            items = page.evaluate("""() => {
                const out = [];
                document.querySelectorAll("a[href*='/explore/'], a[href*='/discovery/item/']").forEach(a => {
                    const href = a.getAttribute('href') || '';
                    const img = a.querySelector('img');
                    const titleEl = a.querySelector("[class*='title']");
                    const alt = img ? (img.getAttribute('alt') || '') : '';
                    out.push({href,
                              title: (titleEl && titleEl.textContent || '').trim() || alt,
                              cover: img ? (img.getAttribute('src') || '') : ''});
                });
                return out;
            }""") or []
            before = len(received)
            for item in items:
                href = item.get('href') or ''
                match = _WWW_NOTE_ID_RE.search(href)
                if not match:
                    continue
                note_id = match.group(1)
                if note_id in seen:
                    continue
                seen.add(note_id)
                received.append({'xhs_id': note_id,
                                 'title': (item.get('title') or note_id).strip(),
                                 'xsec_token': _xsec_token_from_href(href),
                                 'cover': item.get('cover') or '',
                                 'content_type': '', '_source': 'www/dom'})
            if len(received) == before:
                break
            page.evaluate('() => window.scrollTo(0, document.body.scrollHeight)')
            page.wait_for_timeout(900)
    return received


def _xsec_token_from_href(href: str) -> str:
    try:
        from urllib.parse import urlsplit, parse_qs
        return (parse_qs(urlsplit(href).query).get('xsec_token') or [''])[0]
    except Exception:
        return ''


def list_published(max_scrolls: int=35) -> list[dict]:
    """Read own published works.

    Strategy 1: capture the creator note-manager XHR (rich fields).
    Strategy 2: fall back to the signed-in www profile page, which needs no
    request signature. The fallback yields no interaction metrics, so it is
    reported as a partial dataset rather than silently mixed with real numbers.
    """
    with session() as page:
        _ensure_authenticated(page)
        received=[];seen=set();responded=False;last_page=None
        def handle(response):
            nonlocal responded,last_page
            if POSTED_API_FRAGMENT not in response.url:return
            try:
                payload=response.json()
                data=payload.get('data') if isinstance(payload,dict) else None
                if not isinstance(data,dict) or not isinstance(data.get('notes'),list):return
                responded=True
                last_page=data.get('page')
                for item in data['notes']:
                    p=_normalize_creator_post(item)
                    if p['xhs_id'] and p['xhs_id'] not in seen:
                        seen.add(p['xhs_id']);received.append(p)
            except (ValueError,TypeError,KeyError):return
        page.on('response',handle)
        page.goto(NOTE_MANAGER,wait_until='domcontentloaded',timeout=45000)
        if _is_login_url(page.url):
            page.remove_listener('response',handle)
            raise PlatformError('创作者中心未登录')
        try:page.wait_for_timeout(2200)
        except Exception:pass
        stagnant=0
        for _ in range(max_scrolls):
            if last_page == -1:break
            if responded and stagnant>=4:break
            count=len(received)
            page.evaluate('''() => {
                const s=document.querySelector('.note-list,.creator-note-list,[class*="scroll-container"]');
                if(s && s.scrollHeight>s.clientHeight)s.scrollTop+=950;
                else window.scrollBy(0,950);
            }''')
            page.wait_for_timeout(800)
            stagnant=stagnant+1 if count==len(received) else 0
        page.remove_listener('response',handle)
        if responded and received:
            _note_inventory(source='creator/note-manager', works_ok=True)
            return received

        # ---- Fallback: the owner's www profile page ----------------------
        user_id = _creator_user_id(page.context) if page.context else ''
        if not user_id:
            state = _www_login_state(page) if page.url and 'www.xiaohongshu.com' in page.url else {}
            user_id = (state or {}).get('user_id', '')
        try:
            fallback = _list_published_from_www(page, user_id)
        except PlatformError:
            raise
        except Exception as exc:
            LOG.warning('list_published: www fallback failed: %s', exc)
            fallback = []
        if fallback:
            _note_inventory(source='www/profile', works_ok=True,
                            reason='作品取自个人主页，无互动指标')
            return fallback
        if responded:
            # The creator list answered, and it answered "no works" — both the
            # note-manager and the www profile agree. That is a complete and
            # correct dataset (zero works), not a degraded one.
            _note_inventory(source='creator/note-manager', works_ok=True, empty=True,
                            reason='暂无已发布作品')
            return received
        raise PlatformError(
            '作品列表接口未返回可解析数据；请检查创作者中心是否已登录或页面是否改版')

def _profile_from_state(raw:dict)->dict:
    """Normalize the *account-owner* user object, never a suggested profile."""
    if not isinstance(raw,dict):return {}
    info=raw.get('profile') or {}
    if not info.get('nickname') and not info.get('user_id'):return {}
    return {'user_id':str(info.get('user_id') or ''),
            'nickname':str(info.get('nickname') or ''),
            'following':_safe_int(info.get('following')),
            'followers':_safe_int(info.get('followers')),
            'liked_collected':_safe_int(info.get('liked_collected')),
            'profile_url':str(raw.get('url') or ''),
            'captured_at':datetime.now(timezone.utc).isoformat(timespec='seconds')}

def _creator_user_id(context) -> str:
    """The signed-in account id straight from the creator cookie jar.

    Zero navigation, so it cannot disturb the QR tab and cannot be confused
    with a suggested profile. Reference: the creator cookie jar observed in
    runtime/logs/blognotes.log carried x-user-id-creator.xiaohongshu.com.
    """
    try:
        jar = _cookies_for(context, 'https://creator.xiaohongshu.com')
    except Exception:
        return ''
    for name in (CREATOR_USER_ID_COOKIE, 'x-user-id-creator.xiaohongshu.com', 'customerClientId'):
        value = jar.get(name)
        if value and re.fullmatch(r'[0-9a-f]{16,32}', value):
            return value
    return ''


def profile_snapshot()->dict:
    """Read the signed-in user's profile.

    Reference: xiaohongshu-mcp/xiaohongshu/login.go (CurrentUser) and
    user_profile.go (extractUserProfileData); field names and the DOM fallback
    lists come from white0dew/XiaohongshuSkills get_profile_snapshot.

    Flow:
      1. Identity — the creator cookie first (no navigation), and only then a
         www/explore read of __INITIAL_STATE__.user.userInfo, unwrapping both
         Vue wrappers (.value and ._value).
      2. Stats — /user/profile/{userId}, wait for userPageData, read
         basicInfo + interactions; fall back to reading the rendered DOM.
    A throwaway tab is used (session() closes it in finally), so the user's
    login tab is never disturbed. Identity without stats is returned as a
    partial result rather than an error.
    """
    with session() as page:
        # ---- Step 1: identity ---------------------------------------------
        user_id = _creator_user_id(page.context) if page.context else ''
        identity = {'userId': user_id, 'nickname': '', 'redId': '', 'avatar': ''}
        if user_id:
            LOG.info('profile_snapshot: user_id from creator cookie=%s', user_id)

        if not user_id:
            page.goto(WWW_EXPLORE, wait_until='domcontentloaded', timeout=45000)
            for _ in range(12):
                page.wait_for_timeout(1000)
                found = page.evaluate("""() => {
                    const u = window.__INITIAL_STATE__ && window.__INITIAL_STATE__.user;
                    if (!u) return null;
                    const raw = u.userInfo;
                    const info = raw && raw.value !== undefined ? raw.value
                               : (raw && raw._value !== undefined ? raw._value : raw);
                    if (!info || info.guest === true) return null;
                    const uid = info.userId || info.user_id || info.userid;
                    if (!uid) return null;
                    return {
                        userId: String(uid),
                        nickname: info.nickname || info.nickName || '',
                        redId: info.redId || info.red_id || '',
                        avatar: info.avatar || '',
                    };
                }""")
                if found and found.get('userId'):
                    identity.update(found)
                    break
        if not identity.get('userId'):
            raise PlatformError(
                '未能确认小红书登录账号；请确认已在弹出窗口完成扫码登录')
        LOG.info('profile_snapshot: identity=%s', identity)

        # ---- Step 2: profile page → fans / follows / likes ----------------
        profile_url = f"{WWW_HOME}/user/profile/{identity['userId']}"
        page.goto(profile_url, wait_until='domcontentloaded', timeout=45000)
        detail = None
        for _ in range(12):
            page.wait_for_timeout(1000)
            detail = page.evaluate("""() => {
                const u = window.__INITIAL_STATE__ && window.__INITIAL_STATE__.user;
                if (!u) return null;
                const raw = u.userPageData;
                const data = raw && raw.value !== undefined ? raw.value
                           : (raw && raw._value !== undefined ? raw._value : raw);
                if (!data) return null;
                const basic = data.basicInfo || {};
                // interactions is an array like [{name:'关注',count:N},{name:'粉丝',...},{name:'获赞与收藏',...}]
                const interactions = Array.isArray(data.interactions) ? data.interactions : [];
                const byName = {};
                for (const it of interactions) {
                    const name = (it.name || it.type || '').toString().trim();
                    if (name) byName[name] = it.count != null ? it.count : it.value;
                }
                return {
                    nickname: basic.nickname || '',
                    desc: basic.desc || basic.description || '',
                    fans: basic.fans != null ? basic.fans : (byName['粉丝'] ?? byName['fans'] ?? null),
                    follows: basic.follows != null ? basic.follows : (byName['关注'] ?? byName['follows'] ?? null),
                    interaction: basic.interaction != null ? basic.interaction
                                : (byName['获赞与收藏'] ?? byName['获赞和收藏'] ?? byName['interaction'] ?? null),
                    redId: basic.redId || basic.red_id || '',
                };
            }""")
            if detail:
                break
        source = 'www/userPageData'
        if not detail:
            # State missing (SPA changed / not settled): read the rendered DOM.
            detail = page.evaluate("""() => {
                const text = el => el ? (el.textContent || '').trim() : '';
                const pick = sels => {
                    for (const s of sels) {
                        const el = document.querySelector(s);
                        if (el && text(el)) return text(el);
                    }
                    return '';
                };
                const stats = {};
                document.querySelectorAll('[class*="fans"],[class*="follow"],[class*="like"],[class*="count"]')
                    .forEach(el => {
                        const t = text(el);
                        const m = t.match(/(关注|粉丝|获赞与收藏|获赞和收藏)\\s*([\\d.]+\\s*万?)/);
                        if (m) stats[m[1]] = m[2];
                    });
                return {
                    nickname: pick(['h1', '[class*="name"]', '[class*="nickname"]', '[class*="user-name"]']),
                    desc: pick(['[class*="desc"]', '[class*="bio"]', '[class*="signature"]', '[class*="intro"]']),
                    fans: stats['粉丝'] ?? null,
                    follows: stats['关注'] ?? null,
                    interaction: stats['获赞与收藏'] ?? stats['获赞和收藏'] ?? null,
                    redId: '',
                };
            }""")
            source = 'www/dom'
        LOG.info('profile_snapshot: detail(%s)=%s', source, detail)

        if not detail:
            return {
                'user_id': identity['userId'],
                'nickname': identity.get('nickname', ''),
                'red_id': identity.get('redId', ''),
                'avatar': identity.get('avatar', ''),
                'desc': '', 'following': None, 'followers': None, 'liked_collected': None,
                'profile_url': profile_url, 'source': source, 'partial': True,
                'captured_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
            }

        following = _safe_int(detail.get('follows'))
        followers = _safe_int(detail.get('fans'))
        liked = _safe_int(detail.get('interaction'))
        return {
            'user_id': identity['userId'],
            'nickname': detail.get('nickname') or identity.get('nickname') or '',
            'red_id': identity.get('redId') or detail.get('redId') or '',
            'avatar': identity.get('avatar', ''),
            'desc': detail.get('desc', ''),
            'following': following,
            'followers': followers,
            'liked_collected': liked,
            'profile_url': profile_url,
            'source': source,
            'partial': following is None or followers is None or liked is None,
            'captured_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
        }

def fetch_full_note(note_id:str,xsec_token:str='')->dict:
    """Read only a note in the authenticated user's own published inventory."""
    if not re.fullmatch(r'[a-zA-Z0-9]{12,36}',note_id):raise PlatformError('笔记 ID 不合法')
    from urllib.parse import urlencode
    q=('?'+urlencode({'xsec_token':xsec_token,'xsec_source':'pc_creatormng'})) if xsec_token else ''
    with session() as page:
        page.goto('https://www.xiaohongshu.com/explore/'+note_id+q,wait_until='domcontentloaded',timeout=45000)
        page.wait_for_timeout(1600)
        result=page.evaluate('''id => {
          const s=window.__INITIAL_STATE__||{};
          const map=s.note?.noteDetailMap||{};
          let val=map[id];val=(val && '_value' in val)?val._value:val;
          const note=val?.note;
          if(!note)return null;
          const images=(note.imageList||[]).map(v=>v.urlDefault||v.url||
             (v.infoList||[]).find(t=>t.imageScene==='WB_DFT')?.url||'').filter(Boolean);
          return {title:note.title||'',body:note.desc||'',images,type:note.type||'',
                  user_id:note.user?.userId||''};
        }''',note_id)
        if not isinstance(result,dict) or not isinstance(result.get('body'),str):
            raise PlatformError('未能读取笔记全文；可能需要网页端登录或作品已不可见')
        if not result['body'].strip():raise PlatformError('正文为空，停止导入以防丢失原文')
        if result.get('type') and result['type'] not in ('normal','image','image_text','multi_image'):
            raise PlatformError('此作品可能为视频，当前只支持图文反向导入')
        return result


COMMENT_LIMIT = 20        # top-level comments to keep, matching the reference default
COMMENT_REPLY_LIMIT = 10  # replies kept per comment
_COMMENT_SCROLL_MAX = 12


def _comments_from_state(page, note_id: str) -> dict | None:
    """Read the comment tree the page already holds, or None if it is empty.

    Returned raw; timestamps are epoch milliseconds and are normalised in
    Python so the page script stays a plain data extractor.
    """
    return page.evaluate("""id => {
      const unwrap = v => (v && typeof v === 'object' && '_value' in v) ? v._value : v;
      const state = window.__INITIAL_STATE__ || {};
      const map = unwrap(state.note && state.note.noteDetailMap) || {};
      const entry = unwrap(map[id]);
      const box = unwrap(entry && entry.comments);
      if (!box || !Array.isArray(box.list)) return null;
      const who = u => ({ user_id: (u && u.userId) || '', nickname: (u && u.nickname) || '' });
      const one = c => ({
        id: c.id || '', content: c.content || '', user: who(c.user),
        like_count: c.likeCount || 0, created_at: c.createTime || null,
        replies: (c.subComments || []).map(r => ({
          id: r.id || '', content: r.content || '', user: who(r.user),
          like_count: r.likeCount || 0, created_at: r.createTime || null })),
      });
      return { list: box.list.map(one), has_more: !!box.hasMore };
    }""", note_id)


def _comments_from_dom(page, limit: int, replies: int) -> list[dict]:
    """Last-resort scrape of the rendered comment list.

    The reference implementation drives the same DOM nodes (`.parent-comment`
    for a floor, `.comment-item` for the comments inside it), so these
    selectors are known-good on today's layout — but a scrape carries no ids
    and no timestamps, and is reported as partial by the caller.
    """
    return page.evaluate("""caps => {
      const text = el => el ? (el.textContent || '').trim() : '';
      const pick = (root, sels) => {
        for (const s of sels) { const e = root.querySelector(s); if (e) return e; }
        return null;
      };
      const count = el => parseInt(
        (text(pick(el, ['.like .count', '.like-wrapper .count', '.count'])) || '0')
          .replace(/\\D/g, ''), 10) || 0;
      const body = el => text(pick(el, ['.content .note-text', '.content', '.note-text']));
      const name = el => text(pick(el, ['.author .name', '.name', '.nickname']));
      return [...document.querySelectorAll('.parent-comment')].slice(0, caps.limit).map(el => ({
        id: el.id ? el.id.replace(/^comment-/, '') : '',
        content: body(el), user: { user_id: '', nickname: name(el) },
        like_count: count(el), created_at: null,
        // The floor's own comment is the first .comment-item; replies follow.
        replies: [...el.querySelectorAll('.comment-item')].slice(1, caps.replies + 1).map(r => ({
          id: '', content: body(r), user: { user_id: '', nickname: name(r) },
          like_count: count(r), created_at: null })),
      }));
    }""", {'limit': limit, 'replies': replies})


def _scroll_comments(page) -> None:
    """Ask the page for the next page of comments (it fetches on scroll)."""
    page.evaluate("""() => {
      const items = document.querySelectorAll('.parent-comment, .comment-item');
      if (items.length) items[items.length - 1].scrollIntoView({ block: 'end' });
      const box = document.querySelector('.note-scroller, .comments-container');
      if (box) box.scrollTop = box.scrollHeight;
      else window.scrollTo(0, document.body.scrollHeight);
    }""")


def _normalize_comment(raw: dict) -> dict:
    return {
        'id': str(raw.get('id') or ''),
        'content': str(raw.get('content') or '').strip(),
        'user': {'user_id': str((raw.get('user') or {}).get('user_id') or ''),
                 'nickname': str((raw.get('user') or {}).get('nickname') or '')},
        'like_count': _safe_int(raw.get('like_count')) or 0,
        'created_at': _epoch_to_iso(raw.get('created_at')),
        'replies': [_normalize_comment(r) for r in (raw.get('replies') or [])],
    }


def fetch_comments(note_id: str, xsec_token: str = '', limit: int = COMMENT_LIMIT,
                   replies: int = COMMENT_REPLY_LIMIT, report=lambda msg: None) -> dict:
    """Read a note's comments, read-only, in the authenticated user's session.

    Prefers the comment tree the page already holds (`__INITIAL_STATE__`),
    scrolling to pull further pages, and only falls back to scraping the
    rendered list when that tree is empty. A scrape carries no ids and no
    timestamps, so it is returned with `partial: True` rather than passed off
    as the real thing. Fewer comments than exist is normal and also partial.
    """
    if not re.fullmatch(r'[a-zA-Z0-9]{12,36}', note_id):
        raise PlatformError('笔记 ID 不合法')
    limit = max(1, int(limit))
    replies = max(0, int(replies))
    from urllib.parse import urlencode
    q = ('?' + urlencode({'xsec_token': xsec_token, 'xsec_source': 'pc_feed'})) if xsec_token else ''
    with session() as page:
        report('打开笔记页读取评论')
        page.goto('https://www.xiaohongshu.com/explore/' + note_id + q,
                  wait_until='domcontentloaded', timeout=45000)
        page.wait_for_timeout(2000)
        raw = None
        has_more = False
        for attempt in range(_COMMENT_SCROLL_MAX):
            current = _comments_from_state(page, note_id)
            if current is not None:
                raw, has_more = current['list'], current['has_more']
                if len(raw) >= limit or not has_more:
                    break
            elif attempt >= 1:
                # Still nothing one extra wait later: this page carries no
                # comment tree at all, so stop scrolling and try the DOM.
                break
            _scroll_comments(page)
            page.wait_for_timeout(900)
        partial = False
        if not raw:
            scraped = _comments_from_dom(page, limit, replies)
            if not scraped:
                return {'note_id': note_id, 'comments': [], 'count': 0,
                        'has_more': False, 'partial': False}
            raw, partial, has_more = scraped, True, False
            LOG.info('fetch_comments: state empty for %s, scraped %d from DOM',
                     note_id, len(scraped))
        comments = [_normalize_comment(c) for c in raw[:limit]]
        for comment in comments:
            comment['replies'] = comment['replies'][:replies]
        # Truncating a list the page says continues is a partial read, and so is
        # hitting the limit with more still available.
        if len(raw) > limit or has_more:
            partial = True
        return {'note_id': note_id, 'comments': comments, 'count': len(comments),
                'has_more': bool(has_more), 'partial': partial}
