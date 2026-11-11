/**
 * Email auto-drafts: setup wizard + Email Settings (one panel, one shared config).
 *
 * Reads/writes <profile>/workspace/email-assistant/ through /api/email-assistant* (validated server-side).
 * The same config.json drives the email_drafts.py cron job and Lisa's email-drafts skill.
 * No config yet -> wizard (owner, categories + tone, mode). Afterwards -> Status · Categories · Rules · Learned.
 */
(() => {
  if (window.EmailAssistant) return;

  const EA = { data: null, draft: null, tab: 'status', step: 0 };
  const esc = (v) => String(v ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const el = (id) => document.getElementById(id);
  const slug = (v) => String(v || '').trim().toLowerCase().normalize('NFKD').replace(/[\u0300-\u036f]/g, '')
    .replace(/[^a-z0-9]+/g, '-').replace(/^[^a-z]+|-+$/g, '').slice(0, 31);
  // [key, label, placeholder, hint]; the first 4 are always visible, the rest sit under "More options".
  const FIELDS = [
    ['description', 'Which emails belong here?', 'e.g. existing customers with questions about their project',
      'Lisa reads this to sort each new email. Describe the sender or topic in plain words.'],
    ['tone', 'How should the reply sound?', 'e.g. friendly, concrete, solution first', 'A few words are enough.'],
    ['address', 'Sie or du?', '', ''],
    ['farewell', 'Sign-off', 'e.g. Beste Grüße, Alex', 'Exactly as it should appear under the reply.'],
    ['greeting', 'Greeting', 'e.g. Hallo {Vorname},', 'Empty = Lisa picks one that fits.'],
    ['do', 'Always do', 'e.g. offer a short call', 'One thing every reply in this category should do.'],
    ['dont', 'Never do', 'e.g. mention prices', 'Something Lisa must avoid here.'],
    ['language', 'Reply language', '', ''],
    ['max_words', 'Max. length (words)', 'e.g. 150', 'Empty = no limit.'],
  ];
  const BASIC = 4;
  const CHOICES = {
    address: [['', 'Like the sender writes'], ['Sie', 'Sie (formal)'], ['du', 'du (casual)']],
    language: [['', 'Same as the email'], ['German', 'Always German'], ['English', 'Always English']],
  };

  // Order- and type-insensitive snapshot, so "unsaved changes" only fires on a real edit.
  const canon = (v) => (Array.isArray(v) ? v.map(canon)
    : v && typeof v === 'object' ? Object.fromEntries(Object.keys(v).sort().filter((k) => v[k] !== '' && v[k] != null).map((k) => [k, canon(v[k])]))
      : typeof v === 'number' ? String(v) : v);
  const snap = () => JSON.stringify(canon(EA.draft));
  async function load() {
    EA.data = await api('/api/email-assistant');
    EA.draft = JSON.parse(JSON.stringify(EA.data.config || {}));
    EA.saved = snap();
    if (!EA.data.configured) {
      EA.draft = { address: '', name: '', owner: '', facts: '', drafts: false, max_per_run: 8, rules: [],
                   categories: Object.fromEntries(['customer', 'prospect', 'internal', 'other'].map((k) => [k, { ...EA.data.presets[k] }])) };
      EA.step = 0;
    }
  }

  async function post(path, payload) {
    try {
      EA.data = await api(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
      EA.draft = JSON.parse(JSON.stringify(EA.data.config || {}));
      EA.saved = snap();
      notice('Saved.', false);
      return true;
    } catch (e) {
      notice((e && e.message) || 'Could not save.', true);
      return false;
    }
  }

  function notice(text, isError) {
    const n = el('eaNotice');
    if (!n) return;
    n.textContent = text;
    n.className = 'ea-notice ' + (isError ? 'ea-notice--error' : 'ea-notice--ok');
    n.hidden = false;
    clearTimeout(notice._t);
    notice._t = setTimeout(() => { n.hidden = true; }, isError ? 8000 : 2500);
  }

  // ── collect form values back into EA.draft ──────────────────────────────────
  function collectOwner() {
    ['address', 'name', 'owner', 'facts'].forEach((k) => { if (el('ea_' + k)) EA.draft[k] = el('ea_' + k).value.trim(); });
    if (el('ea_mail_url')) EA.draft.mail_url = el('ea_mail_url').value.trim();
    if (el('ea_only_from')) EA.draft.only_from = el('ea_only_from').value.split(',').map((s) => s.trim()).filter(Boolean);
  }
  function collectCategories() {
    EA.openCats = EA.openCats || new Set();
    document.querySelectorAll('[data-ea-cat]').forEach((card) => {
      const name = card.dataset.eaCat;
      const c = {};
      FIELDS.forEach(([f]) => { const i = card.querySelector(`[data-f="${f}"]`); if (i && i.value.trim()) c[f] = i.value.trim(); });
      if (!card.querySelector('[data-f="draft"]').checked) c.draft = false;
      EA.draft.categories[name] = c;
      if (card.open) EA.openCats.add(name); else EA.openCats.delete(name);
    });
  }
  function collectRules() {
    EA.draft.rules = [...document.querySelectorAll('[data-ea-rule]')].map((row) => {
      const kind = row.querySelector('[data-r="kind"]').value;
      const text = row.querySelector('[data-r="text"]').value.trim();
      const target = row.querySelector('[data-r="target"]').value;
      if (!text) return null;
      const r = { [kind]: text };
      if (target === '__skip') r.skip = true; else r.category = target;
      return r;
    }).filter(Boolean);
  }

  // ── pieces ─────────────────────────────────────────────────────────────────
  function mailboxCard() {
    const m = EA.data.mailbox || {};
    if (m.connected && !EA.reconnect) {
      return `<div class="ea-card"><div class="ea-card-head"><strong>Mailbox connected</strong>
        <span class="ea-muted">${esc(m.address || '')}${m.via === 'admin' ? ' · set up by your admin' : ''}</span>
        <button class="ea-link" data-ea-reconnect>Change</button></div></div>`;
    }
    return `<div class="ea-card"><div class="ea-card-head"><strong>Connect your mailbox</strong></div>
      <div class="ea-provider"><span class="ea-chip is-active">IMAP (any mail server)</span>
        <span class="ea-chip" title="Coming soon">Gmail — soon</span><span class="ea-chip" title="Coming soon">Outlook — soon</span></div>
      <div class="ea-grid2">
        <label>Email address<input id="ea_c_user" type="email" autocomplete="username" value="${esc(EA.draft.address || '')}" placeholder="name@company.com"></label>
        <label>Password<input id="ea_c_pass" type="password" autocomplete="current-password" placeholder="mailbox password"></label>
        <label>IMAP server<input id="ea_c_host" placeholder="mail.example.com" autocapitalize="off" spellcheck="false"></label>
        <label>Port<input id="ea_c_port" inputmode="numeric" value="993"></label>
      </div>
      <p class="ea-muted">Lisa only reads mail and saves drafts — she never sends. The password is stored on your server only,
        never shown again. The connection is tested before anything is saved.</p>
      <div class="ea-actions-row"><button class="sm-btn ea-primary" data-ea-connect>Connect and test</button>
        ${m.connected ? '<button class="sm-btn" data-ea-reconnect-cancel>Cancel</button>' : ''}</div></div>`;
  }


  function ownerForm(full) {
    const d = EA.draft;
    return `
      <div class="ea-grid2">
        <label>Mailbox address<input id="ea_address" type="email" value="${esc(d.address)}" placeholder="name@company.com"></label>
        <label>Name on the drafts<input id="ea_name" value="${esc(d.name)}" placeholder="Alexander Müller"></label>
      </div>
      <label>About you / the company (one line)<input id="ea_owner" value="${esc(d.owner)}" placeholder="Runs MATIKA, a digital agency in Vienna"></label>
      <label>Facts drafts may use <small>— services, opening hours, prices. Nothing else is stated; unknowns become [TODO] markers.</small>
        <textarea id="ea_facts" rows="3" placeholder="We build websites and AI assistants…">${esc(d.facts)}</textarea></label>
      ${full ? `<label>Webmail link <small>— shown in the chat message so you can open your drafts</small>
        <input id="ea_mail_url" value="${esc(d.mail_url)}" placeholder="https://webmail.example.com/"></label>` : ''}
      ${full ? `<label>Only these senders (pilot) <small>— comma separated, empty = all mail</small>
        <input id="ea_only_from" value="${esc((d.only_from || []).join(', '))}" placeholder="test@example.com"></label>` : ''}`;
  }

  function fieldInput([f, label, ph, hint], c) {
    const v = c[f] ?? '';
    const input = CHOICES[f]
      ? `<select data-f="${f}">${[...CHOICES[f], ...(CHOICES[f].some(([k]) => k === v) ? [] : [[v, v]])]
          .map(([k, t]) => `<option value="${esc(k)}" ${k === v ? 'selected' : ''}>${esc(t)}</option>`).join('')}</select>`
      : `<input data-f="${f}" value="${esc(v)}" placeholder="${esc(ph)}"${f === 'max_words' ? ' inputmode="numeric"' : ''}>`;
    return `<label>${esc(label)}${input}${hint ? `<small>${esc(hint)}</small>` : ''}</label>`;
  }

  // A rough picture of a draft in this category, so the settings are easy to judge.
  function previewText(c) {
    const du = c.address === 'du';
    const greet = c.greeting || (du ? 'Hallo {Vorname},' : 'Guten Tag {Name},');
    const sign = c.farewell || `${du ? 'LG' : 'Beste Grüße'}\n${EA.draft.name || ''}`.trim();
    return `${greet}\n\n[reply — ${c.tone || 'polite'}${c.do ? `; ${c.do}` : ''}${c.max_words ? `; max. ${c.max_words} words` : ''}]\n\n${sign}`;
  }

  function categoryCards() {
    const cats = EA.draft.categories || {};
    EA.openCats = EA.openCats || new Set();
    const cards = Object.entries(cats).map(([name, c]) => {
      const more = FIELDS.slice(BASIC).some(([f]) => c[f] != null && c[f] !== '');
      return `
      <details class="ea-card ea-cat" data-ea-cat="${esc(name)}" ${EA.openCats.has(name) ? 'open' : ''}>
        <summary class="ea-cat-sum">
          <strong>${esc(name)}</strong>
          <span class="ea-muted ea-cat-desc">${esc(c.description || 'no description yet — open to set it up')}</span>
          <span class="ea-pill ${c.draft === false ? '' : 'is-on'}">${c.draft === false ? 'no drafts' : 'drafts on'}</span>
        </summary>
        <div class="ea-cat-body">
          <label class="ea-switch"><input type="checkbox" data-f="draft" ${c.draft === false ? '' : 'checked'}>
            Write reply drafts for these emails <small>(off = Lisa only sorts and lists them)</small></label>
          <div class="ea-grid2">${FIELDS.slice(0, BASIC).map((fd) => fieldInput(fd, c)).join('')}</div>
          <details class="ea-more" ${more ? 'open' : ''}><summary>More options</summary>
            <div class="ea-grid2">${FIELDS.slice(BASIC).map((fd) => fieldInput(fd, c)).join('')}</div></details>
          <div class="ea-preview"><span class="ea-muted">Example of how a draft will start and end</span>
            <pre data-ea-preview>${esc(previewText(c))}</pre></div>
          <div class="ea-actions-row"><button class="ea-link ea-danger" data-ea-remove="${esc(name)}">Remove category “${esc(name)}”</button></div>
        </div>
      </details>`;
    }).join('');
    const missing = Object.keys(EA.data.presets).filter((k) => !cats[k]);
    return `
      <div class="ea-guide">
        <b>How it works</b>
        <ol><li>A new email arrives → Lisa puts it into <b>one</b> category, using “Which emails belong here?”.</li>
          <li>She writes the reply in that category's tone and saves it in your <b>Drafts</b> folder. Nothing is sent.</li>
          <li>You edit and send it yourself. Lisa learns from what you really sent.</li></ol>
        <span class="ea-muted">Click a category to change it. Don't forget <b>Save</b> at the bottom.</span>
      </div>
      <div class="ea-card ea-addbox">
        <strong>Add a category</strong>
        ${missing.length ? `<div class="ea-add"><span class="ea-muted">Ready-made:</span>${missing.map((k) =>
          `<button class="sm-btn" data-ea-add="${esc(k)}" title="${esc(EA.data.presets[k].description || '')}">+ ${esc(k)}</button>`).join('')}</div>` : ''}
        <div class="ea-add"><span class="ea-muted">Your own:</span>
          <input id="eaNewCat" placeholder="name, e.g. press or job applications" aria-label="New category name">
          <button class="sm-btn" data-ea-add="__new">+ Add</button></div>
      </div>
      ${cards}`;
  }

  function rulesForm() {
    const cats = Object.keys(EA.draft.categories || {});
    const row = (r) => {
      const kind = r.subject ? 'subject' : 'from';
      const target = r.skip ? '__skip' : (r.category || cats[0]);
      return `<div class="ea-rule" data-ea-rule>
        <select data-r="kind" aria-label="Match on"><option value="from" ${kind === 'from' ? 'selected' : ''}>From contains</option>
          <option value="subject" ${kind === 'subject' ? 'selected' : ''}>Subject contains</option></select>
        <input data-r="text" value="${esc(r[kind])}" placeholder="@bigclient.at" aria-label="Text">
        <select data-r="target" aria-label="Then">${cats.map((c) => `<option value="${esc(c)}" ${c === target ? 'selected' : ''}>→ ${esc(c)}</option>`).join('')}
          <option value="__skip" ${target === '__skip' ? 'selected' : ''}>→ ignore</option></select>
        <button class="ea-link" data-ea-rule-del aria-label="Delete rule">✕</button></div>`;
    };
    return `<p class="ea-muted">Fixed rules run before the AI (no cost). First match wins.</p>
      ${(EA.draft.rules || []).map(row).join('')}
      <button class="sm-btn" data-ea-rule-add>+ Rule</button>`;
  }

  function statusView() {
    const s = EA.data.status;
    const cron = EA.data.cron;
    const cronLine = cron ? `Background job: <b>${cron.enabled ? 'on' : 'paused'}</b> · ${esc(cron.schedule || '')} · last run ${esc(cron.last_run_at || '–')} (${esc(cron.last_status || '–')})`
      : 'Background job: <b>not installed yet</b> — your admin starts it once the mailbox is connected.';
    const rows = (s && s.log ? [...s.log].reverse() : []).map((it) => `<tr><td>${esc(it.at)}</td><td>${esc(it.from)}</td>
      <td>${esc(it.subject)}</td><td>${esc(it.category)}</td><td>${esc(it.status)}${it.latency_s != null ? ` <span class="ea-muted">(${esc(it.latency_s)} s)</span>` : ''}</td></tr>`).join('');
    return `<p>${cronLine}</p>
      <p>Drafts: <b>${EA.draft.drafts ? 'ON' : 'OFF (shadow mode: summary only)'}</b>${s ? ` · ${esc(s.stats)}` : ''}</p>
      <p class="ea-muted">Drafts appear in the <b>Drafts</b> folder of your mailbox, inside the original conversation. Review, edit and send them there — Lisa never sends.</p>
      ${rows ? `<div class="ea-table"><table><thead><tr><th>When (UTC)</th><th>From</th><th>Subject</th><th>Category</th><th>Status</th></tr></thead><tbody>${rows}</tbody></table></div>`
             : '<p class="ea-muted">No email processed yet.</p>'}`;
  }

  function learnedView() {
    const props = EA.data.proposals || {};
    const learned = EA.data.learned || {};
    const p = Object.entries(props).map(([cat, pr]) => `
      <div class="ea-card ea-card--proposal">
        <div class="ea-card-head"><strong>Suggestion for ${esc(cat)}</strong><span class="ea-muted">${pr.source === 'sent' ? `from ${esc(pr.based_on)} emails in your Sent folder` : `from ${esc(pr.based_on)} replies you edited`} · ${esc(pr.at || '')}</span></div>
        ${pr.current ? `<p class="ea-muted">Now: ${esc(pr.current)}</p>` : ''}
        <textarea rows="4" data-ea-prop="${esc(cat)}" aria-label="Suggested notes for ${esc(cat)}">${esc(pr.notes)}</textarea>
        ${(pr.examples || []).length ? `<details class="ea-why"><summary>Why? Your ${esc(pr.examples.length)} edits behind this suggestion</summary>
          ${pr.examples.map((ex) => `<div class="ea-compare"><div><span class="ea-muted">Lisa's draft</span><pre>${esc(ex.draft)}</pre></div>
            <div><span class="ea-muted">What you sent</span><pre>${esc(ex.sent)}</pre></div></div>`).join('')}</details>` : ''}
        <div class="ea-actions-row"><button class="sm-btn ea-primary" data-ea-approve="${esc(cat)}">Approve</button>
          <button class="sm-btn" data-ea-reject="${esc(cat)}">Reject</button></div>
      </div>`).join('');
    const cats = Object.keys(EA.draft.categories || {});
    const a = cats.map((cat) => `
      <label>${esc(cat)}<textarea rows="3" data-ea-learned="${esc(cat)}" placeholder="Nothing learned yet">${esc(learned[cat] || '')}</textarea></label>
      <button class="sm-btn" data-ea-learned-save="${esc(cat)}">Save ${esc(cat)}</button>`).join('');
    return `<p class="ea-muted">Every reply you send teaches Lisa your style at once. After 3 edits in a category she also suggests style rules — they apply only after you approve them. You can edit or clear the active rules any time.</p>
      ${p || '<p class="ea-muted">No new suggestions.</p>'}<h4>Active style notes</h4>${a}`;
  }

  // ── render ─────────────────────────────────────────────────────────────────
  const WIZ = [
    ['Your mailbox', () => `${mailboxCard()}${ownerForm(false)}`],
    ['Categories & tone', categoryCards],
    ['Start', () => `
      <label class="ea-choice"><input type="radio" name="ea_mode" value="shadow" ${EA.draft.drafts ? '' : 'checked'}>
        <span><b>Test week (recommended)</b><br>Lisa only sorts and summarizes. No drafts yet.</span></label>
      <label class="ea-choice"><input type="radio" name="ea_mode" value="drafts" ${EA.draft.drafts ? 'checked' : ''}>
        <span><b>Write drafts</b><br>Reply drafts go to your Drafts folder. You review and send.</span></label>`],
  ];

  function render(keepNotice) {
    if (!keepNotice && el('eaNotice')) el('eaNotice').hidden = true;
    const body = el('eaBody');
    const head = el('eaTabs');
    const foot = el('eaFoot');
    if (!EA.data.configured) {
      head.innerHTML = WIZ.map(([t], i) => `<span class="ea-step ${i === EA.step ? 'is-active' : ''}">${i + 1}. ${esc(t)}</span>`).join('');
      body.innerHTML = WIZ[EA.step][1]();
      foot.innerHTML = `${EA.step ? '<button class="sm-btn" data-ea-back>Back</button>' : ''}
        <button class="sm-btn ea-primary" data-ea-next>${EA.step === WIZ.length - 1 ? 'Save and finish' : 'Continue'}</button>`;
      return;
    }
    const tabs = [['status', 'Status'], ['owner', 'Mailbox & facts'], ['categories', 'Categories & tone'], ['rules', 'Rules'], ['learned', 'Learned']];
    updateBadge();
    head.innerHTML = tabs.map(([k, t]) => `<button class="ea-tab ${EA.tab === k ? 'is-active' : ''}" data-ea-tab="${k}" role="tab" aria-selected="${EA.tab === k}">${t}${k === 'learned' && Object.keys(EA.data.proposals || {}).length ? ' •' : ''}</button>`).join('');
    body.innerHTML = { status: statusView, owner: () => mailboxCard() + ownerForm(true) + `
        <label class="ea-switch"><input type="checkbox" id="ea_drafts" ${EA.draft.drafts ? 'checked' : ''}> Write drafts (off = summary only)</label>
        <label>Chat messages (Telegram)<select id="ea_notify">
          <option value="actionable" ${EA.draft.notify !== 'all' && EA.draft.notify !== 'off' ? 'selected' : ''}>Only when I need to act (recommended)</option>
          <option value="all" ${EA.draft.notify === 'all' ? 'selected' : ''}>Every processed email</option>
          <option value="off" ${EA.draft.notify === 'off' ? 'selected' : ''}>None — I check the Drafts folder myself</option></select></label>`,
      categories: categoryCards, rules: rulesForm, learned: learnedView }[EA.tab]();
    foot.innerHTML = ['owner', 'categories', 'rules'].includes(EA.tab) ? '<button class="sm-btn ea-primary" data-ea-save>Save</button>' : '<button class="sm-btn" data-ea-refresh>Refresh</button>';
  }

  function collectCurrent() {
    if (el('ea_address')) collectOwner();
    if (el('ea_drafts')) EA.draft.drafts = el('ea_drafts').checked;
    if (el('ea_notify')) EA.draft.notify = el('ea_notify').value;
    if (document.querySelector('[data-ea-cat]')) collectCategories();
    if (document.querySelector('[data-ea-rule]') || el('eaBody').querySelector('[data-ea-rule-add]')) collectRules();
    const mode = document.querySelector('input[name="ea_mode"]:checked');
    if (mode) EA.draft.drafts = mode.value === 'drafts';
  }

  async function onClick(e) {
    const t = e.target.closest('button');
    if (!t || !el('eaOverlay').contains(t)) return;
    const ds = t.dataset;
    if (ds.eaClose !== undefined) return close();
    collectCurrent();
    if (ds.eaTab) { EA.tab = ds.eaTab; return render(); }
    if (ds.eaReconnect !== undefined) { EA.reconnect = true; return render(); }
    if (ds.eaReconnectCancel !== undefined) { EA.reconnect = false; return render(); }
    if (ds.eaConnect !== undefined) {
      const payload = { user: el('ea_c_user').value.trim(), password: el('ea_c_pass').value,
        host: el('ea_c_host').value.trim(), port: el('ea_c_port').value.trim(), name: (el('ea_name') && el('ea_name').value.trim()) || EA.draft.name || '' };
      t.disabled = true; t.textContent = 'Testing…';
      try {
        const res = await api('/api/email-assistant/connect', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload), timeoutMs: 90000 });
        const keep = EA.draft;
        EA.data = res;
        EA.draft = { ...keep, address: payload.user };
        EA.reconnect = false;
        render(true);
        const f = res.connected_folders || {};
        notice(`Connected. Drafts folder: ${f.drafts || '?'}${f.sent ? ` · Sent: ${f.sent}` : ''}`, false);
      } catch (err) {
        t.disabled = false; t.textContent = 'Connect and test';
        notice((err && err.message) || 'Could not connect.', true);
      }
      return;
    }
    if (ds.eaNext !== undefined) {
      if (EA.step === 0 && !EA.draft.address) return notice('Enter the mailbox address.', true);
      if (EA.step < WIZ.length - 1) { EA.step += 1; return render(); }
      if (await post('/api/email-assistant/config', { config: EA.draft })) { EA.tab = 'status'; render(true); }
      return;
    }
    if (ds.eaBack !== undefined) { EA.step -= 1; return render(); }
    if (ds.eaAdd) {
      const name = ds.eaAdd === '__new' ? slug(el('eaNewCat').value) : ds.eaAdd;
      if (!/^[a-z][a-z0-9_-]{0,30}$/.test(name)) return notice('Type a name first, e.g. "press".', true);
      if (EA.draft.categories[name]) return notice(`"${name}" already exists.`, true);
      EA.draft.categories[name] = { ...(EA.data.presets[name] || { description: '', tone: '' }) };
      (EA.openCats = EA.openCats || new Set()).add(name);
      render();
      const card = document.querySelector(`[data-ea-cat="${CSS.escape(name)}"]`);
      card?.scrollIntoView({ behavior: 'smooth', block: 'center' });
      card?.querySelector('[data-f="description"]')?.focus({ preventScroll: true });
      return notice(`Added "${name}". Fill it in, then press Save.`, false);
    }
    if (ds.eaRemove) {
      if (Object.keys(EA.draft.categories).length <= 1) return notice('Keep at least one category.', true);
      if (!confirm(`Remove the category "${ds.eaRemove}"? Its rules go too. (Nothing changes until you press Save.)`)) return;
      delete EA.draft.categories[ds.eaRemove];
      EA.draft.rules = (EA.draft.rules || []).filter((r) => r.category !== ds.eaRemove);
      return render();
    }
    if (ds.eaRuleAdd !== undefined) { EA.draft.rules = [...(EA.draft.rules || []), { from: '', category: Object.keys(EA.draft.categories)[0] }]; return render(); }
    if (ds.eaRuleDel !== undefined) { t.closest('[data-ea-rule]').remove(); collectRules(); return render(); }
    if (ds.eaSave !== undefined) {
      t.disabled = true; t.textContent = 'Saving…';
      if (await post('/api/email-assistant/config', { config: EA.draft })) render(true);
      else { t.disabled = false; t.textContent = 'Save'; }
      return;
    }
    if (ds.eaRefresh !== undefined) { await load(); return render(); }
    if (ds.eaApprove) {
      const notes = document.querySelector(`[data-ea-prop="${CSS.escape(ds.eaApprove)}"]`).value;
      if (await post('/api/email-assistant/proposal', { category: ds.eaApprove, action: 'approve', notes })) render(true);
      return;
    }
    if (ds.eaReject) { if (await post('/api/email-assistant/proposal', { category: ds.eaReject, action: 'reject' })) render(true); return; }
    if (ds.eaLearnedSave) {
      const notes = document.querySelector(`[data-ea-learned="${CSS.escape(ds.eaLearnedSave)}"]`).value;
      if (await post('/api/email-assistant/learned', { category: ds.eaLearnedSave, notes })) render(true);
    }
  }

  function ensureOverlay() {
    if (el('eaOverlay')) return;
    const o = document.createElement('div');
    o.id = 'eaOverlay';
    o.className = 'ea-overlay';
    o.hidden = true;
    o.setAttribute('role', 'dialog');
    o.setAttribute('aria-modal', 'true');
    o.setAttribute('aria-labelledby', 'eaTitle');
    o.innerHTML = `<div class="ea-dialog">
        <div class="ea-head"><h2 id="eaTitle">Email drafts</h2><button class="ea-link" data-ea-close aria-label="Close">✕</button></div>
        <nav class="ea-tabs" id="eaTabs" role="tablist"></nav>
        <div class="ea-notice" id="eaNotice" role="status" aria-live="polite" hidden></div>
        <div class="ea-body" id="eaBody"></div>
        <div class="ea-foot" id="eaFoot"></div></div>`;
    document.body.appendChild(o);
    o.addEventListener('click', (e) => { if (e.target === o) close(); else onClick(e); });
    o.addEventListener('keydown', (e) => { if (e.key === 'Escape') close(); });
    o.addEventListener('input', (e) => {  // live draft example while typing in a category
      const card = e.target.closest('[data-ea-cat]');
      const pre = card && card.querySelector('[data-ea-preview]');
      if (!pre) return;
      const c = {};
      card.querySelectorAll('[data-f]').forEach((i) => { if (i.type !== 'checkbox') c[i.dataset.f] = i.value.trim(); });
      pre.textContent = previewText(c);
      card.querySelector('.ea-cat-desc').textContent = c.description || 'no description yet — open to set it up';
    });
  }

  let opener = null;
  async function open(mode) {
    ensureOverlay();
    opener = document.activeElement;
    el('eaOverlay').hidden = false;
    el('eaBody').innerHTML = '<p class="ea-muted">Loading…</p>';
    try {
      await load();
      if (mode === 'wizard' && EA.data.configured) EA.tab = 'categories';
      else if (Object.keys(EA.data.proposals || {}).length) EA.tab = 'learned';
      render();
      el('eaOverlay').querySelector('button, input')?.focus();
    } catch (e) {
      el('eaBody').innerHTML = `<p class="ea-notice ea-notice--error">${esc((e && e.message) || 'Could not load the email settings.')}</p>`;
    }
  }
  function close() {
    if (EA.data && EA.data.configured) {
      collectCurrent();
      if (snap() !== EA.saved && !confirm('You have unsaved changes. Close without saving?')) return;
    }
    el('eaOverlay').hidden = true;
    if (opener && opener.focus) opener.focus();
  }

  function updateBadge() {
    const btn = el('eaRailBtn');
    if (!btn || !EA.data) return;
    const n = Object.keys(EA.data.proposals || {}).length;
    let dot = btn.querySelector('.ea-badge');
    if (!n) { if (dot) dot.remove(); btn.setAttribute('aria-label', 'Email drafts'); return; }
    if (!dot) { dot = document.createElement('span'); dot.className = 'ea-badge'; btn.appendChild(dot); }
    dot.textContent = String(n);
    btn.setAttribute('aria-label', `Email drafts — ${n} style suggestion${n > 1 ? 's' : ''} to review`);
  }

  async function refreshBadge() {
    try { EA.data = await api('/api/email-assistant', { timeoutToast: false, redirect401: false }); updateBadge(); } catch (e) { /* not logged in yet */ }
  }

  function addRailButton() {
    if (el('eaRailBtn')) return;
    const settings = document.querySelector('.rail-btn[data-panel="settings"]');
    if (!settings) return;
    const b = document.createElement('button');
    b.id = 'eaRailBtn';
    b.className = 'rail-btn nav-tab has-tooltip';
    b.dataset.tooltip = 'Email drafts';
    b.setAttribute('aria-label', 'Email drafts');
    b.innerHTML = '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="3" y="5" width="18" height="14" rx="2"/><path d="m3 7 9 6 9-6"/></svg>';
    b.addEventListener('click', () => open());
    settings.parentNode.insertBefore(b, settings);
    refreshBadge();
    setInterval(refreshBadge, 5 * 60 * 1000);
  }

  window.EmailAssistant = { open };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', addRailButton);
  else addRailButton();
})();
