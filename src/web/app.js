'use strict';
const $ = id => document.getElementById(id), sleep = ms => new Promise(r => setTimeout(r, ms));
const escapeHTML = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const fmt = n => n == null ? '—' : Number(n).toLocaleString('zh-CN'), when = s => s ? new Date(s).toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }) : '—';
// click_rate arrives either as a 0-1 fraction or an already-scaled percentage
// depending on the response; view_time_avg is seconds. Neither is a plain count.
const fmtMetric = (k, v) => { if (v == null)
    return '—'; const n = Number(v); if (!Number.isFinite(n))
    return '—'; if (k === 'click_rate')
    return (n <= 1 ? n * 100 : n).toFixed(1) + '%'; if (k === 'view_time_avg')
    return n >= 60 ? Math.floor(n / 60) + '分' + Math.round(n % 60) + '秒' : Math.round(n) + '秒'; return fmt(v); };
const axisLabel = n => metric === 'click_rate' ? (n <= 1 ? n * 100 : n).toFixed(0) + '%' : metric === 'view_time_avg' ? Math.round(n) + 's' : Math.round(n);
const METRICS = { views: '阅读', impressions: '曝光', likes: '点赞', favorites: '收藏', comments: '评论', shares: '分享', followers: '涨粉', click_rate: '点击率', view_time_avg: '人均时长', danmaku: '弹幕' };
// Only the Xiaohongshu creator API reports these; zhihu has no equivalent.
const XHS_ONLY = ['impressions', 'followers', 'click_rate', 'view_time_avg', 'danmaku'];
let platform = 'xhs', tab = 'draft', notes = [], detail = null, points = [], metric = 'views', range = 'all', mode = 'total', dirty = false;
const selected = new Set(); let batchCache = null, quotaCache = null;
async function api(path, data, verb) { const init = data === undefined ? {} : { method: verb || 'POST', body: data instanceof FormData ? data : JSON.stringify(data), headers: data instanceof FormData ? {} : { 'Content-Type': 'application/json' } }; const r = await fetch(path, init); const o = await r.json().catch(() => ({})); if (!r.ok || o.ok === false)
    throw Error(o.detail || o.message || `HTTP ${r.status}`); return o; }
function toast(message, error = false) {
    const node=document.createElement('div');node.className='banner'+(error?' error':'');
    const text=document.createElement('span');text.textContent=message;node.append(text);
    node.append(button('×',()=>node.remove()));$('float').prepend(node);
    setTimeout(()=>{node.style.transition='opacity .25s';node.style.opacity='0';setTimeout(()=>node.remove(),260);},
               error?6000:3500);
}
async function safe(fn) { try {
    return await fn();
}
catch (e) {
    toast(e.message, true);
    return null;
} }
function show(id, on = true) { $(id).hidden = !on; }
// One click = one task. A second click while the button is working used to be
// sent to the server too, which is how repeated 登录 clicks opened several
// browser windows.
async function guard(btn, fn) {
    if (!btn || btn.dataset.busy === '1')
        return;
    btn.dataset.busy = '1';
    btn.disabled = true;
    try {
        await fn();
    }
    finally {
        btn.disabled = false;
        btn.dataset.busy = '';
    }
}
function stat(n, k) { return n.stats?.[k] ?? null; }
function published(n) { return n.status === 'published'; }
async function loadNotes() { const requested = platform; const r = await safe(() => api(`/api/notes?platform=${requested}`)); if (!r || requested !== platform)
    return; notes = r.notes; $('published-count').textContent = notes.filter(published).length; $('draft-count').textContent = notes.filter(n => !published(n)).length; $('last-sync').textContent = r.last_synced_at ? '数据更新 ' + when(r.last_synced_at) : ''; renderList(); loadBatches(); }
