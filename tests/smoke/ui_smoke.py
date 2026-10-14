"""Optional isolated browser UI smoke, with mocked HTTP: no platform or local API calls.

Requires an installed Chromium executable; override with CHROMIUM_PATH. Saves
screenshot under runtime/ui-smoke.png, with strictly fictional mock note data.
"""
from __future__ import annotations
import os,shutil
from pathlib import Path
from playwright.sync_api import sync_playwright
ROOT=Path(__file__).resolve().parents[2]
WEB=ROOT/'src'/'web'
MOCK_JS=r'''window.__refreshCalls = 0;
window.fetch = async (path, options) => {
  if (path === "/api/refresh") window.__refreshCalls++;
  let o={ok:true};
  if(path.startsWith('/api/notes?platform=zhihu')) o={ok:true,notes:[
     {uid:'zh1',platform:'zhihu',title:'知乎旅行记',status:'draft',folder:'同名旅途',image_count:1,modified_at:1791000000,issue_count:0,stats:{}}
  ]};
  else if(path.startsWith('/api/notes?')) o={ok:true,notes:[
     {uid:'mock1',title:'国庆沿途',status:'draft',folder:'国庆沿途',image_count:3,modified_at:1791000000,issue_count:0,stats:{}},
     {uid:'mock2',title:'秋天的北京',status:'published',folder:'秋天的北京',image_count:1,modified_at:1791000000,published_at:'2026-09-25T10:20:00Z',stats:{likes:42,views:380,favorites:13},issue_count:0}
  ]};
  else if(path.startsWith('/api/account')) o={ok:true,browser_open:false,last_verified_login:null};
  else if(path.startsWith('/api/notes/zh1')) o={ok:true,note:{uid:'zh1',title:'知乎旅行记',status:'draft',platform:'zhihu',body:'# 知乎旅行记\n\n旅行文章正文。\n\n![图](01.png)',html:'<p>旅行文章正文。</p><img src="/api/img/mock.png" alt="图">',images:[],image_names:['01.png'],stats:{},issues:[],last_error:null,issue_count:0,fingerprint:'zh-mock'}};
  else if(path.startsWith('/api/notes/mock1')) o={ok:true,note:{uid:'mock1',title:'国庆沿途',status:'draft',platform:'xhs',body:'山间的一些风景。\\n\\n#旅行 #日常',images:[],image_names:['01.png','02.png','03.png'],stats:{},issues:[],last_error:null,issue_count:0,fingerprint:'mock'}};
  return {ok:true,status:200,json:async()=>o};
};'''

def main():
    chrome=os.getenv('CHROMIUM_PATH') or next((shutil.which(x) for x in ['chromium','google-chrome','msedge','chrome'] if shutil.which(x)),None)
    if not chrome:raise SystemExit('Chromium not found; set CHROMIUM_PATH')
    with sync_playwright() as pw:
        browser=pw.chromium.launch(executable_path=chrome,headless=True,args=['--no-sandbox','--disable-dev-shm-usage'])
        page=browser.new_page(viewport={'width':1200,'height':780})
        errors=[];page.on('pageerror',lambda err:errors.append(str(err)))
        page.set_content((WEB/'index.html').read_text(encoding='utf-8'))
        page.add_style_tag(content=(WEB/'style.css').read_text(encoding='utf-8'))
        page.evaluate("() => {" + MOCK_JS + "}")
        page.add_script_tag(content=(WEB/'app.js').read_text(encoding='utf-8'))
        page.wait_for_timeout(350)
        assert page.get_by_text('国庆沿途').count()==1,'unpublished note should be listed'
        assert page.locator('#batch-btn').is_disabled()
        page.locator('.batch-check').first.check()
        assert page.locator('#batch-btn').is_enabled()
        assert '1' in page.locator('#batch-btn').inner_text()
        path=ROOT/'runtime'/'ui-home.png';path.parent.mkdir(parents=True,exist_ok=True);page.screenshot(path=str(path))
        page.get_by_text('已发布',exact=False).first.click()
        assert page.get_by_text('秋天的北京').count()==1,'published note should be listed'
        page.get_by_text('未发布',exact=False).first.click()
        page.get_by_text('国庆沿途').first.click()
        assert page.locator('#body-editor').count()==1
        page.locator('#editor-close').click()
        page.get_by_role('button', name='知乎', exact=True).click()
        page.wait_for_timeout(120)
        assert page.get_by_text('知乎旅行记').count()==1,'Zhihu must use the same list'
        page.get_by_text('知乎旅行记').click()
        assert '文' in page.locator('#body-editor').input_value(),'Zhihu editing must work'
        page.locator('#preview-tab').click()
        page.wait_for_timeout(120)
        assert page.locator('#body-preview p').count()==1, 'Zhihu preview must render'
        page.locator('#editor-close').click()
        page.locator('#refresh-btn').click()
        page.wait_for_timeout(120)
        assert page.evaluate('window.__refreshCalls')==1,'Refresh should sync the selected platform'
        page.locator('#help-btn').click()
        assert page.locator('#help-overlay').is_visible()
        assert 'App Secret' in page.locator('#help-body').inner_text()
        page.evaluate("toast('Test banner',true)")
        assert page.locator('.banner-area .banner.error').count()==1
        page.locator('#help-close').click()
        assert not errors,errors
        path=ROOT/'runtime'/'ui-smoke.png';path.parent.mkdir(parents=True,exist_ok=True)
        page.screenshot(path=str(path))
        print('UI smoke OK; mock preview:',path)
        browser.close()

if __name__=='__main__':main()
