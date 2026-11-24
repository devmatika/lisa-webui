/**
 * Email auto-drafts: setup wizard + Email Settings (one panel, one shared config).
 *
 * Reads/writes <profile>/workspace/email-assistant/ through /api/email-assistant* (validated server-side).
 * The same config.json drives the email_drafts.py cron job and Lisa's email-drafts skill.
 * No config yet -> wizard (owner, categories + tone, mode). Afterwards -> Status · Categories · Rules · Learned.
 */
(() => {
  if (window.EmailAssistant) return;

  // box: null = the person's main mailbox; else the id of an extra mailbox (e.g. "jobs"). adding: "+ Add mailbox" wizard.
  const EA = { data: null, draft: null, tab: 'status', step: 0, box: null, adding: false };
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
  // Extra mailboxes go through the mailbox-actions endpoint; the main one keeps its own routes.
  const BOX_ACTION = { '/api/email-assistant/config': 'save', '/api/email-assistant/proposal': 'proposal',
                       '/api/email-assistant/learned': 'learned' };
  function call(path, payload, opts = {}) {
    if (EA.box && BOX_ACTION[path]) {
      const { action, ...rest } = payload || {};
      payload = { ...rest, action: BOX_ACTION[path], box: EA.box, ...(action ? { decision: action } : {}) };
      path = '/api/email-assistant/connect';
    }
    return api(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload), ...opts });
  }

  async function load() {
    EA.data = EA.box ? await call('/api/email-assistant/connect', { action: 'get', box: EA.box })
      : await api('/api/email-assistant');
    if (EA.adding) {  // a blank wizard for the new mailbox; the list of mailboxes stays
      EA.data = { ...EA.data, configured: false, config: {}, mailbox: { connected: false }, status: null, proposals: {}, learned: {}, cron: null };
    }
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
      EA.data = await call(path, payload);
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
      if (card.querySelector('[data-f="folder"]').checked) c.folder = true;
      const fn = card.querySelector('[data-f="folder_name"]').value.trim();
      if (fn) c.folder_name = fn;
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
      const task = row.querySelector('[data-r="task"]').value;
      if (task) r.task = task === 'always';
      const list = row.querySelector('[data-r="list"]').value.trim();
      if (list) r.task_list = list;
      const who = row.querySelector('[data-r="assignee"]').value.trim();
      if (who) r.task_assignee = who;
      return r;
    }).filter(Boolean);
  }

  // ── pieces ─────────────────────────────────────────────────────────────────
  function mailboxCard() {
    const m = EA.data.mailbox || {};
    if (m.connected && !EA.reconnect) {
      const f = EA.checks;  // only right after "Connect and test": what was actually verified
      const checks = f ? `<ul class="ea-checks">
          <li>✓ Signed in to ${esc(f.host || 'the mail server')}</li>
          <li>✓ Drafts folder found: <b>${esc(f.drafts || '?')}</b> — drafts go here, nothing is ever sent</li>
          <li>${f.sent ? `✓ Sent folder found: <b>${esc(f.sent)}</b> — Lisa learns your style from it` : '• No Sent folder found — Lisa learns your style only from your edits'}</li>
        </ul>` : '';
      return `<div class="ea-card ea-card--ok"><div class="ea-card-head"><strong>✓ Mailbox connected</strong>
        <span class="ea-muted">${esc(m.address || '')}${m.via === 'admin' ? ' · set up by your admin' : ''}</span>
        <button class="ea-link" data-ea-reconnect>Change</button></div>${checks}</div>`;
    }
    return `<div class="ea-card"><div class="ea-card-head"><strong>Connect your mailbox</strong></div>
      <div class="ea-provider"><span class="ea-chip is-active">IMAP (any mail server)</span>
        <span class="ea-chip" title="Coming soon">Gmail — soon</span><span class="ea-chip" title="Coming soon">Outlook — soon</span></div>
      <div class="ea-grid2">
        <label>Email address<input id="ea_c_user" type="email" name="ea-mailbox-address" autocomplete="off" value="${esc(EA.draft.address || '')}" placeholder="name@company.com"></label>
        <label>Mailbox password<input id="ea_c_pass" type="password" name="ea-mailbox-password" autocomplete="new-password" placeholder="the password of this mailbox"></label>
      </div>
      <details class="ea-more"><summary>Server settings <span class="ea-muted">— found automatically, change only if it fails</span></summary>
        <div class="ea-grid2">
          <label>IMAP server<input id="ea_c_host" placeholder="automatic" autocapitalize="off" spellcheck="false"></label>
          <label>Port<input id="ea_c_port" inputmode="numeric" value="993"></label>
        </div></details>
      <p class="ea-muted">Lisa reads mail, saves drafts, labels each email and moves newsletters to a “Lisa / Automated” folder — she never sends or deletes.
        The password is stored on your server only, never shown again. The connection is tested before anything is saved.</p>
      <div class="ea-actions-row"><button class="sm-btn ea-primary" data-ea-connect>Connect and test</button>
        ${m.connected ? '<button class="sm-btn" data-ea-reconnect-cancel>Cancel</button>' : ''}</div></div>`;
  }


  function ownerForm(full) {
    const d = EA.draft;
    const fromMailbox = !full;  // wizard: the address is the one connected above (Continue needs a connected mailbox)
    return `
      <div class="ea-grid2">
        ${fromMailbox ? '' : `<label>Mailbox address<input id="ea_address" type="email" value="${esc(d.address)}" placeholder="name@company.com"></label>`}
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
          ${c.folder ? '<span class="ea-pill">📁 folder</span>' : ''}<span class="ea-pill ${c.draft === false ? '' : 'is-on'}">${c.draft === false ? 'no drafts' : 'drafts on'}</span>
        </summary>
        <div class="ea-cat-body">
          <label class="ea-switch"><input type="checkbox" data-f="draft" ${c.draft === false ? '' : 'checked'}>
            Write reply drafts for these emails <small>(off = Lisa only sorts and lists them)</small></label>
          <label class="ea-switch"><input type="checkbox" data-f="folder" ${c.folder ? 'checked' : ''}>
            Move these emails to a folder under “Lisa” <small>(visible in every mail app and webmail; off = they stay in the Inbox)</small></label>
          <label>Folder name <small>— Lisa / …, letters without umlauts</small><input data-f="folder_name" value="${esc(c.folder_name || '')}" placeholder="${esc(name.charAt(0).toUpperCase() + name.slice(1))}" style="max-width:260px"></label>
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
    const suggest = (EA.data.mailbox || {}).connected ? `<div class="ea-card ea-suggest">
        <div><strong>✨ Categories made for your mailbox</strong>
          <span class="ea-muted">${EA.suggestedFrom ? `Lisa looked at the senders and subjects of your last ${esc(EA.suggestedFrom)} emails and suggested the categories below. Change anything you like.`
            : 'Lisa looks at the senders and subjects of your recent emails (not the content) and suggests categories that fit them.'}</span></div>
        <button class="sm-btn" data-ea-suggest>${EA.suggestedFrom ? 'Suggest again' : 'Suggest categories'}</button></div>` : '';
    return `${suggest}
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
        <select data-r="task" aria-label="ClickUp task"><option value="">Task: Lisa decides</option>
          <option value="always" ${r.task === true ? 'selected' : ''}>Task: always</option>
          <option value="never" ${r.task === false ? 'selected' : ''}>Task: never</option></select>
        <input data-r="list" value="${esc(r.task_list || '')}" placeholder="ClickUp list id (optional)" aria-label="ClickUp list id" inputmode="numeric">
        <input data-r="assignee" value="${esc(r.task_assignee || '')}" placeholder="Assign to: user id (optional)" aria-label="ClickUp user id to assign" inputmode="numeric">
        <button class="ea-link" data-ea-rule-del aria-label="Delete rule">✕</button></div>`;
    };
    return `<p class="ea-muted">Fixed rules run before the AI (no cost). First match wins. Task: when a reply is needed, make a ClickUp task always / never / only if important (Lisa decides). List id: put this sender's tasks in that ClickUp list (the number in the list's URL). Assign to: ClickUp user id of the person (easier: tell Lisa "tasks from X go to Moh").</p>
      ${(EA.draft.rules || []).map(row).join('')}
      <button class="sm-btn" data-ea-rule-add>+ Rule</button>`;
  }

  // The automatic thank-you (e.g. jobs@): read-only here, the text is approved and switched on by the admin.
  function autoReplyCard() {
    const ar = EA.data.auto_reply;
    if (!ar) return '';
    const days = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
    const w = ar.send_window || {};
    const dd = (w.days || []).length ? `${days[w.days[0]]}–${days[w.days[w.days.length - 1]]}` : 'every day';
    const hh = (w.hours || []).length === 2 ? ` ${w.hours[0]}:00–${w.hours[1]}:00` : '';
    const roleName = (r) => String(r || '').replace(/-/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase()).replace(/\bAi\b/g, 'AI');
    const label = { en_late: 'English · older application', en: 'English · new application',
                    de_late: 'German · older application', de: 'German · new application' };
    const texts = Object.entries(ar.texts || {}).map(([k, t]) => `<div class="ea-auto-text"><span class="ea-muted">${esc(label[k] || k)}</span>
        <pre>${esc(t.replace('{name}', 'Lea').replace('{role}', k.startsWith('de') ? 'als Vibe Coder' : 'the Vibe Coder role'))}</pre></div>`).join('');
    const pool = ar.talent_pool || [];
    return `<div class="ea-card ea-auto ${ar.enabled ? 'is-on' : ''}">
        <div class="ea-auto-head"><span class="ea-dot" aria-hidden="true"></span><strong>Automatic thank-you</strong>
          <button class="ea-toggle ${ar.enabled ? 'is-on' : ''}" role="switch" aria-checked="${ar.enabled}"
            data-ea-auto="${ar.enabled ? '0' : '1'}" aria-label="Automatic thank-you"><span></span>${ar.enabled ? 'On' : 'Off'}</button></div>
        <p class="ea-muted">${ar.enabled
          ? `Every new application gets a short, personal thank-you from you, in the applicant's language, as a reply in the same thread. Sent ${esc(dd)}${esc(hh)}, at most ${esc(ar.max_per_hour)} per hour.`
          : 'Off — nothing is sent. Switch it on when you are happy with the email below.'}</p>
        <div class="ea-stats">
          <div><b>${esc(ar.sent_total)}</b><span>thanked</span></div>
          <div><b>${esc(ar.sent_today)}</b><span>today</span></div>
          <div><b>${esc(pool.length)}</b><span>talent pool</span></div>
        </div>
        ${texts ? `<details class="ea-more"><summary>Preview the email</summary>${texts}</details>` : ''}
        ${pool.length ? `<details class="ea-more"><summary>Talent pool — said “yes” (${pool.length})</summary>
          <ul class="ea-pool">${pool.slice().reverse().map((p) => `<li><b>${esc(p.name || p.email)}</b>
            <span class="ea-muted">${esc(roleName(p.role))} · ${esc(p.date)}</span></li>`).join('')}</ul></details>` : ''}
        ${ar.retention_days ? `<span class="ea-muted">Applicants who don't reply “yes” are deleted after ${esc(Math.round(ar.retention_days / 30.5))} months.</span>` : ''}
      </div>`;
  }

  // What happened to every mail since setup: totals by outcome and by category (status.json "counts").
  function overviewCard() {
    const s = EA.data.status || {};
    const o = (s.counts || {}).outcome || {};
    const cat = Object.entries((s.counts || {}).category || {}).filter(([k, n]) => k && n).sort((a, b) => b[1] - a[1]);
    const total = Object.values(o).reduce((a, b) => a + b, 0);
    if (!total) return '';
    const tile = (n, label, hint) => `<div title="${esc(hint)}"><b>${esc(n || 0)}</b><span>${esc(label)}</span></div>`;
    const max = cat.length ? cat[0][1] : 1;
    const name = (k) => String(k).replace(/-/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase()).replace(/\bAi\b/g, 'AI');
    return `<div class="ea-card ea-overview"><div class="ea-auto-head"><strong>Overview</strong>
        <span class="ea-muted" style="margin-left:auto">all mail since setup</span></div>
      <div class="ea-stats ea-stats--6">
        ${tile(total, 'emails sorted', 'Every email Lisa looked at')}
        ${tile(o['draft'], 'drafts written', 'Reply drafts in your Drafts folder')}
        ${tile(o['auto-replied'], 'answered automatically', 'Sent with the approved automatic reply')}
        ${tile(o['needs you'], 'need you', 'Lisa could not write a reply: your answer is needed')}
        ${tile(o['newsletter / automatic'], 'newsletters & automatic', 'Newsletters, notifications, marketing')}
        ${tile(s.tasks_total, 'ClickUp tasks', 'Tasks Lisa created for important emails')}
      </div>
      ${cat.length ? `<div class="ea-cats">${cat.map(([k, n]) => `<div class="ea-catrow"><span>${esc(name(k))}</span>
        <span class="ea-bar"><i style="width:${Math.max(4, Math.round(100 * n / max))}%"></i></span><b>${esc(n)}</b></div>`).join('')}</div>` : ''}
    </div>`;
  }

  // "Is Lisa working on this mailbox, and how good are her drafts?" — one card instead of two lines of text.
  function when(iso) {
    if (!iso) return '–';
    const d = new Date(iso);
    if (isNaN(d)) return String(iso);
    const today = new Date();
    const t = d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    return d.toDateString() === today.toDateString() ? `today ${t}` : `${d.toLocaleDateString([], { day: 'numeric', month: 'short' })} ${t}`;
  }
  function cronHuman(expr) {  // "0 6-21 * * *" -> "every hour, 6:00–21:00"
    const m = /^0 (\d+)-(\d+) \* \* \*$/.exec(String(expr || '').trim());
    if (m) return `every hour, ${m[1]}:00–${m[2]}:00`;
    if (/^0 \* \* \* \*$/.test(String(expr || '').trim())) return 'every hour';
    return expr || '';
  }
  function duration(sec) {
    if (sec == null) return '–';
    if (sec < 90) return `${sec} s`;
    if (sec < 5400) return `${Math.round(sec / 60)} min`;
    return `${(sec / 3600).toFixed(1).replace(/\.0$/, '')} h`;
  }
  function assistantCard(s, cron) {
    const paused = !!EA.draft.paused;
    const m = (s && s.metrics) || {};
    const ok = cron && cron.last_status === 'ok';
    const state = paused ? ['Paused', ''] : !cron ? ['Not started yet', ''] : cron.enabled ? ['Running', 'is-on'] : ['Stopped', ''];
    const tile = (n, label, hint) => `<div title="${esc(hint)}"><b>${esc(n)}</b><span>${esc(label)}</span></div>`;
    const sentNote = m.sent ? `Of the drafts you sent: ${esc(m.unchanged)} unchanged · ${esc(m.edited)} edited · ${esc(m.rewritten)} rewritten` : '';
    return `<div class="ea-card ea-auto ${state[1]}">
      <div class="ea-auto-head"><span class="ea-dot" aria-hidden="true"></span><strong>Lisa on this mailbox</strong>
        <span class="ea-pill ${state[1]}">${esc(state[0])}</span>
        <button class="sm-btn ea-pausebtn" data-ea-pause="${paused ? '0' : '1'}"
          title="${paused ? 'Lisa reads this mailbox again' : 'Emergency stop: Lisa stops reading this mailbox immediately'}">${paused ? '▶ Resume' : '⏸ Pause'}</button></div>
      <dl class="ea-facts">
        <div><dt>Checks mail</dt><dd>${cron ? esc(cronHuman(cron.schedule)) : 'not yet — your admin starts it once the mailbox is connected'}</dd></div>
        <div><dt>Last check</dt><dd>${cron ? `${esc(when(cron.last_run_at))} <span class="ea-ok ${ok ? '' : 'is-bad'}">${ok ? '✓ ok' : esc(cron.last_status || '–')}</span>` : '–'}</dd></div>
        <div><dt>Reply drafts</dt><dd>${EA.draft.drafts ? 'On — saved in your <b>Drafts</b> folder, inside the conversation' : 'Off — summary only'}</dd></div>
        <div><dt>Sending</dt><dd>${(EA.data.auto_reply || {}).enabled ? 'Only the automatic thank-you below' : 'Never — you send every reply yourself'}</dd></div>
      </dl>
      ${s ? `<div class="ea-stats">
        ${tile(m.drafts ?? '–', 'drafts written', 'Reply drafts Lisa saved')}
        ${tile(m.sent ?? '–', 'sent by you', 'Drafts you sent from your mailbox')}
        ${tile(m.acceptance != null ? m.acceptance + '%' : '–', 'used as is or edited', 'Sent drafts that were not rewritten')}
        ${tile(duration(m.p95_s), 'until the draft is ready', 'Most drafts are ready within this time (95 %)')}
      </div>` : ''}
      ${sentNote ? `<span class="ea-muted">${sentNote}</span>` : ''}
      ${paused ? '<span class="ea-muted">Paused: Lisa does not read this mailbox at all until you resume. Mail that arrives meanwhile is left alone.</span>' : ''}
    </div>`;
  }

  function statusView() {
    const s = EA.data.status;
    const cron = EA.data.cron;
    const cronLine = cron ? `Background job: <b>${cron.enabled ? 'on' : 'paused'}</b> · ${esc(cron.schedule || '')} · last run ${esc(cron.last_run_at || '–')} (${esc(cron.last_status || '–')})`
      : 'Background job: <b>not installed yet</b> — your admin starts it once the mailbox is connected.';
    const prio = (p) => (p === 'high' ? '<span class="ea-prio is-high">high</span>' : p ? `<span class="ea-prio">${esc(p)}</span>` : '');
    const waiting = (s && s.open) || [];
    const waitList = waiting.length ? `<div class="ea-card"><strong>Waiting for your reply (${waiting.length})</strong>
        <ul class="ea-wait">${waiting.map((v) => `<li>${prio(v.priority)} <b>${esc(v.from)}</b> — ${esc(v.subject)}
          <span class="ea-muted">· ${v.status === 'draft' ? 'draft ready' : 'needs your answer'}${v.due ? ` · due ${esc(v.due)}` : ''}</span></li>`).join('')}</ul>
        <span class="ea-muted">Leaves the list as soon as you reply. Every morning this list also comes as a chat message.</span></div>`
      : (s ? '<p class="ea-muted">Nothing is waiting for your reply. ✅</p>' : '');
    const rows = (s && s.log ? [...s.log].reverse() : []).map((it) => `<tr><td>${esc(it.at)}</td><td>${esc(it.from)}</td>
      <td>${esc(it.subject)}</td><td>${esc(it.category)}</td><td>${prio(it.priority)}</td><td>${esc(it.status)}${it.latency_s != null ? ` <span class="ea-muted">(${esc(it.latency_s)} s)</span>` : ''}</td></tr>`).join('');
    const pause = EA.draft.paused
      ? `<div class="ea-card ea-paused"><strong>⏸ Assistant paused</strong> — Lisa does not read this mailbox at all until you resume. Mail that arrives meanwhile is left alone.
          <div><button class="sm-btn" data-ea-pause="0">▶ Resume</button></div></div>`
      : `<p><button class="sm-btn" data-ea-pause="1" title="Emergency stop: Lisa stops reading this mailbox immediately">⏸ Pause assistant</button></p>`;
    return `${assistantCard(s, cron)}${autoReplyCard()}${overviewCard()}${waitList}
      ${rows ? `<h4>Recent mail</h4><div class="ea-table"><table><thead><tr><th>When (UTC)</th><th>From</th><th>Subject</th><th>Category</th><th>Priority</th><th>Status</th></tr></thead><tbody>${rows}</tbody></table></div>`
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

  function renderBoxes() {
    const boxes = (EA.data && EA.data.boxes) || [];
    const row = el('eaBoxes');
    if (!row) return;
    if (!boxes.length || !boxes[0].address) { row.hidden = true; return; }  // main mailbox not set up yet
    row.hidden = false;
    row.innerHTML = boxes.map((b) => `<button class="ea-chip ${!EA.adding && (b.id || null) === EA.box ? 'is-active' : ''}" data-ea-box="${esc(b.id || '')}"
        aria-pressed="${!EA.adding && (b.id || null) === EA.box}">${esc(b.address || b.id)}</button>`).join('')
      + `<button class="ea-chip ${EA.adding ? 'is-active' : ''}" data-ea-box-add>+ Add mailbox</button>`;
  }

  function render(keepNotice) {
    renderBoxes();
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
        <label class="ea-switch"><input type="checkbox" id="ea_labels" ${EA.draft.labels === false ? '' : 'checked'}> Label each email with its category
          <small>(a tag in Thunderbird; most webmails don't show tags)</small></label>
        <label>Newsletters &amp; notifications <small>— real emails and meeting invites always stay in the Inbox</small>
          <select id="ea_folders"><option value="automated" ${EA.draft.folders !== 'off' ? 'selected' : ''}>Move to the folder “Lisa / ${esc(EA.draft.auto_folder_name || 'Automated')}” (recommended)</option>
            <option value="off" ${EA.draft.folders === 'off' ? 'selected' : ''}>Leave them in the Inbox</option></select></label>
        <div class="ea-grid2">
          <label>Newsletter folder name<input id="ea_auto_folder_name" value="${esc(EA.draft.auto_folder_name || '')}" placeholder="Automated"></label>
          <label>Move newsletters on to the Archive after <small>days, 0 = never</small><input id="ea_auto_archive_days" inputmode="numeric" value="${esc(EA.draft.auto_archive_days ?? 30)}"></label>
          <label>Folder for invoices still to pay <small>— your own folder, e.g. INBOX.Zahlung offen; empty = off</small><input id="ea_invoice_folder" value="${esc(EA.draft.invoice_folder || '')}" placeholder="empty = invoices stay in the Inbox"></label>
        </div>
        <label>Chat messages (Telegram)<select id="ea_notify">
          <option value="actionable" ${EA.draft.notify !== 'all' && EA.draft.notify !== 'off' ? 'selected' : ''}>Only when I need to act (recommended)</option>
          <option value="all" ${EA.draft.notify === 'all' ? 'selected' : ''}>Every processed email</option>
          <option value="off" ${EA.draft.notify === 'off' ? 'selected' : ''}>None — I check the Drafts folder myself</option></select></label>
        <label>Weekly tips &amp; stats <small>— on Mondays: drafts used and unused, and how to make them better</small>
          <select id="ea_review"><option value="7" ${(EA.draft.review_every_days ?? 7) > 0 ? 'selected' : ''}>On (Mondays, recommended)</option>
            <option value="0" ${EA.draft.review_every_days === 0 ? 'selected' : ''}>Off</option></select></label>
        <label>Daily summary at <small>— every morning: all mail still waiting for your reply, most important first. Empty = off.</small>
          <input id="ea_daily_at" type="time" value="${esc(EA.draft.daily_at ?? '07:30')}" style="max-width:160px"></label>
        <div class="ea-card"><label class="ea-switch"><input type="checkbox" id="ea_tasks_on" ${(EA.draft.tasks || {}).enabled ? 'checked' : ''}>
            Create a ClickUp task for important emails</label>
          <div class="ea-grid2">
            <label>ClickUp list ID<input id="ea_tasks_list" inputmode="numeric" value="${esc((EA.draft.tasks || {}).list_id || '')}" placeholder="e.g. 901208765432">
              <small>Open the list in ClickUp: the number in the address after /li/</small></label>
            <label>Assign to (ClickUp user ID)<input id="ea_tasks_user" inputmode="numeric" value="${esc((EA.draft.tasks || {}).assignee || '')}" placeholder="empty = nobody"></label>
            <label>For<select id="ea_tasks_prio"><option value="high" ${(EA.draft.tasks || {}).min_priority !== 'normal' ? 'selected' : ''}>High priority only</option>
              <option value="normal" ${(EA.draft.tasks || {}).min_priority === 'normal' ? 'selected' : ''}>High and normal</option></select></label>
          </div><span class="ea-muted">One task per email (never twice), with what they want, the due date and the webmail link.</span></div>`,
      categories: categoryCards, rules: rulesForm, learned: learnedView }[EA.tab]();
    foot.innerHTML = ['owner', 'categories', 'rules'].includes(EA.tab) ? '<button class="sm-btn ea-primary" data-ea-save>Save</button>' : '<button class="sm-btn" data-ea-refresh>Refresh</button>';
  }

  function collectCurrent() {
    if (el('ea_address') || el('ea_name')) collectOwner();
    if (el('ea_drafts')) EA.draft.drafts = el('ea_drafts').checked;
    if (el('ea_labels')) EA.draft.labels = el('ea_labels').checked;
    if (el('ea_folders')) EA.draft.folders = el('ea_folders').value;
    if (el('ea_auto_folder_name')) EA.draft.auto_folder_name = el('ea_auto_folder_name').value.trim();
    if (el('ea_auto_archive_days')) EA.draft.auto_archive_days = el('ea_auto_archive_days').value.trim();
    if (el('ea_invoice_folder')) EA.draft.invoice_folder = el('ea_invoice_folder').value.trim();
    if (el('ea_notify')) EA.draft.notify = el('ea_notify').value;
    if (el('ea_daily_at')) EA.draft.daily_at = el('ea_daily_at').value;
    if (el('ea_review')) EA.draft.review_every_days = Number(el('ea_review').value);
    if (el('ea_tasks_on')) EA.draft.tasks = { ...(EA.draft.tasks || {}), enabled: el('ea_tasks_on').checked, list_id: el('ea_tasks_list').value.trim(),
      assignee: el('ea_tasks_user').value.trim(), min_priority: el('ea_tasks_prio').value };
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
    if (ds.eaBox !== undefined || ds.eaBoxAdd !== undefined) {  // switch mailbox (unsaved edits ask first)
      if (EA.data.configured && snap() !== EA.saved && !confirm('You have unsaved changes. Switch without saving?')) return;
      EA.adding = ds.eaBoxAdd !== undefined;
      EA.box = EA.adding ? null : (ds.eaBox || null);
      EA.tab = 'status'; EA.suggestedFrom = null; EA.suggestTried = false; EA.checks = null; EA.reconnect = false;
      el('eaBody').innerHTML = '<p class="ea-muted">Loading…</p>';
      try { await load(); render(); } catch (err) { notice((err && err.message) || 'Could not load this mailbox.', true); }
      return;
    }
    if (ds.eaPause !== undefined) {
      if (ds.eaPause === '1' && !confirm('Pause the email assistant? Lisa stops reading this mailbox until you resume.')) return;
      if (await post('/api/email-assistant/config', { config: { ...(EA.data.config || {}), paused: ds.eaPause === '1' } })) render();
      return;
    }
    if (ds.eaAuto !== undefined) {  // the owner's on/off switch for the automatic thank-you
      const on = ds.eaAuto === '1';
      if (on && !confirm('Turn on the automatic thank-you? New applications will get the email shown below, sent from this mailbox.')) return;
      try {
        EA.data = await api('/api/email-assistant/connect', { method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ action: 'auto_reply', box: EA.box, enabled: on }) });
        render(true); notice(on ? 'Automatic thank-you is on.' : 'Automatic thank-you is off. Nothing is sent.', false);
      } catch (err) { notice((err && err.message) || 'Could not change it.', true); }
      return;
    }
    if (ds.eaReconnect !== undefined) { EA.reconnect = true; return render(); }
    if (ds.eaReconnectCancel !== undefined) { EA.reconnect = false; return render(); }
    if (ds.eaConnect !== undefined) {
      const payload = { user: el('ea_c_user').value.trim(), password: el('ea_c_pass').value,
        host: el('ea_c_host').value.trim(), port: el('ea_c_port').value.trim(), name: (el('ea_name') && el('ea_name').value.trim()) || EA.draft.name || '',
        ...(EA.adding ? { box: 'new' } : EA.box ? { box: EA.box } : {}) };
      t.disabled = true; t.textContent = 'Testing…';
      try {
        const res = await api('/api/email-assistant/connect', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload), timeoutMs: 90000 });
        const keep = EA.draft;
        EA.data = res;
        EA.box = res.box || null;
        EA.adding = false;
        EA.draft = { ...keep, address: payload.user };
        EA.reconnect = false;
        render(true);
        EA.checks = { ...(res.connected_folders || {}), host: res.connected_host };
        render(true);
        notice('Connected and tested.', false);
      } catch (err) {
        t.disabled = false; t.textContent = 'Connect and test';
        notice((err && err.message) || 'Could not connect.', true);
      }
      return;
    }
    if (ds.eaSuggest !== undefined) return suggestCategories(t);
    if (ds.eaNext !== undefined) {
      if (EA.step === 0 && !(EA.data.mailbox || {}).connected) return notice('Connect your mailbox first (button above).', true);
      if (EA.step === 0 && !EA.draft.address) return notice('Enter the mailbox address.', true);
      if (EA.step < WIZ.length - 1) {
        EA.step += 1;
        render();
        if (EA.step === 1 && !EA.suggestedFrom && !EA.suggestTried) { EA.suggestTried = true; suggestCategories(el('eaBody').querySelector('[data-ea-suggest]')); }
        return;
      }
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

  // Replace the categories with ones made for this mailbox (wizard: automatic on step 2; later: on request).
  async function suggestCategories(btn) {
    if (EA.data.configured && !confirm('Replace your categories with new suggestions? Nothing changes until you press Save.')) return;
    if (btn) { btn.disabled = true; btn.textContent = 'Lisa is looking at your inbox…'; }
    try {
      const res = await api('/api/email-assistant/connect', { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action: 'suggest', ...(EA.box ? { box: EA.box } : {}) }), timeoutMs: 120000 });
      const old = EA.draft.categories || {};
      EA.draft.categories = res.categories;
      Object.keys(old).forEach((k) => { if (!res.categories[k]) EA.draft.rules = (EA.draft.rules || []).filter((r) => r.category !== k); });
      EA.suggestedFrom = res.based_on;
      EA.openCats = new Set();
      render(true);
      notice(`${Object.keys(res.categories).length} categories suggested.`, false);
    } catch (err) {
      if (btn) { btn.disabled = false; btn.textContent = 'Suggest categories'; }
      notice((err && err.message) || 'No suggestions right now.', true);
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
        <div class="ea-boxes" id="eaBoxes" role="group" aria-label="Mailboxes" hidden></div>
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
  // /#email (link in LISA's Telegram messages) opens the panel directly
  const openFromHash = () => { if (location.hash === '#email') open(); };
  window.addEventListener('hashchange', openFromHash);
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', () => setTimeout(openFromHash, 600));
  else setTimeout(openFromHash, 600);
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', addRailButton);
  else addRailButton();
})();
