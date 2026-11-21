// Lisa Enterprise: personal Google Drive browser inside the Files workspace panel.
// Uses each user's own OAuth (My Drive + Shared drives). Write/upload needs drive_write.
(function () {
  const state = {
    browseAllowed: false,
    writeAllowed: false,
    connected: false,
    ready: false,
    source: 'workspace', // workspace | drive
    stack: [], // {id,name,corpus,drive_id}
    roots: [],
  };

  function $(id) { return document.getElementById(id); }

  async function api(path, body) {
    const opts = { credentials: 'include', headers: {} };
    if (body !== undefined) {
      opts.method = 'POST';
      opts.headers['Content-Type'] = 'application/json';
      opts.body = JSON.stringify(body);
    }
    const r = await fetch(path, opts);
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.error || data.detail || r.statusText || 'Request failed');
    return data;
  }

  async function refreshStatus() {
    if (!window.LISA_ENTERPRISE) return;
    try {
      const s = await api('/api/enterprise/google/drive/status');
      state.browseAllowed = !!s.browse_allowed;
      state.writeAllowed = !!s.write_allowed;
      state.connected = !!s.connected;
      state.ready = !!s.ready;
    } catch (_) {
      state.browseAllowed = state.writeAllowed = state.connected = state.ready = false;
    }
    syncChrome();
    if (typeof renderSessionArtifacts === 'function') renderSessionArtifacts();
  }

  function syncChrome() {
    const bar = $('workspaceFilesSource');
    const driveBtn = $('wsSourceDrive');
    const banner = $('workspaceDriveBanner');
    if (!bar) return;
    const onFiles = typeof _workspacePanelActiveTab === 'undefined' || _workspacePanelActiveTab === 'files';
    bar.hidden = !onFiles || !window.LISA_ENTERPRISE;
    if (driveBtn) {
      driveBtn.hidden = !state.browseAllowed;
      driveBtn.classList.toggle('active', state.source === 'drive');
    }
    const wsBtn = $('wsSourceWorkspace');
    if (wsBtn) wsBtn.classList.toggle('active', state.source === 'workspace');
    if (banner) {
      if (state.source !== 'drive') {
        banner.hidden = true;
        banner.textContent = '';
      } else if (!state.connected) {
        banner.hidden = false;
        banner.innerHTML = 'Connect your Google account under <a href="/lisa#account">My account</a> to browse Drive.';
      } else if (!state.writeAllowed) {
        banner.hidden = false;
        banner.textContent = 'Your role can browse Google Drive, not upload.';
      } else {
        banner.hidden = false;
        banner.textContent = 'Your Google Drive (My Drive and Shared drives you can access).';
      }
    }
  }

  async function ensureRoots() {
    if (state.roots.length) return state.roots;
    const data = await api('/api/enterprise/google/drive/roots');
    state.roots = data.roots || [];
    return state.roots;
  }

  function renderBreadcrumb() {
    const bar = $('breadcrumbBar');
    if (!bar || state.source !== 'drive') return;
    const parts = [{ label: 'Google Drive', idx: -1 }].concat(
      state.stack.map((s, i) => ({ label: s.name, idx: i }))
    );
    bar.style.display = 'flex';
    bar.innerHTML = parts.map((p, i) => {
      const last = i === parts.length - 1;
      if (last) return `<span class="crumb current">${escHtml(p.label)}</span>`;
      return `<button type="button" class="crumb" data-drive-crumb="${p.idx}">${escHtml(p.label)}</button><span class="crumb-sep">/</span>`;
    }).join('');
    bar.querySelectorAll('[data-drive-crumb]').forEach((btn) => {
      btn.onclick = () => {
        const idx = Number(btn.dataset.driveCrumb);
        if (idx < 0) {
          state.stack = [];
          loadDrive();
        } else {
          state.stack = state.stack.slice(0, idx + 1);
          loadDrive();
        }
      };
    });
  }

  function escHtml(s) {
    return String(s || '').replace(/[&<>"']/g, (c) => (
      ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]
    ));
  }

  async function loadDrive() {
    const tree = $('fileTree');
    if (!tree) return;
    if (!state.connected) {
      tree.innerHTML = '<div class="workspace-artifact-empty">Connect Google under My account to browse your Drive.</div>';
      renderBreadcrumb();
      return;
    }
    tree.innerHTML = '<div class="workspace-artifact-empty">Loading Drive…</div>';
    try {
      if (!state.stack.length) {
        const roots = await ensureRoots();
        tree.innerHTML = roots.map((r) => rowHtml({
          id: r.id, name: r.name, folder: true, corpus: r.corpus, drive_id: r.drive_id || '',
        })).join('') || '<div class="workspace-artifact-empty">No Drive roots.</div>';
      } else {
        const cur = state.stack[state.stack.length - 1];
        const data = await api(
          `/api/enterprise/google/drive/list?corpus=${encodeURIComponent(cur.corpus || 'user')}`
          + `&drive_id=${encodeURIComponent(cur.drive_id || '')}`
          + `&folder_id=${encodeURIComponent(cur.id || '')}`
        );
        const items = data.items || [];
        tree.innerHTML = items.map((it) => rowHtml({
          ...it, corpus: cur.corpus, drive_id: cur.drive_id || '',
        })).join('') || '<div class="workspace-artifact-empty">This folder is empty.</div>';
      }
    } catch (e) {
      tree.innerHTML = `<div class="workspace-artifact-empty">${escHtml(e.message || String(e))}</div>`;
    }
    renderBreadcrumb();
    syncChrome();
  }

  function rowHtml(item) {
    const icon = item.folder
      ? '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 7h5l2 2h11v10H3z"/></svg>'
      : '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/></svg>';
    const actions = item.folder ? '' : (
      `<button type="button" class="workspace-drive-action" data-drive-import="${escHtml(item.id)}">Import</button>`
      + (item.webViewLink ? `<a class="workspace-drive-action" href="${escHtml(item.webViewLink)}" target="_blank" rel="noopener">Open</a>` : '')
    );
    return `<div class="file-row workspace-drive-row" data-drive-id="${escHtml(item.id)}" data-drive-folder="${item.folder ? '1' : '0'}" data-drive-corpus="${escHtml(item.corpus || '')}" data-drive-drive="${escHtml(item.drive_id || '')}" data-drive-name="${escHtml(item.name || '')}">
      <button type="button" class="workspace-drive-open">${icon}<span>${escHtml(item.name || item.id)}</span></button>
      <span class="workspace-drive-actions">${actions}</span>
    </div>`;
  }

  function onTreeClick(e) {
    if (state.source !== 'drive') return;
    const importBtn = e.target.closest('[data-drive-import]');
    if (importBtn) {
      e.preventDefault();
      e.stopPropagation();
      importFile(importBtn.dataset.driveImport);
      return;
    }
    const row = e.target.closest('.workspace-drive-row');
    if (!row) return;
    e.preventDefault();
    e.stopPropagation();
    if (row.dataset.driveFolder === '1') {
      state.stack.push({
        id: row.dataset.driveId,
        name: row.dataset.driveName || row.dataset.driveId,
        corpus: row.dataset.driveCorpus || 'user',
        drive_id: row.dataset.driveDrive || '',
      });
      loadDrive();
    } else {
      importFile(row.dataset.driveId);
    }
  }

  async function importFile(fileId) {
    if (!fileId) return;
    try {
      const out = await api('/api/enterprise/google/drive/import', { file_id: fileId });
      if (typeof showToast === 'function') showToast(`Imported to ${out.path}`);
      if (typeof openArtifactPath === 'function' && out.path) {
        // Prefer workspace-relative path under the session workspace.
        const ws = (S.session && S.session.workspace) ? S.session.workspace.replace(/\/+$/, '') + '/' : '';
        openArtifactPath(ws ? ws + out.path : out.path);
      }
      if (typeof renderSessionArtifacts === 'function') renderSessionArtifacts();
    } catch (e) {
      if (typeof showToast === 'function') showToast(e.message || String(e), true);
    }
  }

  async function uploadWorkspacePath(relPath, parentId) {
    if (!state.writeAllowed) throw new Error('Your role can browse Google Drive, not upload');
    return api('/api/enterprise/google/drive/upload', {
      path: relPath, parent_id: parentId || (state.stack[state.stack.length - 1] || {}).id || '',
    });
  }

  window.switchWorkspaceFileSource = async function switchWorkspaceFileSource(source) {
    state.source = source === 'drive' ? 'drive' : 'workspace';
    syncChrome();
    if (state.source === 'drive') {
      await loadDrive();
    } else if (typeof loadDir === 'function') {
      const bar = $('breadcrumbBar');
      if (bar) bar.style.display = '';
      loadDir('.');
    }
  };

  window.LisaDrive = state;
  window.LisaDrive.refresh = refreshStatus;
  window.LisaDrive.uploadWorkspacePath = uploadWorkspacePath;
  window.LisaDrive.syncChrome = syncChrome;

  function boot() {
    const tree = $('fileTree');
    if (tree) tree.addEventListener('click', onTreeClick, true);
    refreshStatus();
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot, { once: true });
  else boot();
})();