function renderList() {
    const term = $('search').value.trim().toLowerCase();
    const filtered = notes.filter(n => (tab === 'published' ? published(n) : !published(n)) && n.title.toLowerCase().includes(term)).sort((a, b) => tab === 'published' ? String(b.published_at || '').localeCompare(String(a.published_at || '')) : b.modified_at - a.modified_at);
    $('count').textContent = filtered.length + ' 篇';
    const host = $('note-list');
    host.replaceChildren();
    $('select-btn').hidden = tab !== 'draft';
    $('batch-btn').hidden = tab !== 'draft';
    $('import-all-btn').hidden = tab !== 'published';
    $('import-all-btn').disabled = !filtered.some(n=>n.remote_only);
    const eligible = filtered.filter(n => n.status === 'draft' && !n.issue_count);
    const eligibleIds = new Set(eligible.map(n => n.uid));
    $('select-btn').textContent = eligible.length && eligible.every(n => selected.has(n.uid)) ? '取消' : '全选';
    $('batch-btn').disabled = ![...selected].some(uid => eligibleIds.has(uid));
    $('batch-btn').textContent = selected.size ? `发布 ${selected.size}` : '批量';
    if (!filtered.length) {
        host.innerHTML = '<div class="empty">暂无笔记</div>';
        return;
    }
    for (const n of filtered) {
        let row = document.createElement('div');
        row.className = 'note-row';
        row.tabIndex = 0;
        const photo = n.image_count ? `<img class="cover" src="/api/notes/${n.uid}/images/0" alt="">` : '<div class="cover none">—</div>';
        const state = n.remote_only ? '未导入' : n.status === 'pending' ? '待确认' : n.issue_count ? '需检查' : n.status === 'published' ? (n.dirty ? '本地已修改' : '已发布') : '未发布';
        const checkable = tab === 'draft' && n.status === 'draft' && !n.issue_count;
        const check = checkable ? `<input class="batch-check" type="checkbox" aria-label="选择 ${escapeHTML(n.title)}" ${selected.has(n.uid) ? 'checked' : ''}>` : '';
        row.innerHTML = `<div class="note-title">${check}${photo}<div class="label-text"><strong>${escapeHTML(n.title)}</strong><div class="detail-meta">${n.image_count} 图 · ${state}</div></div></div><div class="muted sub">${n.status === 'published' ? when(n.published_at) : when(n.modified_at * 1000)}</div><div class="row-metrics">${tab === 'published' ? `<span>阅 <b>${fmt(stat(n, 'views'))}</b></span><span>赞 <b>${fmt(stat(n, 'likes'))}</b></span><span>藏 <b>${fmt(stat(n, 'favorites'))}</b></span>` : '—'}</div>`;
        const cb = row.querySelector('.batch-check');
        if (cb) cb.onclick = e => { e.stopPropagation(); if(cb.checked)selected.add(n.uid);else selected.delete(n.uid); renderList(); };
        row.onclick = () => openDetail(n.uid);
        row.onkeydown = e => { if (e.key === 'Enter')
            openDetail(n.uid); };
        host.append(row);
    }
}
async function loadAccount() {
    const requested=platform;
    const r=await safe(()=>api('/api/account?platform='+requested));
    if(!r || requested!==platform)return;
    $('account-state').textContent=requested==='xhs'?
       (r.profile?.nickname || (r.last_verified_login?'已登录':'未登录')):
       (r.publish_ready?'已配置':'未配置');
    const profile=requested==='xhs'?r.profile:null;
    const statText=profile?`${profile.nickname}　关注 ${fmt(profile.following)}　粉丝 ${fmt(profile.followers)}　获赞与收藏 ${fmt(profile.liked_collected)}`:'';
    $('profile-strip').textContent=statText;
    show('profile-strip',!!statText);
    $('xhs-profile-data').textContent=statText || '暂无资料，登录后刷新';
    $('xhs-account-status').textContent=r.last_verified_login?'已登录 · '+when(r.last_verified_login):'未校验登录';
    const inv=requested==='xhs'?r.inventory:null;
    const partial=!!inv?.partial, empty=!!inv?.empty;
    const note=partial
        ? '部分数据：作品取自个人主页，无互动指标'
        : (empty ? '暂无已发布作品' : '');
    $('xhs-partial').textContent=note;
    show('xhs-partial',!!note);
    $('zhihu-account-status').textContent='发布'+(r.publish_ready?'已配置':'未配置')+' · 数据'+(r.metrics_ready?'已配置':'未配置');
}
function pickPlatform(p) { platform = p; tab = 'draft'; selected.clear(); batchCache=null; quotaCache=null; detail = null; show('editor-overlay', false); document.querySelectorAll('[data-platform]').forEach(b => b.classList.toggle('active', b.dataset.platform === p)); document.querySelectorAll('[data-state]').forEach(b => b.classList.toggle('active', b.dataset.state === tab)); $('search').value = ''; loadNotes(); loadAccount(); }
function switchState(s) { tab = s; selected.clear(); document.querySelectorAll('[data-state]').forEach(b => b.classList.toggle('active', b.dataset.state === s)); renderList(); }
function button(label, action, primary = false) { let b = document.createElement('button'); b.textContent = label; if (primary)
    b.className = 'primary'; b.onclick = action; return b; }
