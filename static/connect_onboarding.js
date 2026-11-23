/**
 * Member getting-started wizard (Lisa Enterprise).
 *
 * Private tour for each person — not the operator company setup wizard.
 */
(function () {
  'use strict';
  if (!window.LISA_ENTERPRISE) return;

  let DATA = null;
  let PAGE_IDX = 0;
  let BUSY = false;

  const STYLE = `
#memberOnboardingOverlay.onboarding-overlay{display:flex;z-index:1200}
#memberOnboardingOverlay .onboarding-card{width:min(920px,100%);color:var(--text)}
#memberOnboardingOverlay .onboarding-main h3{color:var(--text)}
#memberOnboardingOverlay .onboarding-copy{color:var(--muted)}
.member-ob-progress{height:4px;border-radius:999px;background:rgba(127,127,127,.18);margin:14px 0 4px;overflow:hidden}
.member-ob-progress>i{display:block;height:100%;border-radius:999px;background:var(--accent,var(--blue,#7cb9ff));width:0;transition:width .25s ease}
.member-ob-stepno{font-size:11px;color:var(--muted);letter-spacing:.04em;margin-top:4px}
.member-ob-check{padding:14px;border-radius:14px;border:1px solid var(--border);background:rgba(127,127,127,.04);margin-bottom:10px}
.member-ob-check strong{display:block;font-size:13px;margin-bottom:4px;color:var(--text)}
.member-ob-check span{font-size:12px;color:var(--muted);line-height:1.55}
.member-ob-check.ok{border-color:rgba(93,202,138,.45);background:rgba(93,202,138,.1)}
.member-ob-check.wait{border-color:rgba(230,180,80,.4);background:rgba(230,180,80,.08)}
.member-ob-actions{display:flex;flex-wrap:wrap;gap:8px;margin-top:14px;align-items:center}
.member-ob-field{display:flex;flex-direction:column;gap:6px;margin-top:12px;max-width:320px}
.member-ob-field span{font-size:12px;font-weight:700;color:var(--text)}
.member-ob-field input{padding:10px 12px;border-radius:10px;font-size:13px;background:var(--input-bg);border:1px solid var(--border2);color:var(--text)}
.member-ob-list{margin:8px 0 0;padding-left:18px;font-size:13px;color:var(--muted);line-height:1.65}
.member-ob-list b{color:var(--text)}
.member-ob-badge{display:inline-block;margin-left:6px;padding:2px 8px;border-radius:999px;font-size:10px;font-weight:700;letter-spacing:.04em;background:rgba(127,127,127,.1);color:var(--muted);vertical-align:middle}
.member-ob-badge.opt{color:var(--ok,#5dca8a);background:rgba(93,202,138,.12)}
.member-ob-cta{font-weight:700;color:var(--blue);border-color:rgba(124,185,255,.32)!important}
.onboarding-step .member-ob-dot{margin-left:auto;font-size:11px;color:var(--muted)}
.onboarding-step .member-ob-dot.ok{color:var(--ok,#5dca8a)}
/* Light theme: card must not stay on the dark gradient while --text is dark. */
:root:not(.dark) #memberOnboardingOverlay.onboarding-overlay{background:rgba(15,20,30,.45)}
:root:not(.dark) #memberOnboardingOverlay .onboarding-card{
  background:linear-gradient(180deg,#ffffff,#f7f8fb)!important;
  border-color:var(--border,#e2e6ee)!important;
  color:#1a1f2c!important;
  box-shadow:0 18px 50px rgba(15,23,42,.14);
}
:root:not(.dark) #memberOnboardingOverlay .onboarding-sidebar{
  background:linear-gradient(180deg,rgba(2,136,168,.08),#f4f6fa)!important;
  border-right-color:var(--border,#e2e6ee);
}
:root:not(.dark) #memberOnboardingOverlay .onboarding-sidebar h2,
:root:not(.dark) #memberOnboardingOverlay .onboarding-main h3,
:root:not(.dark) #memberOnboardingOverlay .onboarding-step-title,
:root:not(.dark) #memberOnboardingOverlay .member-ob-check strong,
:root:not(.dark) #memberOnboardingOverlay .member-ob-list b{color:#0f172a!important}
:root:not(.dark) #memberOnboardingOverlay .onboarding-sidebar p,
:root:not(.dark) #memberOnboardingOverlay .onboarding-step-desc,
:root:not(.dark) #memberOnboardingOverlay .onboarding-copy,
:root:not(.dark) #memberOnboardingOverlay .member-ob-list,
:root:not(.dark) #memberOnboardingOverlay .member-ob-check span,
:root:not(.dark) #memberOnboardingOverlay .member-ob-stepno{color:#5b6472!important}
:root:not(.dark) #memberOnboardingOverlay .onboarding-step{
  background:rgba(15,23,42,.03);
  color:#1a1f2c;
}
:root:not(.dark) #memberOnboardingOverlay .onboarding-step.active,
:root:not(.dark) #memberOnboardingOverlay .onboarding-step.done{
  background:rgba(2,136,168,.1);
  border-color:rgba(2,136,168,.28);
}
:root:not(.dark) #memberOnboardingOverlay .onboarding-step-index{
  background:rgba(15,23,42,.08);color:#1a1f2c;
}
:root:not(.dark) #memberOnboardingOverlay .onboarding-step.active .onboarding-step-index,
:root:not(.dark) #memberOnboardingOverlay .onboarding-step.done .onboarding-step-index{
  background:#0288a8;color:#fff;
}
:root:not(.dark) #memberOnboardingOverlay .member-ob-check{
  background:#fff;border-color:#e2e6ee;
}
:root:not(.dark) #memberOnboardingOverlay .onboarding-actions .sm-btn{
  background:#fff;border:1px solid #d0d5dd;color:#1a1f2c;
}
:root:not(.dark) #memberOnboardingOverlay .member-ob-cta{
  background:rgba(2,136,168,.1)!important;
  border-color:rgba(2,136,168,.35)!important;
  color:#0288a8!important;
}
/* Clay skin: warm palette, serif headings, charcoal primary, accent step markers (no teal/blue). */
:root[data-skin="clay"] #memberOnboardingOverlay.onboarding-overlay{background:rgba(20,20,19,.45)!important}
:root[data-skin="clay"] #memberOnboardingOverlay .onboarding-card{background:var(--bg)!important;border-color:var(--border)!important;color:var(--text)!important;box-shadow:0 24px 60px -20px rgba(20,20,19,.35)!important;border-radius:22px!important}
:root[data-skin="clay"] #memberOnboardingOverlay .onboarding-sidebar{background:var(--sidebar)!important;border-right-color:var(--border)!important}
:root[data-skin="clay"] #memberOnboardingOverlay .onboarding-sidebar h2,:root[data-skin="clay"] #memberOnboardingOverlay .onboarding-main h3{font-family:var(--font-conversation);font-weight:500!important;letter-spacing:-.01em;color:var(--strong)!important}
:root[data-skin="clay"] #memberOnboardingOverlay .onboarding-sidebar p,:root[data-skin="clay"] #memberOnboardingOverlay .onboarding-step-desc,:root[data-skin="clay"] #memberOnboardingOverlay .onboarding-copy,:root[data-skin="clay"] #memberOnboardingOverlay .member-ob-list,:root[data-skin="clay"] #memberOnboardingOverlay .member-ob-check span,:root[data-skin="clay"] #memberOnboardingOverlay .member-ob-stepno{color:var(--muted)!important}
:root[data-skin="clay"] #memberOnboardingOverlay .member-ob-list b,:root[data-skin="clay"] #memberOnboardingOverlay .member-ob-check strong,:root[data-skin="clay"] #memberOnboardingOverlay .onboarding-step-title{color:var(--strong)!important}
:root[data-skin="clay"] #memberOnboardingOverlay .onboarding-step{background:transparent!important;border:1px solid transparent!important;border-radius:12px!important;color:var(--text)!important}
:root[data-skin="clay"] #memberOnboardingOverlay .onboarding-step:hover{background:var(--surface-subtle-hover)!important}
:root[data-skin="clay"] #memberOnboardingOverlay .onboarding-step.active{background:var(--surface)!important;border-color:var(--border2)!important;box-shadow:0 1px 2px rgba(20,20,19,.05)}
:root[data-skin="clay"] #memberOnboardingOverlay .onboarding-step.done{background:transparent!important}
:root[data-skin="clay"] #memberOnboardingOverlay .onboarding-step-index{background:var(--surface-subtle-hover)!important;color:var(--muted)!important}
:root[data-skin="clay"] #memberOnboardingOverlay .onboarding-step.active .onboarding-step-index{background:var(--accent)!important;color:#fff!important}
:root[data-skin="clay"] #memberOnboardingOverlay .onboarding-step.done .onboarding-step-index{background:var(--accent-bg-strong)!important;color:var(--accent-text)!important}
:root[data-skin="clay"] #memberOnboardingOverlay .member-ob-progress>i{background:var(--accent)!important}
:root[data-skin="clay"] #memberOnboardingOverlay .member-ob-check{background:var(--surface)!important;border-color:var(--border)!important}
:root[data-skin="clay"] #memberOnboardingOverlay .onboarding-actions .sm-btn{background:var(--surface)!important;border:1px solid var(--border2)!important;color:var(--text)!important;border-radius:10px!important}
:root[data-skin="clay"] #memberOnboardingOverlay .member-ob-cta,:root[data-skin="clay"] #memberOnboardingOverlay .onboarding-actions #memberObNext{background:var(--strong)!important;border-color:var(--strong)!important;color:var(--bg)!important;border-radius:10px!important}
:root[data-skin="clay"] #memberOnboardingOverlay .member-ob-cta:hover,:root[data-skin="clay"] #memberOnboardingOverlay .onboarding-actions #memberObNext:hover{opacity:.9}
/* Fit any laptop: fixed card height, sidebar and body scroll on their own, buttons always visible. */
#memberOnboardingOverlay.onboarding-overlay{padding:16px}
#memberOnboardingOverlay .onboarding-card{display:flex;height:min(640px,calc(100vh - 32px));max-height:none!important;overflow:hidden!important}
#memberOnboardingOverlay .onboarding-shell{flex:1;min-height:0;width:100%}
#memberOnboardingOverlay .onboarding-sidebar{overflow-y:auto;min-height:0}
#memberOnboardingOverlay .onboarding-main{min-height:0;overflow:hidden}
#memberOnboardingOverlay .onboarding-body{flex:1;min-height:0;overflow-y:auto;padding-right:4px}
#memberOnboardingOverlay .onboarding-actions{flex:none;margin-top:auto;padding-top:14px;border-top:1px solid var(--border)}
@media (max-height:760px){
  #memberOnboardingOverlay .onboarding-sidebar{padding:18px 18px!important}
  #memberOnboardingOverlay .onboarding-sidebar h2{font-size:21px!important;margin:6px 0 4px!important}
  #memberOnboardingOverlay .onboarding-sidebar p{font-size:12px;line-height:1.5;margin:0 0 4px}
  #memberOnboardingOverlay .member-ob-progress{margin:8px 0 2px}
  #memberOnboardingOverlay .onboarding-steps{gap:4px!important;margin-top:12px!important}
  #memberOnboardingOverlay .onboarding-step{padding:6px 10px!important;align-items:center}
  #memberOnboardingOverlay .onboarding-step-desc{display:none}
  #memberOnboardingOverlay .onboarding-main{padding:20px 22px 16px!important;gap:12px!important}
}
/* "Finish setting up" reminder: shown after "Skip for now" until the setup is complete */
#memberObPill{position:fixed;top:52px;right:18px;z-index:900;display:flex;align-items:center;gap:10px;padding:7px 8px 7px 12px;border-radius:999px;
  background:var(--surface,#fff);border:1px solid var(--border2,#ddd);box-shadow:0 6px 24px -10px rgba(20,20,19,.25);color:var(--text);font-size:13px;
  cursor:pointer;animation:memberObPillIn .35s cubic-bezier(.2,.8,.2,1) both}
#memberObPill:hover{border-color:var(--muted)}
#memberObPill .ring{width:22px;height:22px;flex:none}
#memberObPill .ring circle{fill:none;stroke-width:3}
#memberObPill .ring .bg{stroke:var(--border2,#ddd)} #memberObPill .ring .fg{stroke:var(--accent,#c96442);stroke-linecap:round;transform:rotate(-90deg);transform-origin:50% 50%}
#memberObPill b{font-weight:600;color:var(--strong,var(--text))} #memberObPill .n{color:var(--muted)}
#memberObPill .x{width:22px;height:22px;display:grid;place-items:center;border:0;border-radius:50%;background:transparent;color:var(--muted);cursor:pointer;font-size:15px;line-height:1}
#memberObPill .x:hover{background:var(--hover-bg,rgba(0,0,0,.05));color:var(--text)}
@keyframes memberObPillIn{from{opacity:0;transform:translateY(-6px)}to{opacity:1;transform:none}}
@media (max-width:720px){#memberObPill{top:auto;bottom:96px;right:12px}}
@media (prefers-reduced-motion:reduce){#memberObPill{animation:none}}
@media (max-width:720px){
  #memberOnboardingOverlay .onboarding-steps{display:flex;flex-direction:row;gap:6px;overflow-x:auto;padding-bottom:4px}
  #memberOnboardingOverlay .onboarding-step{min-width:140px;flex-shrink:0}
  #memberOnboardingOverlay .onboarding-step-desc{display:none}
}
`;

  function brand() {
    try {
      if (window.__BRANDING__ && window.__BRANDING__.appName) return window.__BRANDING__.appName;
    } catch (_) {}
    return window._botName || 'Lisa';
  }

  function ensureStyle() {
    if (document.getElementById('member-ob-style')) return;
    const s = document.createElement('style');
    s.id = 'member-ob-style';
    s.textContent = STYLE;
    document.head.appendChild(s);
  }

  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }

  async function api(path, opts) {
    const r = await fetch(path, {
      credentials: 'include',
      headers: { 'Content-Type': 'application/json', ...(window.__csrf ? { 'X-CSRF-Token': window.__csrf } : {}) },
      ...opts,
      body: opts && opts.body != null ? JSON.stringify(opts.body) : undefined,
    });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.error || data.message || r.statusText || 'Request failed');
    return data;
  }

  function toast(msg) {
    const el = document.getElementById('toast');
    if (!el) return;
    el.textContent = msg;
    el.classList.add('show');
    setTimeout(() => el.classList.remove('show'), 2800);
  }

  function pages() { return (DATA && DATA.pages) || []; }
  function currentPage() { return pages()[PAGE_IDX] || pages()[0]; }
  function pageIndex(id) {
    const i = pages().findIndex((p) => p.id === id);
    return i < 0 ? 0 : i;
  }

  function closeWizard() {
    const el = document.getElementById('memberOnboardingOverlay');
    if (el) el.remove();
    document.removeEventListener('keydown', onKey);
  }

  // Small "Finish setting up" pill after the wizard was skipped: easy to find again, never in the way.
  function removePill() { const p = document.getElementById('memberObPill'); if (p) p.remove(); }
  function renderPill(data) {
    removePill();
    if (!data || data.completed || document.getElementById('memberOnboardingOverlay')) return;
    try { if (sessionStorage.getItem('lisa-setup-pill-hidden') === '1') return; } catch (_) {}
    const items = (data.checklist || []).filter((c) => c.status !== 'waiting');
    const done = items.filter((c) => c.status === 'done').length;
    if (!items.length || done >= items.length) return;
    ensureStyle();
    const pct = done / items.length, C = 2 * Math.PI * 9;
    const el = document.createElement('div');
    el.id = 'memberObPill'; el.setAttribute('role', 'button'); el.tabIndex = 0;
    el.setAttribute('aria-label', 'Finish setting up ' + brand());
    el.innerHTML = '<svg class="ring" viewBox="0 0 22 22" aria-hidden="true"><circle class="bg" cx="11" cy="11" r="9"/>'
      + '<circle class="fg" cx="11" cy="11" r="9" stroke-dasharray="' + (C * pct).toFixed(1) + ' ' + C.toFixed(1) + '"/></svg>'
      + '<span><b>Finish setting up</b> <span class="n">· ' + done + ' of ' + items.length + '</span></span>'
      + '<button type="button" class="x" aria-label="Hide until next visit">×</button>';
    el.addEventListener('click', (e) => {
      if (e.target.closest('.x')) {
        e.stopPropagation();
        try { sessionStorage.setItem('lisa-setup-pill-hidden', '1'); } catch (_) {}
        removePill(); return;
      }
      removePill(); reopen();
    });
    el.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); el.click(); } });
    document.body.appendChild(el);
  }

  function onKey(e) {
    if (e.key === 'Escape') dismiss();
  }

  async function persistStep(id) {
    try { await api('/api/enterprise/onboarding/connect/step', { method: 'POST', body: { step: id } }); }
    catch (_) {}
  }

  async function dismiss() {
    if (BUSY) return;
    BUSY = true;
    try { await api('/api/enterprise/onboarding/connect/dismiss', { method: 'POST', body: {} }); }
    catch (_) {}
    BUSY = false;
    closeWizard();
    renderPill(DATA);
  }

  async function finish() {
    if (BUSY) return;
    BUSY = true;
    try { await api('/api/enterprise/onboarding/connect/complete', { method: 'POST', body: {} }); }
    catch (_) {}
    BUSY = false;
    closeWizard();
    toast('You are ready — ask ' + brand() + ' anything.');
  }

  async function refresh() {
    DATA = await api('/api/enterprise/onboarding/connect');
    renderSidebar();
    renderBody();
  }

  async function skipStep() {
    const p = currentPage();
    if (!p || p.kind !== 'action' || BUSY) return;
    BUSY = true;
    try {
      DATA = await api('/api/enterprise/onboarding/connect/skip', { method: 'POST', body: { step: p.id } });
      BUSY = false;
      go(PAGE_IDX + 1);
    } catch (err) {
      BUSY = false;
      toast(err.message || 'Could not skip');
    }
  }

  async function connectMicrosoft() {
    if (BUSY) return;
    BUSY = true;
    try {
      await api('/api/enterprise/mail/accounts/microsoft', {
        method: 'POST', body: { label: 'Work', allow: 'draft' },
      });
      await refresh();
      BUSY = false;
      toast('Microsoft 365 connected.');
      go(PAGE_IDX + 1);
    } catch (err) {
      BUSY = false;
      toast(err.message || 'Could not connect Microsoft 365');
    }
  }

  async function linkTelegram() {
    const input = document.getElementById('memberObTgCode');
    const code = (input && input.value || '').trim();
    if (!code) { toast('Enter the pairing code from Telegram.'); return; }
    if (BUSY) return;
    BUSY = true;
    try {
      const r = await api('/api/enterprise/telegram/link', { method: 'POST', body: { code } });
      await refresh();
      BUSY = false;
      toast(r.gateway_restarting ? 'Linked. Telegram is ready in about a minute.' : 'Telegram linked.');
      go(PAGE_IDX + 1);
    } catch (err) {
      BUSY = false;
      toast(err.message || 'Could not link Telegram');
    }
  }

  function stepDot(p) {
    if (p.kind !== 'action') return '';
    if (p.done) return '<span class="member-ob-dot ok">✓</span>';
    if (p.optional) return '<span class="member-ob-dot">opt</span>';
    return '<span class="member-ob-dot">·</span>';
  }

  function renderSidebar() {
    const wrap = document.getElementById('memberObSteps');
    const bar = document.getElementById('memberObBar');
    const stepNo = document.getElementById('memberObStepNo');
    if (!wrap) return;
    const list = pages();
    const pct = list.length ? Math.round(((PAGE_IDX + 1) / list.length) * 100) : 0;
    if (bar) bar.style.width = pct + '%';
    if (stepNo) stepNo.textContent = 'Step ' + (PAGE_IDX + 1) + ' of ' + list.length;
    wrap.innerHTML = list.map((p, idx) => {
      const cls = 'onboarding-step' + (idx === PAGE_IDX ? ' active' : '') + (p.done && idx !== PAGE_IDX ? ' done' : '');
      return `<div class="${cls}" data-idx="${idx}" role="button" tabindex="0">
        <div class="onboarding-step-index">${idx + 1}</div>
        <div><div class="onboarding-step-title">${esc(p.title)}</div>
        <div class="onboarding-step-desc">${esc(p.desc || '')}</div></div>
        ${stepDot(p)}
      </div>`;
    }).join('');
    wrap.querySelectorAll('[data-idx]').forEach((el) => {
      const goIdx = () => go(Number(el.dataset.idx));
      el.addEventListener('click', goIdx);
      el.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); goIdx(); } });
    });
  }

  function setupHint() {
    if (!(DATA && DATA.can_run_setup)) return '';
    return `<p class="onboarding-copy" style="margin-top:10px">You can add it now in the
      <a href="/#setup">company setup wizard</a> (Google / Microsoft 365), then come back here.</p>`;
  }

  function bodyWelcome() {
    const name = esc(DATA.display_name || 'there');
    const b = esc(brand());
    return `<div>
      <h3 style="margin:0 0 8px;font-size:1.25rem">Hi ${name}</h3>
      <p class="onboarding-copy"><b>${b}</b> is your private work assistant. Chats, memory and connected accounts stay in your profile — colleagues and administrators cannot read them.</p>
      <ul class="member-ob-list">
        <li>Ask questions in chat like a helpful colleague.</li>
        <li>Connect <b>your</b> Google, Microsoft or Telegram when you are ready.</li>
        <li>Reopen this guide anytime: <b>My account → Getting started</b>.</li>
      </ul>
    </div>`;
  }

  function bodyBasics() {
    const b = esc(brand());
    return `<div>
      <h3 style="margin:0 0 8px;font-size:1.15rem">How ${b} works for you</h3>
      <div class="member-ob-check"><strong>Chat</strong><span>Everyday questions, drafts and follow-ups. Your history stays private.</span></div>
      <div class="member-ob-check"><strong>Files &amp; Resources</strong><span>Uploads from this chat. With Google connected, browse your Drive (including Shared drives) from Files.</span></div>
      <div class="member-ob-check"><strong>Company knowledge</strong><span>Answers from areas your role may read — one shared company vault.</span></div>
      <div class="member-ob-check"><strong>My account</strong><span>Telegram, Google, e-mail and sign-out. Open from the Organization / My account menu.</span></div>
    </div>`;
  }

  function bodyGoogle() {
    const g = DATA.google || {};
    if (g.connected) {
      return `<div><h3 style="margin:0 0 8px">Google</h3>
        <div class="member-ob-check ok"><strong>Connected</strong><span>Calendar, Docs and Drive use your Google account.</span></div>
        <p class="onboarding-copy">Change this anytime under My account.</p></div>`;
    }
    if (!g.ready) {
      return `<div><h3 style="margin:0 0 8px">Google <span class="member-ob-badge">waiting on IT</span></h3>
        <div class="member-ob-check wait"><strong>Not available yet</strong>
        <span>Your administrator still needs to add the company Google app (Setup wizard → Google). You can continue and connect later.</span></div>
        ${setupHint()}
        <div class="member-ob-actions"><button type="button" class="sm-btn" id="memberObSkip">Continue without Google</button></div></div>`;
    }
    return `<div><h3 style="margin:0 0 8px">Connect Google</h3>
      <p class="onboarding-copy">${esc(brand())} uses <b>your</b> Google account for calendar, documents and Drive. Nobody else's assistant can use it.</p>
      <div class="member-ob-actions">
        <a class="sm-btn member-ob-cta" href="/api/enterprise/google/start">Connect Google</a>
        <button type="button" class="sm-btn" id="memberObSkip" style="opacity:.75">Skip for now</button>
      </div></div>`;
  }

  function bodyMicrosoft() {
    const m = DATA.microsoft || {};
    const addr = esc(m.own_email || 'your work address');
    if (m.connected) {
      return `<div><h3 style="margin:0 0 8px">Microsoft 365</h3>
        <div class="member-ob-check ok"><strong>Connected</strong><span>Work mailbox ${addr} is ready.</span></div></div>`;
    }
    if (!m.ready) {
      return `<div><h3 style="margin:0 0 8px">Microsoft 365 <span class="member-ob-badge">waiting on IT</span></h3>
        <div class="member-ob-check wait"><strong>Not available yet</strong>
        <span>The administrator still needs to add Microsoft 365 in the company setup wizard.</span></div>
        ${setupHint()}
        <div class="member-ob-actions"><button type="button" class="sm-btn" id="memberObSkip">Continue without Microsoft</button></div></div>`;
    }
    return `<div><h3 style="margin:0 0 8px">Connect Microsoft 365</h3>
      <p class="onboarding-copy">Add your work mailbox <b>${addr}</b> through the company's Microsoft app. No password needed. Mail is only sent after you confirm in chat.</p>
      <div class="member-ob-actions">
        <button type="button" class="sm-btn member-ob-cta" id="memberObMs">Connect Microsoft</button>
        <button type="button" class="sm-btn" id="memberObSkip" style="opacity:.75">Skip for now</button>
      </div></div>`;
  }

  function bodyTelegram() {
    const t = DATA.telegram || {};
    if (t.linked) {
      return `<div><h3 style="margin:0 0 8px">Telegram</h3>
        <div class="member-ob-check ok"><strong>Linked</strong><span>You can talk to your assistant from your phone.</span></div></div>`;
    }
    return `<div><h3 style="margin:0 0 8px">Link Telegram <span class="member-ob-badge opt">optional</span></h3>
      <p class="onboarding-copy">Talk to your private assistant from your phone through the company bot.</p>
      <ol class="member-ob-list">
        <li>In Telegram, open the company bot and send it any message.</li>
        <li>The bot replies with a pairing code. Enter it below.</li>
      </ol>
      <label class="member-ob-field"><span>Pairing code</span>
        <input id="memberObTgCode" maxlength="12" autocomplete="off" inputmode="text" placeholder="e.g. AB12CD"></label>
      <div class="member-ob-actions">
        <button type="button" class="sm-btn member-ob-cta" id="memberObTg">Link Telegram</button>
        <button type="button" class="sm-btn" id="memberObSkip" style="opacity:.75">Skip for now</button>
      </div></div>`;
  }

  function checklistHtml() {
    const items = (DATA && DATA.checklist) || [];
    if (!items.length) return '';
    return items.map((c) => {
      const cls = c.status === 'done' ? 'ok' : (c.status === 'waiting' ? 'wait' : '');
      const mark = c.status === 'done' ? '✓ ' : (c.status === 'skipped' ? '– ' : '');
      return `<div class="member-ob-check ${cls}"><strong>${mark}${esc(c.label)}</strong><span>${esc(c.detail || '')}</span></div>`;
    }).join('');
  }

  function bodyFinish() {
    return `<div>
      <h3 style="margin:0 0 8px">You're ready</h3>
      <p class="onboarding-copy">Start a chat whenever you like. Manage accounts under
        <a href="/lisa#account">My account</a>. Reopen this guide there anytime.</p>
      ${checklistHtml()}
    </div>`;
  }

  function renderBody() {
    const body = document.getElementById('memberObBody');
    const nextBtn = document.getElementById('memberObNext');
    const backBtn = document.getElementById('memberObBack');
    const skipAll = document.getElementById('memberObSkipAll');
    if (!body || !DATA) return;
    const p = currentPage();
    const id = p ? p.id : 'welcome';
    const renderers = {
      welcome: bodyWelcome, basics: bodyBasics, google: bodyGoogle,
      microsoft: bodyMicrosoft, telegram: bodyTelegram, finish: bodyFinish,
    };
    body.innerHTML = (renderers[id] || bodyWelcome)();
    if (backBtn) backBtn.style.display = PAGE_IDX === 0 ? 'none' : '';
    if (nextBtn) {
      nextBtn.textContent = id === 'finish' ? 'Start chatting' : 'Continue';
      nextBtn.focus();
    }
    if (skipAll) skipAll.style.display = id === 'finish' ? 'none' : '';

    const skip = document.getElementById('memberObSkip');
    if (skip) skip.addEventListener('click', skipStep);
    const ms = document.getElementById('memberObMs');
    if (ms) ms.addEventListener('click', connectMicrosoft);
    const tg = document.getElementById('memberObTg');
    if (tg) tg.addEventListener('click', linkTelegram);
    const tgIn = document.getElementById('memberObTgCode');
    if (tgIn) tgIn.addEventListener('keydown', (e) => { if (e.key === 'Enter') linkTelegram(); });
  }

  function go(idx) {
    const list = pages();
    if (!list.length) return;
    PAGE_IDX = Math.max(0, Math.min(idx, list.length - 1));
    const p = currentPage();
    if (p) persistStep(p.id);
    renderSidebar();
    renderBody();
  }

  function ensureDom() {
    if (document.getElementById('memberOnboardingOverlay')) return;
    ensureStyle();
    const overlay = document.createElement('div');
    overlay.id = 'memberOnboardingOverlay';
    overlay.className = 'onboarding-overlay';
    overlay.setAttribute('role', 'dialog');
    overlay.setAttribute('aria-modal', 'true');
    overlay.setAttribute('aria-labelledby', 'memberObTitle');
    overlay.innerHTML = `<div class="onboarding-card">
      <div class="onboarding-shell">
        <div class="onboarding-sidebar">
          <div class="onboarding-badge">GETTING STARTED</div>
          <h2 id="memberObTitle">Your ${esc(brand())}</h2>
          <p>A short private guide — not company setup.</p>
          <div class="member-ob-progress" aria-hidden="true"><i id="memberObBar"></i></div>
          <div class="member-ob-stepno" id="memberObStepNo"></div>
          <div class="onboarding-steps" id="memberObSteps"></div>
        </div>
        <div class="onboarding-main">
          <div class="onboarding-body" id="memberObBody"></div>
          <div class="onboarding-actions">
            <button class="sm-btn" id="memberObBack" type="button" style="display:none">Back</button>
            <button class="sm-btn" id="memberObSkipAll" type="button" style="margin-right:auto;opacity:.7">Skip for now</button>
            <button class="sm-btn member-ob-cta" id="memberObNext" type="button">Continue</button>
          </div>
        </div>
      </div>
    </div>`;
    document.body.appendChild(overlay);
    document.getElementById('memberObBack').addEventListener('click', () => go(PAGE_IDX - 1));
    document.getElementById('memberObSkipAll').addEventListener('click', dismiss);
    document.getElementById('memberObNext').addEventListener('click', () => {
      if (currentPage() && currentPage().id === 'finish') finish();
      else go(PAGE_IDX + 1);
    });
    document.addEventListener('keydown', onKey);
  }

  function openWizard(data, preferStep) {
    DATA = data;
    removePill();
    ensureDom();
    const title = document.getElementById('memberObTitle');
    if (title) title.textContent = 'Your ' + brand();
    let idx = 0;
    if (preferStep) idx = pageIndex(preferStep);
    else if (data.step) idx = pageIndex(data.step);
    else {
      const firstNeed = (data.pages || []).findIndex((p) => p.kind === 'action' && !p.done);
      idx = firstNeed >= 0 ? firstNeed : 0;
    }
    // After Google OAuth, if that step is done, land on the next one.
    if (preferStep === 'google' && data.google && data.google.connected) {
      idx = Math.min(pageIndex('google') + 1, pages().length - 1);
    }
    PAGE_IDX = idx;
    renderSidebar();
    renderBody();
  }

  async function show(opts) {
    opts = opts || {};
    try {
      const data = await api('/api/enterprise/onboarding/connect');
      if (opts.force || data.show) openWizard(data, opts.step);
      else { closeWizard(); renderPill(data); }
    } catch (_) {}
  }

  async function reopen() {
    try {
      const data = await api('/api/enterprise/onboarding/connect/reopen', { method: 'POST', body: {} });
      openWizard(data, 'welcome');
    } catch (err) {
      toast(err.message || 'Could not open getting started');
    }
  }

  window.LISA_MEMBER_ONBOARDING = { show, reopen, close: closeWizard };

  function boot() {
    const q = new URLSearchParams(location.search);
    if (q.get('member_onboarding') === '1' || q.get('getting_started') === '1') {
      reopen().finally(() => {
        history.replaceState(null, '', location.pathname + location.hash);
      });
      return;
    }
    if (q.get('connected')) {
      const what = q.get('connected');
      if (what === 'google') toast('Google connected.');
      else if (what) toast(what + ' connected.');
      show({ force: true, step: what === 'google' ? 'google' : undefined }).finally(() => {
        if (location.pathname === '/' || location.pathname === '/index.html') {
          history.replaceState(null, '', location.pathname + location.hash);
        }
      });
      return;
    }
    show({ force: false });
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();
})();
