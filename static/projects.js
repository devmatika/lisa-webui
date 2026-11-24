/**
 * Projects page: the company's ClickUp projects sorted the PARA way (Projects / Areas / Archive), read live from
 * ClickUp through /api/projects-clickup (read-only; /api/projects is the chat folders). Editing stays in ClickUp: every task and list links there.
 * Same look as Email drafts (email_assistant.css: ea-* classes) plus a few pj-* classes for the cards.
 */
(() => {
  if (window.LisaProjects) return;
  const PJ = { data: null, tab: 'projects', q: '' };
  const esc = (v) => String(v ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const el = (id) => document.getElementById(id);
  const link = (url, text) => (/^https:\/\//.test(url || '') ? `<a href="${esc(url)}" target="_blank" rel="noopener">${esc(text)}</a>` : esc(text));
  const TABS = [['projects', 'Projects'], ['areas', 'Areas'], ['archive', 'Archive']];
  let opener = null;

  const matches = (p) => {
    const q = PJ.q.trim().toLowerCase();
    if (!q) return true;
    return [p.project, p.owner, ...Object.keys(p.people || {})].some((x) => String(x || '').toLowerCase().includes(q));
  };

  function tile(n, label, cls = '') {
    return `<div class="pj-tile ${n && cls ? cls : ''}"><b>${esc(n ?? 0)}</b><span>${esc(label)}</span></div>`;
  }

  function card(p, full) {
    if (p.error) return `<div class="ea-card"><div class="ea-card-head"><strong>${esc(p.project)}</strong></div><p class="ea-notice ea-notice--error">${esc(p.error)}</p></div>`;
    const late = (p.late && p.late.items) || [];
    const people = Object.entries(p.people || {}).slice(0, 4).map(([w, n]) => `${esc(w)} ${esc(n)}`).join(' · ');
    return `<div class="ea-card pj-card">
      <div class="ea-card-head"><strong>${esc(p.project)}</strong>
        <span class="ea-muted">${p.owner ? `responsible: ${esc(p.owner)}` : 'no one responsible set'}${p.last_activity ? ` · last activity ${esc(p.last_activity)}` : ''}</span>
        ${(p.lists || [])[0] ? `<span class="pj-open">${link(p.lists[0], 'Open in ClickUp ↗')}</span>` : ''}</div>
      <div class="pj-tiles">${tile(p.late && p.late.count, 'late', 'is-bad')}${tile(p.open, 'open')}
        ${full ? tile(p.in_progress && p.in_progress.count, 'in progress') + tile(p.done_this_week && p.done_this_week.count, 'done this week', 'is-good') : ''}
        ${tile(p.without_owner && p.without_owner.count, 'without owner', 'is-warn')}</div>
      ${people ? `<p class="ea-muted">Open per person: ${people}</p>` : ''}
      ${late.length ? `<details class="ea-more" ${full ? 'open' : ''}><summary>Late tasks</summary><ul class="pj-list">${late.map((t) =>
        `<li>${link(t.url, t.name)} <span class="ea-muted">· due ${esc(t.due)}${t.who ? ` · ${esc(t.who)}` : ''}</span></li>`).join('')}</ul></details>` : ''}
    </div>`;
  }

  function render() {
    const d = PJ.data || {};
    el('pjTabs').innerHTML = TABS.map(([k, t]) => `<button class="ea-tab ${PJ.tab === k ? 'is-active' : ''}" data-pj-tab="${k}" role="tab" aria-selected="${PJ.tab === k}">${t} <span class="ea-muted">${esc((d[k] || []).length)}</span></button>`).join('');
    if (!d.configured) {
      el('pjBody').innerHTML = `<div class="ea-card"><strong>No project map yet</strong><p class="ea-muted">An admin maps each ClickUp folder or list once to a project, an area or the archive, with the person responsible. Then your projects show up here, live from ClickUp.</p></div>`;
      return;
    }
    if (d.error) { el('pjBody').innerHTML = `<p class="ea-notice ea-notice--error">${esc(d.error)}</p>`; return; }
    if (!['projects', 'areas', 'archive'].some((k) => (d[k] || []).length)) {
      el('pjBody').innerHTML = `<div class="ea-card"><strong>No projects shared with you yet</strong><p class="ea-muted">The project map decides who sees which project. Ask an admin to add you to the projects you work on.</p></div>`;
      return;
    }
    const rows = (d[PJ.tab] || []).filter(matches);
    const intro = {
      projects: 'Active client projects: what is late, open, who is responsible and when something last happened.',
      areas: 'Ongoing work (sales, admin, marketing): only the counts and what is late.',
      archive: 'Finished or inactive projects, out of the way.',
    }[PJ.tab];
    let body;
    if (PJ.tab === 'archive') {
      body = rows.length ? `<ul class="pj-list">${rows.map((p) => `<li>${link((p.lists || [])[0], p.project)}${p.owner ? ` <span class="ea-muted">· ${esc(p.owner)}</span>` : ''}</li>`).join('')}</ul>` : '<p class="ea-muted">Nothing here.</p>';
    } else {
      rows.sort((a, b) => ((b.late && b.late.count) || 0) - ((a.late && a.late.count) || 0));
      body = rows.length ? `<div class="pj-cards">${rows.map((p) => card(p, PJ.tab === 'projects')).join('')}</div>` : '<p class="ea-muted">Nothing matches.</p>';
    }
    el('pjBody').innerHTML = `<p class="ea-muted">${intro}</p>${body}`;
    el('pjFoot').innerHTML = `<span class="ea-muted pj-at">Live from ClickUp${d.at ? `, read at ${esc(d.at)}` : ''}. Read-only: change things in ClickUp.</span><button class="sm-btn" type="button" data-pj-refresh>Refresh</button>`;
  }

  async function load(refresh) {
    el('pjBody').innerHTML = '<p class="ea-muted">Reading ClickUp…</p>';
    try {
      PJ.data = await api('/api/projects-clickup' + (refresh ? '?refresh=1' : ''));
      render();
    } catch (e) {
      el('pjBody').innerHTML = `<p class="ea-notice ea-notice--error">${esc((e && e.message) || 'Could not read ClickUp.')}</p>`;
    }
  }

  function ensureOverlay() {
    if (el('pjOverlay')) return;
    const o = document.createElement('div');
    o.id = 'pjOverlay';
    o.className = 'ea-overlay';
    o.hidden = true;
    o.setAttribute('role', 'dialog');
    o.setAttribute('aria-modal', 'true');
    o.setAttribute('aria-labelledby', 'pjTitle');
    o.innerHTML = `<div class="ea-dialog">
        <div class="ea-head"><h2 id="pjTitle">Projects</h2>
          <input class="pj-filter" id="pjFilter" type="search" placeholder="Filter by project or person" aria-label="Filter by project or person">
          <button class="ea-link" data-pj-close aria-label="Close">✕</button></div>
        <nav class="ea-tabs" id="pjTabs" role="tablist"></nav>
        <div class="ea-body" id="pjBody"></div>
        <div class="ea-foot" id="pjFoot"></div></div>`;
    document.body.appendChild(o);
    o.addEventListener('click', (e) => {
      if (e.target === o || e.target.closest('[data-pj-close]')) return close();
      const tab = e.target.closest('[data-pj-tab]');
      if (tab) { PJ.tab = tab.dataset.pjTab; render(); }
      if (e.target.closest('[data-pj-refresh]')) load(true);
    });
    o.addEventListener('keydown', (e) => { if (e.key === 'Escape') close(); });
    o.querySelector('#pjFilter').addEventListener('input', (e) => { PJ.q = e.target.value; render(); });
  }

  function open() {
    ensureOverlay();
    opener = document.activeElement;
    el('pjOverlay').hidden = false;
    el('pjFilter').focus();
    load(false);
  }
  function close() {
    el('pjOverlay').hidden = true;
    if (opener && opener.focus) opener.focus();
  }

  async function addRailButton() {
    if (el('pjRailBtn')) return;
    try {  // only people with projects in the company project map see the page
      if (!((await api('/api/projects-clickup?check=1')) || {}).count) return;
    } catch (_) { return; }
    if (el('pjRailBtn')) return;
    const settings = document.querySelector('.rail-btn[data-panel="settings"]');
    if (!settings) return;
    const b = document.createElement('button');
    b.id = 'pjRailBtn';
    b.className = 'rail-btn nav-tab has-tooltip';
    b.dataset.tooltip = 'Projects';
    b.setAttribute('aria-label', 'Projects');
    b.innerHTML = '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="3" y="4" width="5" height="16" rx="1"/><rect x="10" y="4" width="5" height="10" rx="1"/><rect x="17" y="4" width="4" height="13" rx="1"/></svg>';
    b.addEventListener('click', open);
    settings.parentNode.insertBefore(b, settings);
  }

  window.LisaProjects = { open };
  const openFromHash = () => { if (location.hash === '#projects') open(); };  // link from Telegram messages
  window.addEventListener('hashchange', openFromHash);
  const start = () => { addRailButton(); setTimeout(openFromHash, 600); };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start);
  else start();
})();