async function openDetail(uid) { let r = await safe(() => api(`/api/notes/${uid}`)); if (!r)
    return; detail = r.note; dirty = false; show('editor-overlay', true); $('detail-title').textContent = detail.title; $('detail-state').textContent = detail.status === 'published' ? '已发布' : detail.status === 'pending' ? '待确认' : '未发布'; $('body-editor').value = detail.body; $('body-editor').disabled = detail.status === 'pending' || detail.remote_only; $('upload-input').disabled = detail.status === 'pending' || detail.remote_only; $('save-state').textContent = ''; setupEditor(); renderImages(); renderIssues(); renderActions(); hideComments(); show('stats-section', published(detail)); if (published(detail)) {
    renderMetrics();
    const t = await safe(() => api(`/api/notes/${uid}/trend`));
    points = t?.points || [];
    metric = platform === 'zhihu' ? 'likes' : 'views';
    range = 'all';
    mode = 'total';
    drawControls();
} }
let renderSeq = 0, renderTimer = null, mathjaxLoaded = false, mathjaxReady = null;
async function typeset(host) {
    if (mathjaxReady) {
        try { await mathjaxReady; } catch (_) { }
    } else if (window.MathJax && window.MathJax.startup) {
        try { mathjaxReady = window.MathJax.startup.promise; await mathjaxReady; } catch (_) { }
    }
    if (window.MathJax && window.MathJax.typesetPromise)
        try { await window.MathJax.typesetPromise([host]); } catch (_) { }
}
function setupEditor() {
    const zh = detail.platform === 'zhihu';
    show('md-toolbar', zh);
    if (zh) {
        loadMathJax();
        renderPreview();
        $('body-editor').oninput = () => { dirty = true; $('save-state').textContent = '未保存'; clearTimeout(renderTimer); renderTimer = setTimeout(() => renderPreview(), 120); };
    } else {
        $('body-editor').oninput = () => { dirty = true; $('save-state').textContent = '未保存'; $('body-preview').textContent = $('body-editor').value; };
        $('body-preview').style.whiteSpace = 'pre-wrap';
    }
}
async function renderPreview() {
    if (!detail || detail.platform !== 'zhihu')
        return;
    const uid = detail.uid, seq = ++renderSeq;
    const host = $('body-preview');
    try {
        const r = await api(`/api/notes/${uid}/render`, { text: $('body-editor').value });
        if (seq !== renderSeq || !detail || detail.uid !== uid)
            return;
        host.innerHTML = r.html;
        await typeset(host);
    } catch (e) {
        if (seq === renderSeq)
            $('save-state').textContent = '预览：' + e.message;
    }
}
function loadMathJax() {
    if (mathjaxLoaded || window.MathJax)
        return;
    mathjaxLoaded = true;
    window.MathJax = { tex: { inlineMath: [['$', '$'], ['\\(', '\\)']], displayMath: [['$$', '$$'], ['\\[', '\\]']] }, options: { skipHtmlTags: ['script', 'noscript', 'style', 'textarea', 'pre', 'code'] } };
    const s = document.createElement('script');
    s.src = '/static/vendor/mathjax/tex-chtml.js';
    s.onload = () => { if (window.MathJax && window.MathJax.startup) mathjaxReady = window.MathJax.startup.promise; };
    document.head.append(s);
}
function mdTool(cmd) {
    if (!detail)
        return;
    const el = $('body-editor'), v = el.value, s = el.selectionStart, e = el.selectionEnd, sel = v.slice(s, e) || '文本';
    const insert = {
        bold: ['**' + sel + '**', s + 2],
        italic: ['*' + sel + '*', s + 1],
        h2: ['## ' + sel + '\n\n', s + 4],
        h3: ['### ' + sel + '\n\n', s + 5],
        quote: ['> ' + sel + '\n\n', s + 2],
        code: ['```\n' + sel + '\n```', s + 4],
        math: ['$$' + sel + '$$', s + 2],
        table: ['\n|列1|列2|\n|-|-|\n|内容|内容|\n', s + 1],
        divider: ['\n---\n', s + 1],
    }[cmd];
    if (!insert)
        return;
    const [text, pos] = insert;
    el.setRangeText(text, s, e, 'end');
    el.focus();
    el.setSelectionRange(pos, pos);
    dirty = true;
    $('save-state').textContent = '未保存';
    clearTimeout(renderTimer);
    renderTimer = setTimeout(() => renderPreview(), 120);
}
const MD_TOOLS = [['bold', 'B', '加粗'], ['italic', 'I', '斜体'], ['h2', 'H2', '标题'], ['h3', 'H3', '小标题'], ['quote', '❝', '引用'], ['code', '‹›', '代码块'], ['math', '∑', '公式'], ['table', '▦', '表格'], ['divider', '—', '分割线']];
function renderIssues() { const msgs = [...(detail.issues || []), ...(detail.last_error ? [detail.last_error] : [])]; $('issues').textContent = msgs.join(' · '); show('issues', !!msgs.length); }
// Comments are read live on demand, never cached: a stale copy shown as current
// would be worse than none. Every field is text-node assigned, never innerHTML,
// because comment bodies are other people's input.
function hideComments() { show('comments-section', false); $('comment-list').replaceChildren(); $('comments-state').textContent = ''; }
function commentNode(c, reply) { const wrap = document.createElement('div'); wrap.className = reply ? 'comment comment-reply' : 'comment'; const head = document.createElement('div'); const who = document.createElement('strong'); who.textContent = (c.user && c.user.nickname) || '匿名用户'; head.append(who); const meta = document.createElement('span'); meta.className = 'muted sub'; meta.textContent = [c.created_at ? when(c.created_at) : '', c.like_count ? `赞 ${fmt(c.like_count)}` : ''].filter(Boolean).join(' · '); head.append(meta); wrap.append(head); const body = document.createElement('div'); body.className = 'comment-body'; body.textContent = c.content || ''; wrap.append(body); (c.replies || []).forEach(r => wrap.append(commentNode(r, true))); return wrap; }
async function loadComments(btn) { await guard(btn, async () => {
    $('comments-state').textContent = '读取中…';
    const r = await safe(() => api(`/api/notes/${detail.uid}/comments`));
    if (!r) {
        $('comments-state').textContent = '';
        return;
    }
    const data = r.comments || {};
    const list = $('comment-list');
    list.replaceChildren();
    (data.comments || []).forEach(c => list.append(commentNode(c, false)));
    $('comments-state').textContent = list.childElementCount
        ? `${data.count} 条` + (data.partial ? '（部分：含抓取内容或未读完）' : '')
        : '暂无评论';
    show('comments-section', true);
}); }
function renderImages() { let arr = detail.image_names || []; const hero = $('hero'); hero.replaceChildren(); if (arr.length) {
    const img = document.createElement('img');
    img.src = detail.images[0];
    img.alt = '封面';
    hero.append(img);
}
else
    hero.textContent = '暂无图片'; const host = $('image-list'); host.replaceChildren(); arr.forEach((name, i) => { let el = document.createElement('div'); el.className = 'image-row'; let thumb = document.createElement('img'); thumb.src = detail.images[i]; thumb.alt = name; thumb.onclick = () => { hero.replaceChildren(); const image = document.createElement('img'); image.src = detail.images[i]; hero.append(image); }; el.append(thumb); let text = document.createElement('span'); text.textContent = name; el.append(text); if (detail.status !== 'pending' && !detail.remote_only) {
    if (!name.toLowerCase().endsWith('.png'))
        el.append(button('转换', () => convertImage(name)));
    el.append(button('移除', () => removeImage(name)));
} host.append(el); }); $('file-info').textContent = arr.length + ' 张图片'; }
async function saveBody() { if (!detail || detail.remote_only)
    return false; const text = $('body-editor').value; if (!dirty && text === detail.body)
    return true; const r = await safe(() => api(`/api/notes/${detail.uid}/body`, { text, expected_hash: detail.fingerprint }, 'PUT')); if (!r)
    return false; detail = r.note; dirty = false; $('save-state').textContent = '已保存'; renderActions(); renderIssues(); loadNotes(); return true; }
