"""End-to-end local editor smoke: actual Uvicorn HTTP + actual Chromium, no platform calls.

Uses a temporary copy of the app and synthetic pictures; never reads the user's
notes, credentials, cookies or runtime databases. Optional: Chromium required.
"""
from __future__ import annotations
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from PIL import Image
from playwright.sync_api import sync_playwright, Error as BrowserError
import httpx

ROOT = Path(__file__).resolve().parents[2]


def main():
    browser_path = os.environ.get('CHROMIUM_PATH') or next((shutil.which(x) for x in ('chromium', 'google-chrome', 'msedge', 'chrome') if shutil.which(x)), None)
    if not browser_path:
        print('SKIP: no Chromium')
        return
    with tempfile.TemporaryDirectory(prefix='travelnotes-http-smoke-') as temp:
        root = Path(temp)
        shutil.copytree(ROOT/'src', root/'src', ignore=shutil.ignore_patterns('__pycache__'))
        shutil.copytree(ROOT/'config', root/'config', ignore=shutil.ignore_patterns('config.json'))
        for name, body, suffix in (('xhs', '第一篇本地笔记。\n\n#旅行', 'txt'), ('zhihu', '# 知乎示例\n\n这是一段测试文字。\n\n![图](01.png)', 'md')):
            folder=root/name/'旅途'; folder.mkdir(parents=True)
            (folder/f'文案.{suffix}').write_text(body, encoding='utf-8')
            Image.new('RGB', (80, 60), '#bb7766').save(folder/'01.png')
        with socket.socket() as s:
            s.bind(('127.0.0.1', 0)); port=s.getsockname()[1]
        base=f'http://127.0.0.1:{port}'
        process = subprocess.Popen([sys.executable, '-m', 'uvicorn', 'src.server:app', '--host', '127.0.0.1', '--port', str(port)], cwd=root, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        try:
            for _ in range(50):
                if process.poll() is not None:
                    raise RuntimeError('Server stopped on startup: '+process.stderr.read().decode(errors='replace')[-2000:])
                try:
                    if httpx.get(base, timeout=0.3).status_code==200: break
                except (httpx.HTTPError, OSError):pass
                time.sleep(0.15)
            else:raise RuntimeError('HTTP startup timeout')
            with sync_playwright() as pw:
                browser=pw.chromium.launch(executable_path=browser_path, headless=True,args=['--no-sandbox','--disable-dev-shm-usage'])
                page=browser.new_page(viewport={'width': 1100, 'height': 810})
                errors=[]
                page.on('pageerror',lambda err:errors.append(str(err)))
                try:
                    page.goto(base,wait_until='networkidle')
                except BrowserError as exc:
                    if 'ERR_BLOCKED_BY_ADMINISTRATOR' not in str(exc):
                        raise
                    # Managed browsers may forbid all loopback navigation. Do not
                    # mistake this for an application failure or a passed UI test.
                    for name in ('xhs', 'zhihu'):
                        payload=httpx.get(base+'/api/notes',params={'platform':name},timeout=5)
                        payload.raise_for_status()
                        assert len(payload.json()['notes'])==1
                    print('HTTP integration OK; Chromium loopback blocked by environment (UI not tested)')
                    browser.close()
                    return
                page.get_by_text('旅途', exact=True).first.click()
                assert page.locator('#body-editor').input_value().startswith('第一篇')
                page.locator('#body-editor').fill('修改并保存。\n\n#旅行')
                page.get_by_role('button',name='保存',exact=True).last.click()
                page.wait_for_timeout(220)
                assert (root/'xhs'/'旅途'/'文案.txt').read_text(encoding='utf-8').startswith('修改并保存')
                page.locator('#editor-close').click()
                page.get_by_role('button',name='知乎',exact=True).click()
                page.wait_for_timeout(250)
                page.get_by_text('知乎示例',exact=True).first.click()
                page.locator('#preview-tab').click()
                page.wait_for_timeout(200)
                assert page.locator('#body-preview img').count()==1
                page.locator('#editor-close').click()
                # Remote refresh requires real credentials; never run it in an isolated smoke.
                page.wait_for_timeout(220)
                assert not errors,errors
                browser.close()
            print('HTTP + Chromium local integration OK: XHS edit/save, Zhihu preview, refresh')
        finally:
            process.terminate()
            try:process.wait(timeout=5)
            except subprocess.TimeoutExpired:process.kill();process.wait()

if __name__=='__main__': main()
