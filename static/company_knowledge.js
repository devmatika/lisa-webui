// Company knowledge tab in the workspace panel (Lisa Enterprise): browse and read the company vault,
// read-only, showing only the areas the signed-in person may read. The server decides access on every
// request (/api/enterprise/knowledge/*); this file only displays what it returns.
(function () {
  'use strict';
  const st = { areas: null, filter: '', open: {}, doc: null, results: null, loading: false };
  const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const el = () => document.getElementById('workspaceKnowledge');

  async function get(url) {
    const r = await fetch(url, { credentials: 'same-origin', headers: { Accept: 'application/json' } });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.error || ('Request failed (' + r.status + ')'));
    return data;
  }

  // Show the tab only to people whose role may read company knowledge.
  async function init() {
    try {
      const me = await get('/api/enterprise/me');
      if (!(me.permissions || []).includes('knowledge.read')) return;
      const tab = document.getElementById('workspaceKnowledgeTab');
      if (tab) tab.hidden = false;
    } catch (_) { /* not an organisation install: no tab */ }
  }

  function byTitle() {
    const out = {};
    for (const a of st.areas || []) for (const d of a.docs) {
      const k = d.title.toLowerCase();
      if (!(k in out)) out[k] = d.path;
    }
    return out;
  }

  // Obsidian [[Note]] / [[Note|label]] / [[Note#part]] links lead to notes the person may read;
  // anything else stays plain text. Front matter and embedded files are not shown.
  function renderNote(text) {
    const titles = byTitle();
    const links = [];
    let src = String(text || '').replace(/^---\n[\s\S]*?\n---\n/, '').replace(/!\[\[[^\]]*\]\]/g, '');
    src = src.replace(/\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|([^\]]+))?\]\]/g, (_m, target, label) => {
      const name = target.trim().split('/').pop();
      const path = titles[name.toLowerCase()];
      const shown = (label || name).trim();
      if (!path) return shown;
      links.push({ path, shown });
      return 'qzknlink' + (links.length - 1) + 'qz';
    });
    let html = typeof renderMd === 'function' ? renderMd(src) : '<pre>' + esc(src) + '</pre>';
    html = html.replace(/qzknlink(\d+)qz/g, (_m, i) => {
      const l = links[Number(i)];
      return `<a href="#" class="ck-link" data-ck-open="${esc(l.path)}">${esc(l.shown)}</a>`;
    });
    return html;
  }

  function areaList() {
    const q = st.filter.trim().toLowerCase();
    if (!st.areas.length) {
      return '<div class="ck-empty"><b>Nothing to show yet</b>Your role cannot read any company knowledge area, or the company vault is not connected yet. Company notes live here — your Files tab is your private workspace, not a personal copy of the company vault.</div>';
    }
    const sorted = [...st.areas].sort((x, y) => (y.docs.length > 0) - (x.docs.length > 0));  // full areas first
    const blocks = sorted.map((a) => {
      const docs = q ? a.docs.filter((d) => d.title.toLowerCase().includes(q) || d.path.toLowerCase().includes(q)) : a.docs;
      if (q && !docs.length) return '';
      const open = q || st.open[a.key];
      const rows = open ? docs.map((d) => {
        const folder = d.path.split('/').slice(1, -1).join(' / ');
        return `<button type="button" class="ck-doc" data-ck-open="${esc(d.path)}"><span class="ck-doc-title">${esc(d.title)}</span>${folder ? `<span class="ck-doc-folder">${esc(folder)}</span>` : ''}</button>`;
      }).join('') || '<div class="ck-none">No notes in this area yet.</div>' : '';
      return `<div class="ck-area${a.docs.length ? '' : ' ck-area-empty'}"><button type="button" class="ck-area-head" data-ck-area="${esc(a.key)}" aria-expanded="${open ? 'true' : 'false'}">
        <span class="ck-chev" aria-hidden="true">${open ? '▾' : '▸'}</span><span class="ck-area-name">${esc(a.name)}</span><span class="ck-count">${docs.length}${a.truncated ? '+' : ''}</span></button>${rows}</div>`;
    }).join('');
    return blocks || `<div class="ck-empty"><b>No note title matches</b>Press Enter to search inside the notes.</div>`;
  }

  function resultsList() {
    if (!st.results.length) return `<div class="ck-empty"><b>Nothing found</b>Lisa has no note about this yet.</div>`;
    return st.results.map((h) => `<button type="button" class="ck-doc" data-ck-open="${esc(h.path)}"><span class="ck-doc-title">${esc(h.title)}</span>
      <span class="ck-doc-folder">${esc(h.snippet.replace(/\s+/g, ' ').slice(0, 140))}</span></button>`).join('');
  }

  function render() {
    const root = el();
    if (!root) return;
    if (st.doc) {
      const d = st.doc;
      root.innerHTML = `<div class="ck-reader">
        <div class="ck-bar"><button type="button" class="ck-back" data-ck-back>← All notes</button>
          <button type="button" class="ck-ask" data-ck-ask title="Ask Lisa about this note">Ask Lisa</button></div>
        <div class="ck-path">${esc(d.path.split('/').slice(0, -1).join(' / '))}</div>
        <div class="ck-note msg-body">${renderNote(d.content)}</div>
        ${d.truncated ? '<div class="ck-none">This note is long; Lisa shows the beginning.</div>' : ''}
        <div class="ck-foot">Read-only copy of the company vault. Changes are made in the vault itself; suggestions go through <i>Teach Lisa</i>.</div></div>`;
      return;
    }
    root.innerHTML = `<div class="ck-search"><input type="search" id="ckFilter" placeholder="Filter notes · Enter searches inside" aria-label="Filter or search company knowledge" value="${esc(st.filter)}" autocomplete="off"></div>
      <div class="ck-list">${st.loading ? '<div class="ck-none">Loading…</div>' : st.results ? `<div class="ck-results-head">Search results <button type="button" class="ck-clear" data-ck-clear>Clear</button></div>` + resultsList() : st.areas ? areaList() : ''}</div>`;
  }

  async function load() {
    st.loading = !st.areas;
    render();
    try {
      st.areas = (await get('/api/enterprise/knowledge/tree')).areas || [];
      if (st.areas.length === 1) st.open[st.areas[0].key] = true;
    } catch (e) {
      st.areas = [];
      if (typeof showToast === 'function') showToast(e.message);
    }
    st.loading = false;
    render();
  }

  async function openDoc(path) {
    try {
      st.doc = await get('/api/enterprise/knowledge/doc?path=' + encodeURIComponent(path));
      render();
      const root = el();
      if (root) root.scrollTop = 0;
    } catch (e) {
      if (typeof showToast === 'function') showToast(e.message);
    }
  }

  function onClick(e) {
    const t = e.target.closest('[data-ck-open],[data-ck-area],[data-ck-back],[data-ck-ask],[data-ck-clear]');
    if (!t) return;
    e.preventDefault();
    if (t.dataset.ckOpen) return openDoc(t.dataset.ckOpen);
    if (t.dataset.ckArea) { st.open[t.dataset.ckArea] = !st.open[t.dataset.ckArea]; return render(); }
    if (t.hasAttribute('data-ck-back')) { st.doc = null; return render(); }
    if (t.hasAttribute('data-ck-clear')) { st.results = null; st.filter = ''; render(); const f = document.getElementById('ckFilter'); if (f) f.focus(); return; }
    if (t.hasAttribute('data-ck-ask')) {
      const box = document.getElementById('msg');
      if (!box || !st.doc) return;
      box.value = `About the company note "${st.doc.title}" (${st.doc.path}): `;
      box.dispatchEvent(new Event('input', { bubbles: true }));
      box.focus();
    }
  }

  function onInput(e) {
    if (e.target.id !== 'ckFilter') return;
    st.filter = e.target.value;
    st.results = null;
    const pos = e.target.selectionStart;
    render();
    const f = document.getElementById('ckFilter');
    if (f) { f.focus(); f.setSelectionRange(pos, pos); }
  }

  async function onKey(e) {
    if (e.target.id !== 'ckFilter' || e.key !== 'Enter' || !st.filter.trim()) return;
    e.preventDefault();
    try {
      st.results = (await get('/api/enterprise/knowledge/search?q=' + encodeURIComponent(st.filter.trim()))).results || [];
    } catch (err) {
      if (typeof showToast === 'function') showToast(err.message);
      return;
    }
    render();
  }

  function wire() {
    const root = el();
    if (!root || root.dataset.wired) return;
    root.dataset.wired = '1';
    root.addEventListener('click', onClick);
    root.addEventListener('input', onInput);
    root.addEventListener('keydown', onKey);
  }

  // Called by switchWorkspacePanelTab('knowledge'): refresh each time so new notes and changed access show.
  window.renderCompanyKnowledge = function () { wire(); if (!st.doc) load(); else render(); };

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init, { once: true });
  else init();
})();