async function refreshDetail() { if (detail)
    await openDetail(detail.uid); loadNotes(); }
async function convertImage(filename) { const r = await safe(() => api(`/api/notes/${detail.uid}/convert`, { filename })); if (r) {
    toast('已转换 ' + r.filename);
    refreshDetail();
} }
async function removeImage(filename) { if (!confirm('移出图片 ' + filename + '？'))
    return; const r = await safe(() => api(`/api/notes/${detail.uid}/remove-image`, { filename })); if (r)
    refreshDetail(); }
async function upload(files) { if (!detail || !files.length)
    return; if (dirty && !(await saveBody()))
    return; for (const file of files) {
    const data = new FormData();
    data.append('file', file);
    const r = await safe(() => api(`/api/notes/${detail.uid}/images`, data));
    if (!r)
        return;
    if (detail.platform === 'zhihu') {
        const editor = $('body-editor');
        editor.setRangeText(`\n\n![图片](${r.filename})\n`, editor.selectionStart, editor.selectionEnd, 'end');
        dirty = true;
    }
} if (dirty) {
    const fresh = await safe(() => api(`/api/notes/${detail.uid}`));
    if (!fresh)
        return;
    detail.fingerprint = fresh.note.fingerprint;
    const ok = await saveBody();
    if (!ok)
        return;
} await refreshDetail(); }
function renderActions() { let a = $('detail-actions'); a.replaceChildren(); if(detail.remote_only) {
    a.append(button('导入',()=>importOne(detail.platform_id),true));
    if(detail.url)a.append(button('原文',()=>window.open(detail.url,'_blank','noopener,noreferrer')));
    return;
} a.append(button('目录', () => safe(() => api(`/api/notes/${detail.uid}/open-folder`, {})))); if (detail.status !== 'pending')
    a.append(button('保存', saveBody)); if (published(detail)) {
    if (detail.url)
        a.append(button('原文', () => window.open(detail.url, '_blank', 'noopener,noreferrer')));
    if (detail.platform === 'xhs' && !detail.remote_only) {
        const b = button('读评论', () => loadComments(b));
        a.append(b);
    }
    a.append(button('刷新', () => startSync(detail.uid)));
    if (detail.dirty) {
        let d = document.createElement('span');
        d.textContent = '本地已修改';
        d.className = 'muted sub';
        a.append(d);
    }
}
else if (detail.status === 'draft' && !detail.issue_count) {
    a.append(button('发布', () => publishNote(detail.uid), true));
}
else if (detail.status === 'pending') {
    a.append(button('核对', () => startSync(detail.uid)));
    a.append(button('重置', resetPending));
} if (detail.status !== 'published')
    a.append(button('关联', linkExisting)); }
