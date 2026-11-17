// Company knowledge tab in the workspace panel (Lisa Enterprise): browse and read the company vault,
// read-only, showing only the areas the signed-in person may read. Nested folders inside an area are
// shown as a tree (from each note's path). The server decides access on every request.
(function () {
  'use strict';
  const st = { areas: null, filter: '', open: {}, folders: {}, doc: null, results: null, loading: false };
  const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const el = () => document.getElementById('workspaceKnowledge');

  async function get(url) {
    const r = await fetch(url, { credentials: 'same-origin', headers: { Accept: 'application/json' } });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.error || ('Request failed (' + r.status + ')'));
    return data;
  }

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

  // Build a nested folder tree from flat docs. Path is "area/sub/.../note.md".
  function docsToTree(docs) {
    const root = { folders: {}, docs: [] };
    for (const d of docs) {
      const parts = String(d.path || '').split('/').filter(Boolean);
      if (parts.length < 2) continue;
      const rel = parts.slice(1);
      let node = root;
      for (let i = 0; i < rel.length - 1; i++) {
        const name = rel[i];
        if (!node.folders[name]) node.folders[name] = { name, folders: {}, docs: [] };
        node = node.folders[name];
      }
      node.docs.push(d);
    }
    return root;
  }

  function countTree(node) {
    let n = (node.docs || []).length;
    for (const child of Object.values(node.folders || {})) n += countTree(child);
    return n;
  }

  function folderOpen(id, force) {
    if (force) return true;
    if (!(id in st.folders)) return false;
    return !!st.folders[id];
  }

  function renderNode(node, areaKey, prefix, depth, forceOpen) {
    const folderNames = Object.keys(node.folders || {}).sort((a, b) => a.localeCompare(b, undefined, { sensitivity: 'base' }));
    const parts = [];
    for (const name of folderNames) {
      const child = node.folders[name];
      const id = areaKey + '/' + (prefix ? prefix + '/' : '') + name;
      const open = folderOpen(id, forceOpen);
      const n = countTree(child);
      parts.push(`<div class="ck-folder" style="--ck-depth:${depth}">
        <button type="button" class="ck-folder-head" data-ck-folder="${esc(id)}" aria-expanded="${open ? 'true' : 'false'}">
          <span class="ck-chev" aria-hidden="true">${open ? '▾' : '▸'}</span>
          <span class="ck-folder-name">${esc(name)}</span>
          <span class="ck-count">${n}</span>
        </button>
        ${open ? `<div class="ck-folder-body">${renderNode(child, areaKey, prefix ? prefix + '/' + name : name, depth + 1, forceOpen)}</div>` : ''}
      </div>`);
    }
    const docs = [...(node.docs || [])].sort((a, b) => a.title.localeCompare(b.title, undefined, { sensitivity: 'base' }));
    for (const d of docs) {
      parts.push(`<button type="button" class="ck-doc" style="--ck-depth:${depth}" data-ck-open="${esc(d.path)}"><span class="ck-doc-title">${esc(d.title)}</span></button>`);
    }
    return parts.join('') || (depth === 0 ? '<div class="ck-none">No notes in this area yet.</div>' : '');
  }

  function areaList() {
    const q = st.filter.trim().toLowerCase();
    if (!st.areas.length) {
      return '<div class="ck-empty"><b>Nothing to show yet</b>Your role cannot read any company knowledge area, or the company vault is not connected yet. Company notes live here — your Files tab is your private workspace, not a personal copy of the company vault.</div>';
    }
    const sorted = [...st.areas].sort((x, y) => (y.docs.length > 0) - (x.docs.length > 0));
    const blocks = sorted.map((a) => {
      const docs = q
        ? a.docs.filter((d) => d.title.toLowerCase().includes(q) || d.path.toLowerCase().includes(q))
        : a.docs;
      if (q && !docs.length) return '';
      const open = q || st.open[a.key];
      const tree = docsToTree(docs);
      const body = open ? `<div class="ck-area-body">${renderNode(tree, a.key, '', 0, !!q)}</div>` : '';
      return `<div class="ck-area${a.docs.length ? '' : ' ck-area-empty'}">
        <button type="button" class="ck-area-head" data-ck-area="${esc(a.key)}" aria-expanded="${open ? 'true' : 'false'}">
          <span class="ck-chev" aria-hidden="true">${open ? '▾' : '▸'}</span>
          <span class="ck-area-name">${esc(a.name)}</span>
          <span class="ck-count">${docs.length}${a.truncated ? '+' : ''}</span>
        </button>${body}</div>`;
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
    const t = e.target.closest('[data-ck-open],[data-ck-area],[data-ck-folder],[data-ck-back],[data-ck-ask],[data-ck-clear]');
    if (!t) return;
    e.preventDefault();
    if (t.dataset.ckOpen) return openDoc(t.dataset.ckOpen);
    if (t.dataset.ckArea) { st.open[t.dataset.ckArea] = !st.open[t.dataset.ckArea]; return render(); }
    if (t.dataset.ckFolder) {
      const id = t.dataset.ckFolder;
      st.folders[id] = !folderOpen(id, false);
      return render();
    }
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

  window.renderCompanyKnowledge = function () { wire(); if (!st.doc) load(); else render(); };

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init, { once: true });
  else init();
})();