async function publishNote(uid) { if (dirty && !(await saveBody()))
    return; const n = detail && detail.uid === uid ? detail : notes.find(x => x.uid === uid); if (!n || n.status !== 'draft')
    return; if (!confirm(`发布到${platform === 'xhs' ? '小红书' : '知乎'}：${n.title}？`))
    return; const r = await safe(() => api(`/api/notes/${uid}/publish`, {})); if (r?.job)
    watch(r.job); }
async function resetPending() { if (!confirm('确认平台上没有这篇？复位后可重新发布。'))
    return; let r = await safe(() => api(`/api/notes/${detail.uid}/reset`, {})); if (r)
    refreshDetail(); }
async function linkExisting() { const raw = prompt('平台文章 ID'); if (!raw)
    return; const r = await safe(() => api(`/api/notes/${detail.uid}/link`, { platform_id: raw.trim() })); if (r?.job)
    watch(r.job); }
async function startSync(uid = null) {
    const r = await safe(() => uid ? api(`/api/notes/${uid}/sync`, {}) : api('/api/refresh', { platform }));
    if (r?.job) watch(r.job);
}
async function refreshAll() {
    const r=await safe(()=>api('/api/refresh',{platform}));
    if(r?.job)watch(r.job);
    await loadNotes();await loadAccount();
}
async function importOne(id) {
    if(!confirm('导回本地：'+(detail?.title||id)+'？'))return;
    const r=await safe(()=>api('/api/import',{platform,id}));
    if(r?.job)watch(r.job);
}
async function importAll() {
    const count=notes.filter(n=>n.remote_only).length;
    if(!count || !confirm(`导回 ${count} 篇未保存到本地的作品？`))return;
    const r=await safe(()=>api('/api/import-all',{platform}));
    if(r?.job)watch(r.job);
}
function eligibleNotes() { return notes.filter(n => n.status === 'draft' && !n.issue_count && selected.has(n.uid)); }
function selectAll() {
    const term = $('search').value.trim().toLowerCase();
    const candidates = notes.filter(n => n.status === 'draft' && !n.issue_count && n.title.toLowerCase().includes(term));
    if (candidates.length && candidates.every(n => selected.has(n.uid))) candidates.forEach(n => selected.delete(n.uid));
    else candidates.forEach(n => selected.add(n.uid));
    renderList();
}
async function createBatch() {
    const items = eligibleNotes();
    if (!items.length) return;
    if (items.length > 30) { toast('每批最多 30 篇', true); return; }
    const quota = quotaCache || {limit:6,gap_minutes:60,used:0};
    if (!confirm(`将 ${items.length} 篇加入队列。\n本地上限 ${quota.limit} 篇/24小时，间隔 ≥ ${quota.gap_minutes} 分钟。\n异常时自动暂停。继续？`)) return;
    const r = await safe(() => api('/api/batches', {platform, uids:items.map(n => n.uid)}));
    if (r?.ok) { selected.clear(); renderList(); await loadBatches(); }
}
async function batchAction(id, action) {
    const r=await safe(() => api(`/api/batches/${id}/${action}`,{}));
    if(r) loadBatches();
}
async function batchSkip(id,uid) {
    if(!confirm('跳过不会重新提交，之后需人工核实。确定？')) return;
    const r=await safe(() => api(`/api/batches/${id}/skip`,{uid}));
    if(r)loadBatches();
}
async function loadBatches() {
    const p=platform;
    const [r, policy]=await Promise.all([safe(() => api('/api/batches?platform='+p)),safe(() => api('/api/publish-policy?platform='+p))]);
    if(policy && p===platform) quotaCache=policy.quota;
    if(!r || p!==platform)return;
    batchCache=r.batches?.[0] || null;
    const strip=$('batch-strip');strip.replaceChildren();
    const b=batchCache;
    if(!b) {show('batch-strip',false);$('quota-status').textContent=quotaCache?`发布额度 ${quotaCache.used}/${quotaCache.limit} · 24h`:'';return;}
    const q=b.quota || {};
    $('quota-status').textContent=`额度 ${q.used??0}/${q.limit??6} · 24h`;
    if(!['running','paused'].includes(b.status)){show('batch-strip',false);return;}
    show('batch-strip',true);
    const label=document.createElement('span');
    const c=b.counts||{};
    label.textContent=`队列 ${b.status==='running'?'运行中':'已暂停'} · ${c.done||0}/${b.items.length} 篇`;
    strip.append(label);
    const future=new Date(q.next_allowed_at).getTime();
    if(b.status==='running' && future>Date.now()+30000){const t=document.createElement('small');t.textContent='下次 '+when(q.next_allowed_at);strip.append(t);}
    if(b.last_error){const err=document.createElement('small');err.className='warning';err.textContent=b.last_error;strip.append(err);}
    const actions=document.createElement('div');actions.className='actions';
    actions.append(button(b.status==='running'?'暂停':'继续',()=>batchAction(b.id,b.status==='running'?'pause':'resume')));
    actions.append(button('终止',()=>{if(confirm('终止未完成的队列？'))batchAction(b.id,'stop');}));
    strip.append(actions);
    for(const item of b.items.filter(item=>item.state==='attention')) {
       const issue=document.createElement('div');issue.className='batch-issue';
       issue.textContent='需核对 '+(notes.find(n=>n.uid===item.note_uid)?.title||item.note_uid)+' · '+(item.message||'');
       issue.append(button('跳过',()=>batchSkip(b.id,item.note_uid)));
       strip.append(issue);
    }
}

const watching = new Map();
function watch(id) {
    // The server may answer a repeated click with the job already in flight;
    // show one banner per job instead of stacking a new one each time.
    if (!watching.has(id))
        watching.set(id, watchJob(id).finally(() => watching.delete(id)));
    return watching.get(id);
}
async function watchJob(id) {
    const el=document.createElement('div');el.className='banner running';
    const head=document.createElement('strong');head.textContent='处理中';
    const log=document.createElement('span');log.className='logs';
    const close=button('×',()=>el.remove());el.append(head,log,close);$('float').prepend(el);
    for(let i=0;i<200;i++) {
        let r;try{r=await api('/api/tasks/'+id);}catch(e){toast(e.message,true);break;}
        const j=r.job;
        head.textContent=j.name+' · '+(j.state==='running'?'处理中':j.state==='done'?'完成':'失败');
        log.textContent=(j.log||[]).slice(-2).join(' · ');
        if(j.state!=='running') {
            el.classList.remove('running');
            if(j.state==='error')el.classList.add('error');
            await loadNotes();await loadAccount();
            if(detail?.remote_only && j.name.startsWith('导入') && j.state==='done'){detail=null;show('editor-overlay',false);}
            else if(detail)await openDetail(detail.uid).catch(()=>{detail=null;show('editor-overlay',false);});
            if(j.state==='done')setTimeout(()=>{el.style.transition='opacity .25s';el.style.opacity='0';setTimeout(()=>el.remove(),260);},5000);
            else if(j.state==='error')setTimeout(()=>{el.style.transition='opacity .25s';el.style.opacity='0';setTimeout(()=>el.remove(),260);},8000);
            return;
        }
        await sleep(1500);
    }
    if(el.isConnected)head.textContent='任务仍在后台运行';
}
function renderMetrics() { const host = $('stat-grid'); host.replaceChildren(); const m = detail.stats || {}; for (const [k, label] of Object.entries(METRICS)) {
    let div = document.createElement('div');
    div.className = 'stat';
    div.innerHTML = `<b>${fmtMetric(k, m[k])}</b><span>${label}</span>`;
    host.append(div);
} $('stats-time').textContent = detail.last_captured_at ? when(detail.last_captured_at) : '暂无数据'; }
function controls(id, options, selected, onChange) { const h = $(id); h.replaceChildren(); for (const [value, label] of options) {
    const b = button(label, () => { onChange(value); drawControls(); });
    if (value === selected)
        b.className = 'active';
    h.append(b);
} }
function drawControls() { controls('metric-switch', Object.entries(METRICS).filter(([k]) => platform === 'xhs' || !XHS_ONLY.includes(k)), metric, v => metric = v); controls('range-switch', [['7d', '7天'], ['30d', '30天'], ['all', '全部']], range, v => range = v); controls('mode-switch', [['total', '累计'], ['delta', '增量']], mode, v => mode = v); drawChart(); }
function drawChart() { const host = $('chart'); host.replaceChildren(); const now = Date.now(); let filtered = points.filter(p => range === 'all' || now - Date.parse(p.captured_at) <= ({ "7d": 7, "30d": 30 }[range] * 86400000)); const values = filtered.map(p => p[metric] == null ? null : Number(p[metric])); if (mode === 'delta') {
    for (let i = values.length - 1; i >= 0; i--)
        values[i] = i === 0 || values[i] == null || values[i - 1] == null ? null : values[i] - values[i - 1];
} let valid = values.map((v, i) => ({ i, v })).filter(x => x.v != null && Number.isFinite(x.v)); if (!valid.length) {
    host.innerHTML = '<div class="empty">暂无数据</div>';
    return;
} const ns = 'http://www.w3.org/2000/svg', svg = document.createElementNS(ns, 'svg'); svg.setAttribute('viewBox', '0 0 740 210'); const add = (tag, attrs, text) => { let e = document.createElementNS(ns, tag); for (const [k, v] of Object.entries(attrs))
    e.setAttribute(k, v); if (text != null)
    e.textContent = text; svg.append(e); }; const lo = Math.min(0, ...valid.map(v => v.v)), hi = Math.max(1, ...valid.map(v => v.v)), span = hi - lo || 1, x = i => 55 + (filtered.length === 1 ? .5 : i / (filtered.length - 1)) * 665, y = v => 15 + (hi - v) / span * 160; for (let j = 0; j < 5; j++) {
    const n = lo + j * span / 4, yy = y(n);
    add('line', { x1: 55, y1: yy, x2: 720, y2: yy, stroke: 'currentColor', 'stroke-opacity': '.13' });
    add('text', { x: 48, y: yy + 3, 'text-anchor': 'end', fill: 'currentColor', opacity: '.55', 'font-size': 11 }, axisLabel(n));
} let run = []; values.forEach((v, i) => { if (v == null) {
    if (run.length > 1)
        add('polyline', { points: run.join(' '), fill: 'none', stroke: 'var(--accent)', 'stroke-width': 2 });
    run = [];
}
else {
    const xx = x(i), yy = y(v);
    run.push(xx + ',' + yy);
    add('circle', { cx: xx, cy: yy, r: 2.8, fill: 'var(--accent)' });
} }); if (run.length > 1)
    add('polyline', { points: run.join(' '), fill: 'none', stroke: 'var(--accent)', 'stroke-width': 2 }); [0, Math.floor((filtered.length - 1) / 2), filtered.length - 1].filter((v, i, a) => a.indexOf(v) === i).forEach(i => add('text', { x: x(i), y: 198, 'font-size': 11, 'text-anchor': i === 0 ? 'start' : i === filtered.length - 1 ? 'end' : 'middle', fill: 'currentColor', opacity: '.65' }, when(filtered[i].captured_at))); host.append(svg); }
async function newNote() { const title = $('new-title').value.trim(); if (!title)
    return; const r = await safe(() => api('/api/notes', { platform, title })); if (r) {
    show('new-overlay', false);
    await loadNotes();
    openDetail(r.uid);
} }
function accountDialog() { show('account-overlay'); $('account-platform').textContent = platform === 'xhs' ? '小红书' : '知乎'; show('xhs-account', platform === 'xhs'); show('zhihu-account', platform === 'zhihu'); loadAccount(); }
document.querySelectorAll('[data-platform]').forEach(b => b.onclick = () => pickPlatform(b.dataset.platform));
document.querySelectorAll('[data-state]').forEach(b => b.onclick = () => switchState(b.dataset.state));
$('search').oninput = renderList;
$('refresh-btn').onclick = () => guard($('refresh-btn'), refreshAll);
$('import-all-btn').onclick=importAll;
$('select-btn').onclick = selectAll;
$('batch-btn').onclick = createBatch;
$('new-btn').onclick = () => { show('new-overlay'); $('new-title').value = ''; $('new-title').focus(); };
$('new-close').onclick = () => show('new-overlay', false);
$('new-confirm').onclick = newNote;
$('new-title').onkeydown = e => { if (e.key === 'Enter')
    newNote(); };
$('open-dir').onclick = () => safe(() => api('/api/open-content', { platform }));
$('account-btn').onclick = accountDialog;
$('account-close').onclick = () => show('account-overlay', false);
$('xhs-login').onclick = () => guard($('xhs-login'), async () => {
    const r = await safe(() => api('/api/account/xhs/login', {}));
    if (r?.job)
        await watch(r.job);
});
$('xhs-check').onclick = () => guard($('xhs-check'), async () => {
    const r = await safe(() => api('/api/account/xhs/check', {}));
    if (r?.job)
        await watch(r.job);
});
$('zhihu-save').onclick = async () => { const r = await safe(() => api('/api/account/zhihu/credentials', { app_key: $('zhihu-key').value, app_secret: $('zhihu-secret').value, access_secret: $('zhihu-access').value })); if (r) {
    $('zhihu-secret').value = '';
    $('zhihu-access').value = '';
    toast('已保存');
    loadAccount();
} };
$('editor-close').onclick = () => { if (dirty && !confirm('正文未保存，确定关闭？'))
    return; show('editor-overlay', false); detail = null; };
(function initMdToolbar() {
    const host = $('md-toolbar');
    for (const [cmd, label, title] of MD_TOOLS) {
        const b = document.createElement('button');
        b.type = 'button';
        b.textContent = label;
        b.title = title;
        b.onclick = () => mdTool(cmd);
        host.append(b);
    }
})();
$('upload-input').onchange = e => upload([...e.target.files]).then(() => $('upload-input').value = '');
for (const overlay of ['editor-overlay', 'account-overlay', 'new-overlay'])
    $(overlay).onclick = e => { if (e.target === $(overlay)) {
        if (overlay === 'editor-overlay' && dirty)
            return;
        show(overlay, false);
        if (overlay === 'editor-overlay')
            detail = null;
    } };
document.onkeydown = e => { if (e.key === 'Escape') {
    if (!dirty) {
        show('editor-overlay', false);
        detail = null;
    }
    show('new-overlay', false);
    show('account-overlay', false);
} if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 's' && detail) {
    e.preventDefault();
    saveBody();
} };
loadNotes();
loadAccount();
setInterval(() => { if (!detail) loadNotes(); else loadBatches(); }, 18000);

function openHelp() {
    const zh=platform==='zhihu';
    const h=$('help-body');
    h.innerHTML=zh?`<p><b>知乎发布</b></p>
      <ol><li>在 <a href="https://www.zhihu.com/playground/zhihu-publisher" target="_blank" rel="noopener noreferrer">Publisher 授权页</a> 申请权限。</li>
      <li>App Key 填主页 URLToken（<code>zhihu.com/people/<b>your-token</b></code> 中的 <code>your-token</code>）。</li>
      <li>App Secret 填 Publisher 密钥。未授权不保证发布成功。</li>
      <li>在 <a href="https://developer.zhihu.com/" target="_blank" rel="noopener noreferrer">数据开放平台</a> 申请 Access Secret，填入第三栏。</li>
      <li>保存后点「刷新」验证读取，再用测试文章验证发布。</li></ol>
      <p>三项仅存本机，网页不显示密码。发布授权与数据授权相互独立。</p>`
      :`<p><b>小红书</b></p><ol><li>点「账号 → 登录」，在独立 Chrome/Edge 窗口完成扫码登录。</li>
       <li>登录后自动同步资料、作品与互动统计；也可点「校验」或「刷新」。</li>
       <li>昵称或粉丝显示「—」时，在同一浏览器登录小红书网页版后刷新。</li>
       <li>远端旧笔记显示「未导入」，打开后点「导入」，或用「导入全部」。</li></ol>
       <p>结构变化或权限不足时保留旧数据，不假定导出成功。</p>`;
    show('help-overlay');
}
$('help-btn').onclick=openHelp;
$('zhihu-help').onclick=openHelp;
$('help-close').onclick=()=>show('help-overlay',false);
$('help-overlay').onclick=e=>{if(e.target===$('help-overlay'))show('help-overlay',false);};
