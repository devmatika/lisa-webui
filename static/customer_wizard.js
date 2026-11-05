/**
 * Customer Onboarding Wizard for Lisa / Hermes WebUI.
 * Multi-step guided setup for workspace, AI provider, capabilities,
 * service connectors, action approvals, and daily briefings.
 */

const CUSTOMER_WIZARD = {
  active: false,
  step: 0,
  steps: ['company', 'provider', 'capabilities', 'services', 'approvals', 'knowledge', 'briefing', 'finish'],
  stepTitles: [
    'Company & Assistant',
    'AI Model Provider',
    'Choose Capabilities',
    'Connect Services',
    'Action Approvals',
    'Persona & Knowledge',
    'Daily Briefing',
    'Validate & Finish'
  ],
  status: null,
  draft: {
    company: {},
    provider: {},
    capabilities: [],
    services: {},
    approvals: {},
    knowledge: {
      soul: '',
      user: '',
      memory: '',
      obsidian_vault_path: '',
      obsidian_team_link: '',
      activeTab: 'soul'
    },
    briefing: {}
  },
  probes: {},
  validation: null,
  applied: false,
};

// ── Entry Points & Loading ────────────────────────────────────────────────────

async function loadCustomerWizard(options = {}) {
  try {
    const res = await api('/api/customer-wizard/status');
    if (!res) return false;
    CUSTOMER_WIZARD.status = res;

    // Initialize local draft from server status
    CUSTOMER_WIZARD.draft.company = { ...res.company };
    CUSTOMER_WIZARD.draft.provider = {
      provider: res.provider.provider || 'openrouter',
      model: res.provider.model || '',
      base_url: res.provider.base_url || '',
      api_key: '',
    };
    CUSTOMER_WIZARD.draft.capabilities = (res.capabilities || [])
      .filter(c => c.selected)
      .map(c => c.id);
    CUSTOMER_WIZARD.draft.approvals = {};
    (res.approvals || []).forEach(a => {
      CUSTOMER_WIZARD.draft.approvals[a.id] = a.current || a.default;
    });
    CUSTOMER_WIZARD.draft.knowledge = {
      ...(res.knowledge || {}),
      team_members: (res.knowledge && res.knowledge.team_members) || res.team_members || [],
      activeTab: (CUSTOMER_WIZARD.draft.knowledge && CUSTOMER_WIZARD.draft.knowledge.activeTab) || 'team'
    };
    CUSTOMER_WIZARD.draft.briefing = { ...res.briefing };

    if (!options.force) {
      // If already configured or completed, don't pop up automatically
      if (res.completed || res.is_configured) {
        return false;
      }
    }

    openCustomerWizard();
    return true;
  } catch (err) {
    console.warn('Customer wizard status check failed:', err);
    return false;
  }
}

function openCustomerWizard() {
  CUSTOMER_WIZARD.active = true;
  CUSTOMER_WIZARD.applied = false;
  const overlay = $('customerWizardOverlay');
  if (overlay) {
    overlay.style.display = 'flex';
  }
  _renderCustomerWizardSteps();
  _renderCustomerWizardBody();
}

function closeCustomerWizard() {
  CUSTOMER_WIZARD.active = false;
  const overlay = $('customerWizardOverlay');
  if (overlay) {
    overlay.style.display = 'none';
  }
}

async function runSetupWizardAgain() {
  try {
    await api('/api/customer-wizard/reset', { method: 'POST', body: '{}' });
    await loadCustomerWizard({ force: true });
    showToast('Setup wizard reset. Opening setup…');
  } catch (err) {
    showToast('Failed to reset setup wizard: ' + err.message);
  }
}

// ── Step Navigation & Auto-Saving ─────────────────────────────────────────────

async function _saveCurrentStepDraft() {
  const currentStepName = CUSTOMER_WIZARD.steps[CUSTOMER_WIZARD.step];
  if (!currentStepName || currentStepName === 'finish') return;
  _collectCurrentStepData(currentStepName);
  try {
    const dataToSend = CUSTOMER_WIZARD.draft[currentStepName] || {};
    await api('/api/customer-wizard/step', {
      method: 'POST',
      body: JSON.stringify({
        step: currentStepName,
        data: dataToSend
      })
    });
  } catch (err) {
    console.warn('Auto-save step draft failed:', err);
  }
}

async function prevCustomerWizardStep() {
  if (CUSTOMER_WIZARD.step > 0) {
    await _saveCurrentStepDraft();
    CUSTOMER_WIZARD.step--;
    _renderCustomerWizardSteps();
    _renderCustomerWizardBody();
  }
}

async function nextCustomerWizardStep() {
  const currentStepName = CUSTOMER_WIZARD.steps[CUSTOMER_WIZARD.step];
  const nextBtn = $('customerWizardNextBtn');
  if (nextBtn) nextBtn.disabled = true;

  try {
    // Save draft step progress to backend
    await _saveCurrentStepDraft();

    if (CUSTOMER_WIZARD.step < CUSTOMER_WIZARD.steps.length - 1) {
      CUSTOMER_WIZARD.step++;
      _renderCustomerWizardSteps();
      _renderCustomerWizardBody();
    } else {
      // On the final step, apply changes
      await _applyCustomerWizard();
    }
  } catch (err) {
    _showWizardNotice(err.message || String(err), 'error');
  } finally {
    if (nextBtn) nextBtn.disabled = false;
  }
}

async function skipCustomerWizardStep() {
  if (CUSTOMER_WIZARD.step < CUSTOMER_WIZARD.steps.length - 1) {
    await _saveCurrentStepDraft();
    CUSTOMER_WIZARD.step++;
    _renderCustomerWizardSteps();
    _renderCustomerWizardBody();
  }
}

function _collectCurrentStepData(stepName) {
  if (stepName === 'company') {
    CUSTOMER_WIZARD.draft.company = {
      company_name: ($('cwCompany') ? $('cwCompany').value : '').trim(),
      assistant_name: ($('cwAssistant') ? $('cwAssistant').value : '').trim() || 'Lisa',
      admin_name: ($('cwAdmin') ? $('cwAdmin').value : '').trim() || 'Admin',
      language: ($('cwLanguage') ? $('cwLanguage').value : 'en'),
      timezone: ($('cwTimezone') ? $('cwTimezone').value : '').trim() || 'Europe/Vienna',
      logo: ($('cwLogo') ? $('cwLogo').value : '').trim(),
    };
  } else if (stepName === 'provider') {
    CUSTOMER_WIZARD.draft.provider = {
      provider: ($('cwProvider') ? $('cwProvider').value : 'openrouter'),
      model: ($('cwModel') ? $('cwModel').value : '').trim(),
      base_url: ($('cwBaseUrl') ? $('cwBaseUrl').value : '').trim(),
      api_key: ($('cwApiKey') ? $('cwApiKey').value : '').trim(),
    };
  } else if (stepName === 'capabilities') {
    CUSTOMER_WIZARD.draft.capabilities = _getSelectedCheckboxes('cwCapabilities');
  } else if (stepName === 'approvals') {
    // approvals are updated in CUSTOMER_WIZARD.draft.approvals on change
  } else if (stepName === 'knowledge') {
    CUSTOMER_WIZARD.draft.knowledge = CUSTOMER_WIZARD.draft.knowledge || {};
    if ($('cwSoulEditor')) CUSTOMER_WIZARD.draft.knowledge.soul = $('cwSoulEditor').value;
    if ($('cwUserEditor')) CUSTOMER_WIZARD.draft.knowledge.user = $('cwUserEditor').value;
    if ($('cwMemoryEditor')) CUSTOMER_WIZARD.draft.knowledge.memory = $('cwMemoryEditor').value;
    if ($('cwObsidianPath')) CUSTOMER_WIZARD.draft.knowledge.obsidian_vault_path = $('cwObsidianPath').value.trim();
    if ($('cwObsidianTeamLink')) CUSTOMER_WIZARD.draft.knowledge.obsidian_team_link = $('cwObsidianTeamLink').value.trim();
  } else if (stepName === 'briefing') {
    CUSTOMER_WIZARD.draft.briefing = {
      enabled: ($('cwBriefingEnabled') ? $('cwBriefingEnabled').checked : false),
      channel: ($('cwBriefingChannel') ? $('cwBriefingChannel').value : 'telegram'),
      recipient: ($('cwBriefingRecipient') ? $('cwBriefingRecipient').value : '').trim(),
      time: ($('cwBriefingTime') ? $('cwBriefingTime').value : '08:30').trim(),
      timezone: ($('cwBriefingTz') ? $('cwBriefingTz').value : 'Europe/Vienna').trim(),
      topics: _getSelectedCheckboxes('cwBriefingTopics'),
    };
  }
}

function _getSelectedCheckboxes(groupName) {
  const nodes = document.querySelectorAll(`input[name="${groupName}"]:checked`);
  return Array.from(nodes).map(n => n.value);
}

// ── Rendering Sidebar & Steps ─────────────────────────────────────────────────

function _renderCustomerWizardSteps() {
  const container = $('customerWizardSteps');
  if (!container) return;

  container.innerHTML = CUSTOMER_WIZARD.steps.map((s, idx) => {
    const isCurrent = idx === CUSTOMER_WIZARD.step;
    const isDone = idx < CUSTOMER_WIZARD.step;
    const cls = isCurrent ? 'cw-step-item active' : (isDone ? 'cw-step-item done' : 'cw-step-item');
    const badge = isDone ? '✓' : String(idx + 1);
    return `
      <div class="${cls}" onclick="if(${isDone}){_saveCurrentStepDraft().then(()=>{CUSTOMER_WIZARD.step=${idx};_renderCustomerWizardSteps();_renderCustomerWizardBody();});}">
        <span class="cw-step-num">${badge}</span>
        <span class="cw-step-title">${CUSTOMER_WIZARD.stepTitles[idx]}</span>
      </div>
    `;
  }).join('');

  // Update Buttons
  const backBtn = $('customerWizardBackBtn');
  const nextBtn = $('customerWizardNextBtn');
  const skipBtn = $('customerWizardSkipBtn');

  if (backBtn) {
    backBtn.style.display = CUSTOMER_WIZARD.step > 0 && !CUSTOMER_WIZARD.applied ? 'inline-block' : 'none';
  }
  if (skipBtn) {
    const canSkip = ['services', 'briefing'].includes(CUSTOMER_WIZARD.steps[CUSTOMER_WIZARD.step]);
    skipBtn.style.display = canSkip && !CUSTOMER_WIZARD.applied ? 'inline-block' : 'none';
  }
  if (nextBtn) {
    if (CUSTOMER_WIZARD.applied) {
      nextBtn.textContent = 'Start Chat';
      nextBtn.onclick = () => { closeCustomerWizard(); if (typeof switchPanel === 'function') switchPanel('chat'); };
    } else if (CUSTOMER_WIZARD.step === CUSTOMER_WIZARD.steps.length - 1) {
      nextBtn.textContent = 'Apply & Finish';
      nextBtn.onclick = () => nextCustomerWizardStep();
    } else {
      nextBtn.textContent = 'Continue';
      nextBtn.onclick = () => nextCustomerWizardStep();
    }
  }
}

function _showWizardNotice(msg, type = 'info') {
  const notice = $('customerWizardNotice');
  if (!notice) return;
  if (!msg) {
    notice.style.display = 'none';
    notice.innerHTML = '';
    return;
  }
  notice.style.display = 'block';
  notice.className = `customer-wizard-status cw-notice-${type}`;
  notice.innerHTML = `<div>${msg}</div>`;
}

// ── Rendering Step Bodies ─────────────────────────────────────────────────────

function _renderCustomerWizardBody() {
  const body = $('customerWizardBody');
  if (!body) return;
  _showWizardNotice('', 'info');

  const stepName = CUSTOMER_WIZARD.steps[CUSTOMER_WIZARD.step];
  if (stepName === 'company') {
    _renderStepCompany(body);
  } else if (stepName === 'provider') {
    _renderStepProvider(body);
  } else if (stepName === 'capabilities') {
    _renderStepCapabilities(body);
  } else if (stepName === 'services') {
    _renderStepServices(body);
  } else if (stepName === 'approvals') {
    _renderStepApprovals(body);
  } else if (stepName === 'knowledge') {
    _renderStepKnowledge(body);
  } else if (stepName === 'briefing') {
    _renderStepBriefing(body);
  } else if (stepName === 'finish') {
    _renderStepFinish(body);
  }
}

// ── Step A: Company and Assistant ─────────────────────────────────────────────

function _renderStepCompany(el) {
  const d = CUSTOMER_WIZARD.draft.company;
  el.innerHTML = `
    <div class="cw-step-pane">
      <div class="cw-pane-head">
        <h3>Company & Assistant Profile</h3>
        <p>Set up organization branding, your AI assistant name, and primary timezone.</p>
      </div>

      <div class="cw-form-group">
        <label>Company Name <span class="cw-req">*</span></label>
        <input type="text" id="cwCompany" value="${_escapeHtml(d.company_name || 'Matika')}" placeholder="e.g. Acme Corporation" required>
      </div>

      <div class="cw-form-row">
        <div class="cw-form-group">
          <label>Assistant Name <span class="cw-req">*</span></label>
          <input type="text" id="cwAssistant" value="${_escapeHtml(d.assistant_name || 'Lisa')}" placeholder="Lisa" required>
        </div>
        <div class="cw-form-group">
          <label>Admin Display Name</label>
          <input type="text" id="cwAdmin" value="${_escapeHtml(d.admin_name || 'Admin')}" placeholder="e.g. Sarah">
        </div>
      </div>

      <div class="cw-form-row">
        <div class="cw-form-group">
          <label>Preferred Language</label>
          <select id="cwLanguage">
            <option value="en" ${d.language === 'en' ? 'selected' : ''}>English (en)</option>
            <option value="de" ${d.language === 'de' ? 'selected' : ''}>Deutsch (de)</option>
            <option value="fa" ${d.language === 'fa' ? 'selected' : ''}>فارسی (fa)</option>
          </select>
        </div>
        <div class="cw-form-group">
          <label>IANA Timezone <span class="cw-req">*</span></label>
          <input type="text" id="cwTimezone" value="${_escapeHtml(d.timezone || 'Europe/Vienna')}" placeholder="Europe/Vienna">
        </div>
      </div>

      <div class="cw-form-group">
        <label>Company Logo URL / Path <span class="cw-opt">(optional)</span></label>
        <input type="text" id="cwLogo" value="${_escapeHtml(d.logo || '')}" placeholder="static/matika-logo.png or https://...">
      </div>
    </div>
  `;
}

// ── Step B: AI Provider ───────────────────────────────────────────────────────

function _renderStepProvider(el) {
  const p = CUSTOMER_WIZARD.draft.provider;
  const statusProvider = (CUSTOMER_WIZARD.status && CUSTOMER_WIZARD.status.provider) || {};
  const supported = statusProvider.supported_providers || [];
  const selectedMeta = supported.find(s => s.id === p.provider) || {};

  const models = selectedMeta.models || [];
  const keyPlaceholder = statusProvider.is_configured ? '•••••••• (Configured — leave blank to keep)' : 'Enter API key';

  el.innerHTML = `
    <div class="cw-step-pane">
      <div class="cw-pane-head">
        <h3>AI Model Provider</h3>
        <p>Configure model access for intelligent reasoning and tool execution.</p>
      </div>

      <div class="cw-form-group">
        <label>Provider</label>
        <select id="cwProvider" onchange="CUSTOMER_WIZARD.draft.provider.provider=this.value;_renderCustomerWizardBody();">
          ${supported.map(s => `<option value="${s.id}" ${s.id === p.provider ? 'selected' : ''}>${s.label}</option>`).join('')}
        </select>
      </div>

      <div class="cw-form-group">
        <label>Default Model <span class="cw-req">*</span></label>
        ${models.length > 0 ? `
          <select id="cwModel">
            ${models.map(m => `<option value="${m.id}" ${m.id === p.model ? 'selected' : ''}>${m.label || m.id}</option>`).join('')}
          </select>
        ` : `
          <input type="text" id="cwModel" value="${_escapeHtml(p.model || selectedMeta.default_model || '')}" placeholder="e.g. gpt-4o">
        `}
      </div>

      ${selectedMeta.requires_base_url ? `
        <div class="cw-form-group">
          <label>Base URL <span class="cw-req">*</span></label>
          <input type="text" id="cwBaseUrl" value="${_escapeHtml(p.base_url || '')}" placeholder="http://localhost:11434/v1">
        </div>
      ` : ''}

      <div class="cw-form-group">
        <label>API Key ${selectedMeta.key_optional ? '<span class="cw-opt">(optional for local)</span>' : '<span class="cw-req">*</span>'}</label>
        <input type="password" id="cwApiKey" placeholder="${keyPlaceholder}" autocomplete="new-password">
      </div>

      <div class="cw-probe-box">
        <button type="button" class="sm-btn" onclick="_testProviderConnection()">Test Connection</button>
        <span id="cwProviderProbeStatus" class="cw-probe-msg"></span>
      </div>
    </div>
  `;
}

async function _testProviderConnection() {
  const statusSpan = $('cwProviderProbeStatus');
  if (statusSpan) {
    statusSpan.innerHTML = '<span class="cw-spinner"></span> Connecting…';
    statusSpan.className = 'cw-probe-msg probing';
  }

  const provider = ($('cwProvider') ? $('cwProvider').value : 'openrouter');
  const baseUrl = ($('cwBaseUrl') ? $('cwBaseUrl').value : '').trim();
  const apiKey = ($('cwApiKey') ? $('cwApiKey').value : '').trim();

  try {
    const res = await api('/api/onboarding/probe', {
      method: 'POST',
      body: JSON.stringify({ provider, base_url: baseUrl, api_key: apiKey || undefined })
    });
    if (res && res.ok) {
      statusSpan.className = 'cw-probe-msg ok';
      statusSpan.textContent = `✓ Connected. ${Array.isArray(res.models) ? res.models.length : 0} models available.`;
    } else {
      statusSpan.className = 'cw-probe-msg error';
      statusSpan.textContent = `✕ Connection failed: ${res.detail || res.error || 'unreachable'}`;
    }
  } catch (err) {
    statusSpan.className = 'cw-probe-msg error';
    statusSpan.textContent = `✕ Probe error: ${err.message}`;
  }
}

// ── Step C: Choose Capabilities ───────────────────────────────────────────────

function _renderStepCapabilities(el) {
  const caps = (CUSTOMER_WIZARD.status && CUSTOMER_WIZARD.status.capabilities) || [];
  const selected = new Set(CUSTOMER_WIZARD.draft.capabilities || []);

  el.innerHTML = `
    <div class="cw-step-pane">
      <div class="cw-pane-head">
        <h3>Choose Assistant Capabilities</h3>
        <p>Enable specialized capabilities for your business. Verified dependencies are reflected below.</p>
      </div>

      <div class="cw-cap-grid">
        ${caps.map(c => {
          const isChecked = selected.has(c.id);
          const badgeCls = `cw-badge-${c.status}`;
          return `
            <div class="cw-cap-card ${isChecked ? 'selected' : ''}" onclick="_toggleCapability('${c.id}', event)">
              <div class="cw-cap-header">
                <input type="checkbox" id="cap_${c.id}" value="${c.id}" ${isChecked ? 'checked' : ''} onclick="event.stopPropagation();_toggleCapability('${c.id}')">
                <label for="cap_${c.id}" class="cw-cap-title">${_escapeHtml(c.title)}</label>
                <span class="cw-status-badge ${badgeCls}">${_formatStatus(c.status)}</span>
              </div>
              <p class="cw-cap-desc">${_escapeHtml(c.description)}</p>
              <div class="cw-cap-deps">
                <small>Requires: ${c.required_integrations.join(', ')}</small>
              </div>
            </div>
          `;
        }).join('')}
      </div>
    </div>
  `;
}

function _toggleCapability(id, evt) {
  if (evt && evt.target && evt.target.tagName === 'INPUT') return;
  const set = new Set(CUSTOMER_WIZARD.draft.capabilities || []);
  if (set.has(id)) set.delete(id);
  else set.add(id);
  CUSTOMER_WIZARD.draft.capabilities = Array.from(set);
  _renderCustomerWizardBody();
}

function _getServiceIcon(id) {
  const icons = {
    google_workspace: '🗓️',
    clickup: '📋',
    onyx: '🧠',
    fireflies: '🎙️',
    microsoft_graph: '📧',
    telegram: '✈️',
    mattermost: '💬',
  };
  return icons[id] || '🔌';
}

function _toggleServiceCard(serviceId) {
  CUSTOMER_WIZARD.expandedServices = CUSTOMER_WIZARD.expandedServices || {};
  CUSTOMER_WIZARD.expandedServices[serviceId] = !CUSTOMER_WIZARD.expandedServices[serviceId];
  _renderCustomerWizardBody();
}

function _handleServiceFileSelected(serviceId, inputEl) {
  if (!inputEl || !inputEl.files || !inputEl.files.length) return;
  const file = inputEl.files[0];
  const reader = new FileReader();
  reader.onload = function(e) {
    CUSTOMER_WIZARD.stagedFiles = CUSTOMER_WIZARD.stagedFiles || {};
    CUSTOMER_WIZARD.stagedFiles[serviceId] = CUSTOMER_WIZARD.stagedFiles[serviceId] || {};
    CUSTOMER_WIZARD.stagedFiles[serviceId][file.name] = e.target.result;

    const dropzone = $(`dropzone_label_${serviceId}`);
    if (dropzone) {
      dropzone.className = 'cw-dropzone-inner file-selected';
      dropzone.innerHTML = `✓ <strong>${_escapeHtml(file.name)}</strong> (${Math.round(file.size / 1024)} KB) ready to save!`;
    }
    _showWizardNotice(`File "${file.name}" staged for ${serviceId}. Click "Save & Connect" to activate.`, 'info');
  };
  reader.readAsText(file);
}

// ── Step D: Connect Services ──────────────────────────────────────────────────

function _renderStepServices(el) {
  const services = (CUSTOMER_WIZARD.status && CUSTOMER_WIZARD.status.services) || [];
  CUSTOMER_WIZARD.expandedServices = CUSTOMER_WIZARD.expandedServices || { google_workspace: true };
  CUSTOMER_WIZARD.stagedFiles = CUSTOMER_WIZARD.stagedFiles || {};

  el.innerHTML = `
    <div class="cw-step-pane">
      <div class="cw-pane-head">
        <h3>Connect Business Services</h3>
        <p>Configure credentials and upload token files directly below. Settings and keys are encrypted and saved to your private environment.</p>
      </div>

      <div class="cw-services-list">
        ${services.map(s => {
          const probe = CUSTOMER_WIZARD.probes[s.id] || {};
          const isExpanded = !!CUSTOMER_WIZARD.expandedServices[s.id];
          const isVerified = (s.status === 'verified' || (probe.ok && (probe.status === 'verified' || probe.ok)));
          const isConfigured = (s.status === 'configured' || isVerified || probe.ok);
          const effectiveStatus = isVerified ? 'verified' : (isConfigured ? 'configured' : (probe.status === 'error' ? 'error' : s.status));
          const statusCls = `cw-badge-${effectiveStatus}`;
          const currentVals = s.current_values || {};
          const stagedForService = CUSTOMER_WIZARD.stagedFiles[s.id] || {};
          const stagedNames = Object.keys(stagedForService);
          const displayMsg = (probe.message !== undefined ? probe.message : s.message) || '';
          const barOk = isVerified || (probe.ok === true);
          const barError = (!probe.ok && probe.status === 'error') || (s.status === 'error');

          return `
            <div class="cw-service-card ${isVerified ? 'is-verified' : ''}" id="card_${s.id}">
              <div class="cw-service-top" onclick="_toggleServiceCard('${s.id}')">
                <div class="cw-service-info">
                  <div class="cw-service-title-row">
                    <span class="cw-service-icon">${_getServiceIcon(s.id)}</span>
                    <span class="cw-service-name">${_escapeHtml(s.name)}</span>
                  </div>
                  <div class="cw-service-desc">${_escapeHtml(s.description || '')}</div>
                </div>
                <div class="cw-service-badge-col">
                  <span class="cw-status-badge ${statusCls}">${_formatStatus(effectiveStatus)}</span>
                  <button type="button" class="cw-toggle-btn" title="Toggle Configuration Form" onclick="event.stopPropagation();_toggleServiceCard('${s.id}')">
                    ${isExpanded ? '▲ Hide' : '▼ Configure'}
                  </button>
                </div>
              </div>

              ${displayMsg ? `
                <div class="cw-service-status-bar ${barOk ? 'ok' : (barError ? 'error' : '')}">
                  <span>${barOk ? '✓' : (barError ? '✕' : 'ℹ')}</span>
                  <span>${_escapeHtml(displayMsg)}</span>
                </div>
              ` : ''}

              ${s.supported ? `
                <div class="cw-service-config-body" style="display: ${isExpanded ? 'flex' : 'none'};">
                  ${s.supports_file_upload ? `
                    <div class="cw-file-upload-section">
                      <div class="cw-file-upload-title">Credentials File (${(s.accepted_files || []).join(', ')})</div>
                      <small class="cw-hint">${_escapeHtml(s.upload_hint || '')}</small>
                      
                      ${s.configured_files && s.configured_files.length ? `
                        <div class="cw-file-present-tag">✓ Present on server: ${s.configured_files.join(', ')}</div>
                      ` : ''}

                      <div class="cw-dropzone" onclick="document.getElementById('file_input_${s.id}').click()">
                        <input type="file" id="file_input_${s.id}" accept=".json" style="display:none" onchange="_handleServiceFileSelected('${s.id}', this)">
                        <div class="cw-dropzone-inner ${stagedNames.length ? 'file-selected' : ''}" id="dropzone_label_${s.id}">
                          ${stagedNames.length ? `
                            ✓ <strong>${_escapeHtml(stagedNames.join(', '))}</strong> selected and ready to save
                          ` : `
                            📁 <span>Click to select credentials.json or google_token.json from your computer</span>
                          `}
                        </div>
                      </div>
                    </div>
                  ` : ''}

                  ${(s.fields && s.fields.length) ? `
                    <div class="cw-service-fields-grid">
                      ${s.fields.map(f => {
                        const val = currentVals[f.id] || '';
                        return `
                          <div class="cw-form-group" style="margin-bottom: 10px;">
                            <label>
                              ${_escapeHtml(f.label)}
                              ${f.required ? '<span class="cw-req">*</span>' : '<span class="cw-opt">(optional)</span>'}
                            </label>
                            <input
                              type="${f.type || 'text'}"
                              id="field_${s.id}_${f.id}"
                              value="${_escapeHtml(val)}"
                              placeholder="${_escapeHtml(f.placeholder || '')}"
                              autocomplete="off"
                            >
                            ${f.hint ? `<small class="cw-hint">${_escapeHtml(f.hint)}</small>` : ''}
                          </div>
                        `;
                      }).join('')}
                    </div>
                  ` : ''}

                  ${(s.id === 'google_workspace') ? `
                    <div class="cw-oauth-action-box" id="google_auth_box" style="margin: 14px 0; padding: 14px; border: 1px solid var(--border-color, #444); border-radius: 8px; background: rgba(0, 110, 179, 0.08);">
                      <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom: 8px;">
                        <div style="font-weight:600; font-size:14px; color:var(--text-bright,#fff);">
                          🔑 Google Workspace Account Authorization
                        </div>
                        <span class="cw-status-badge ${isVerified ? 'cw-badge-verified' : 'cw-badge-needs_auth'}">
                          ${isVerified ? '✓ Authorized' : '⚠️ User Authorization Required'}
                        </span>
                      </div>
                      <p class="cw-hint" style="margin-bottom:12px;">
                        ${isVerified ?
                          'Your Google Workspace account is connected and verified. You can generate a new link below anytime to refresh consent.' :
                          'Client credentials are saved. Click the button below to sign in with your Google Workspace account and grant Calendar & Gmail permissions.'
                        }
                      </p>

                      <div style="display:flex; gap:10px; align-items:center; flex-wrap:wrap;">
                        <button type="button" class="btn primary sm-btn" id="btn_get_google_auth" onclick="_getGoogleOAuthUrl()">
                          🔗 1. Get Google Authorization Link
                        </button>
                        <span class="cw-hint">Opens in a new browser tab</span>
                      </div>

                      <div id="google_auth_step_instructions" style="display:none; margin-top:12px; padding:12px; background:rgba(0,0,0,0.25); border-radius:6px;">
                        <ol style="margin:0 0 10px 18px; padding:0; font-size:13px; line-height:1.5;">
                          <li>Visit the authorization link: <a id="google_auth_url_link" href="#" target="_blank" rel="noopener" style="color:var(--primary,#3b82f6); font-weight:600; text-decoration:underline;">Click here to sign in with Google ↗</a></li>
                          <li>Sign in with your Google Workspace account and click <strong>Allow</strong> to grant Calendar & Gmail permissions.</li>
                          <li>When redirected to <code>http://localhost:1/?code=...</code>, copy the <strong>code</strong> (or the entire address bar URL) and paste it below:</li>
                        </ol>
                        <div style="display:flex; gap:8px; align-items:center;">
                          <input type="text" id="google_auth_code_input" placeholder="Paste code (or http://localhost:1/?code=...)" style="flex:1;" />
                          <button type="button" class="btn success sm-btn" id="btn_submit_google_code" onclick="_submitGoogleOAuthCode()">
                            ✓ 2. Complete Authorization
                          </button>
                        </div>
                        <div id="google_auth_exchange_msg" style="margin-top:8px;"></div>
                      </div>
                    </div>
                  ` : ''}

                  <div class="cw-service-actions-bar">
                    <button type="button" class="btn primary sm-btn" id="btn_save_${s.id}" onclick="_saveAndConnectService('${s.id}')">
                      Save & Connect
                    </button>
                    <button type="button" class="sm-btn" onclick="_testServiceConnection('${s.id}')">
                      Test Connection
                    </button>
                    <span class="cw-service-msg" id="msg_${s.id}"></span>
                  </div>
                </div>
              ` : `
                <div class="cw-service-unsupported">
                  <small>⚠️ ${_escapeHtml(s.message)}</small>
                </div>
              `}
            </div>
          `;
        }).join('')}
      </div>
    </div>
  `;
}

async function _getGoogleOAuthUrl() {
  const btn = document.getElementById('btn_get_google_auth');
  if (btn) btn.disabled = true;
  try {
    const res = await fetch('/api/customer-wizard/google-auth-url', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ redirect_uri: 'http://localhost:1' }),
    });
    const data = await res.json();
    if (!data.ok || !data.auth_url) {
      alert(data.message || 'Failed to generate Google Authorization URL. Make sure credentials.json or client_secret_*.json is uploaded.');
      if (btn) btn.disabled = false;
      return;
    }
    const container = document.getElementById('google_auth_step_instructions');
    const link = document.getElementById('google_auth_url_link');
    if (link) link.href = data.auth_url;
    if (container) container.style.display = 'block';
    window.open(data.auth_url, '_blank');
  } catch (err) {
    alert('Error generating Google OAuth URL: ' + err.message);
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function _submitGoogleOAuthCode() {
  const input = document.getElementById('google_auth_code_input');
  const code = (input ? input.value : '').trim();
  const msgEl = document.getElementById('google_auth_exchange_msg');
  const btn = document.getElementById('btn_submit_google_code');
  if (!code) {
    if (msgEl) msgEl.innerHTML = '<span style="color:var(--error,#ff5555)">Please paste the authorization code or redirect URL first.</span>';
    return;
  }
  if (btn) btn.disabled = true;
  if (msgEl) msgEl.innerHTML = '<span class="cw-spinner"></span> Exchanging code and verifying Google connection…';

  try {
    const res = await fetch('/api/customer-wizard/google-auth-code', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ code }),
    });
    const data = await res.json();
    if (data.ok) {
      if (msgEl) msgEl.innerHTML = `<span style="color:var(--success,#44bb44)">✓ ${data.message}</span>`;
      await _testServiceConnection('google_workspace');
      await loadCustomerWizard({ force: true, preserveStep: true });
    } else {
      if (btn) btn.disabled = false;
      if (msgEl) msgEl.innerHTML = `<span style="color:var(--error,#ff5555)">✕ ${data.message}</span>`;
    }
  } catch (err) {
    if (btn) btn.disabled = false;
    if (msgEl) msgEl.innerHTML = `<span style="color:var(--error,#ff5555)">✕ Error: ${err.message}</span>`;
  }
}

async function _saveAndConnectService(serviceId) {
  const s = (CUSTOMER_WIZARD.status && CUSTOMER_WIZARD.status.services || []).find(x => x.id === serviceId);
  if (!s) return;

  const btn = $(`btn_save_${serviceId}`);
  const msgEl = $(`msg_${serviceId}`);
  if (btn) btn.disabled = true;
  if (msgEl) {
    msgEl.className = 'cw-service-msg';
    msgEl.innerHTML = '<span class="cw-spinner"></span> Saving credentials & probing connection…';
  }

  // Collect field inputs
  const envVars = {};
  (s.fields || []).forEach(f => {
    const inp = $(`field_${serviceId}_${f.id}`);
    if (inp && inp.value.trim()) {
      envVars[f.id] = inp.value.trim();
    }
  });

  // Collect staged files
  const files = (CUSTOMER_WIZARD.stagedFiles && CUSTOMER_WIZARD.stagedFiles[serviceId]) || {};

  try {
    const res = await api('/api/customer-wizard/configure-service', {
      method: 'POST',
      body: JSON.stringify({
        service_id: serviceId,
        env_vars: envVars,
        files: files,
      })
    });

    CUSTOMER_WIZARD.probes[serviceId] = res;

    // Refresh overall status to sync capabilities and service badges
    const latestStatus = await api('/api/customer-wizard/status');
    if (latestStatus && latestStatus.services) {
      CUSTOMER_WIZARD.status = latestStatus;
    }

    if (btn) btn.disabled = false;
    if (res.ok) {
      // Clear staged files as they are saved now
      if (CUSTOMER_WIZARD.stagedFiles) {
        CUSTOMER_WIZARD.stagedFiles[serviceId] = {};
      }
      _showWizardNotice(`✓ ${s.name} configured and successfully connected!`, 'success');
    } else {
      _showWizardNotice(`Connection attempt failed for ${s.name}: ${res.message || res.detail || ''}`, 'warn');
    }

    // Keep card expanded
    CUSTOMER_WIZARD.expandedServices = CUSTOMER_WIZARD.expandedServices || {};
    CUSTOMER_WIZARD.expandedServices[serviceId] = true;

    _renderCustomerWizardBody();

    const newMsgEl = $(`msg_${serviceId}`);
    if (newMsgEl) {
      newMsgEl.className = 'cw-service-msg ' + (res.ok ? 'ok' : 'error');
      newMsgEl.textContent = res.message || (res.ok ? 'Connected & Verified' : 'Failed');
    }
  } catch (err) {
    if (btn) btn.disabled = false;
    if (msgEl) {
      msgEl.className = 'cw-service-msg error';
      msgEl.textContent = 'Save error: ' + err.message;
    }
    _showWizardNotice(`Error saving ${s.name}: ${err.message}`, 'error');
  }
}

async function _testServiceConnection(serviceId) {
  const msgEl = $(`msg_${serviceId}`);
  if (msgEl) {
    msgEl.className = 'cw-service-msg';
    msgEl.innerHTML = '<span class="cw-spinner"></span> Testing connection…';
  }

  try {
    const res = await api('/api/customer-wizard/probe', {
      method: 'POST',
      body: JSON.stringify({ service_id: serviceId })
    });
    CUSTOMER_WIZARD.probes[serviceId] = res;

    const latestStatus = await api('/api/customer-wizard/status');
    if (latestStatus && latestStatus.services) {
      CUSTOMER_WIZARD.status = latestStatus;
    }

    // Keep card expanded
    CUSTOMER_WIZARD.expandedServices = CUSTOMER_WIZARD.expandedServices || {};
    CUSTOMER_WIZARD.expandedServices[serviceId] = true;

    _renderCustomerWizardBody();

    const newMsgEl = $(`msg_${serviceId}`);
    if (newMsgEl) {
      newMsgEl.className = 'cw-service-msg ' + (res.ok ? 'ok' : 'error');
      newMsgEl.textContent = res.message || (res.ok ? 'Connection Verified' : 'Failed');
    }
  } catch (err) {
    if (msgEl) {
      msgEl.className = 'cw-service-msg error';
      msgEl.textContent = 'Probe error: ' + err.message;
    }
  }
}

// ── Step E: Permissions & Approvals ───────────────────────────────────────────

function _renderStepApprovals(el) {
  const approvals = (CUSTOMER_WIZARD.status && CUSTOMER_WIZARD.status.approvals) || [];
  const current = CUSTOMER_WIZARD.draft.approvals || {};

  el.innerHTML = `
    <div class="cw-step-pane">
      <div class="cw-pane-head" style="display:flex; justify-content:space-between; align-items:flex-start; flex-wrap:wrap; gap:10px;">
        <div>
          <h3>Action Permissions & Human Approvals</h3>
          <p>Define which business operations require explicit human review before execution.</p>
        </div>
        <button type="button" class="btn sm-btn secondary" onclick="_resetApprovalsToDefault()" title="Reset all actions to recommended security defaults" style="white-space:nowrap;">
          ↺ Reset to Defaults
        </button>
      </div>

      <div class="cw-approvals-table">
        <div class="cw-approval-header-row">
          <span>Action</span>
          <span>Policy</span>
          <span>Enforcement Layer</span>
        </div>
        ${approvals.map(a => {
          const val = current[a.id] || a.current || a.default;
          return `
            <div class="cw-approval-row">
              <div class="cw-approval-action">
                <strong>${_escapeHtml(a.category)}</strong>: ${_escapeHtml(a.action)}
              </div>
              <div class="cw-approval-segmented">
                <label class="${val === 'automatic' ? 'selected' : ''}">
                  <input type="radio" name="app_${a.id}" value="automatic" ${val === 'automatic' ? 'checked' : ''} onchange="_setApprovalPolicy('${a.id}', 'automatic')"> Auto
                </label>
                <label class="${val === 'ask' ? 'selected' : ''}">
                  <input type="radio" name="app_${a.id}" value="ask" ${val === 'ask' ? 'checked' : ''} onchange="_setApprovalPolicy('${a.id}', 'ask')"> Ask
                </label>
                <label class="${val === 'disabled' ? 'selected' : ''}">
                  <input type="radio" name="app_${a.id}" value="disabled" ${val === 'disabled' ? 'checked' : ''} onchange="_setApprovalPolicy('${a.id}', 'disabled')"> Disabled
                </label>
              </div>
              <div class="cw-approval-handler">
                <small>${_escapeHtml(a.handler)}</small>
              </div>
            </div>
          `;
        }).join('')}
      </div>
    </div>
  `;
}

function _setApprovalPolicy(id, value) {
  CUSTOMER_WIZARD.draft.approvals = CUSTOMER_WIZARD.draft.approvals || {};
  CUSTOMER_WIZARD.draft.approvals[id] = value;
  _renderCustomerWizardBody();
  _saveCurrentStepDraft();
}

function _resetApprovalsToDefault() {
  const approvals = (CUSTOMER_WIZARD.status && CUSTOMER_WIZARD.status.approvals) || [];
  CUSTOMER_WIZARD.draft.approvals = {};
  approvals.forEach(a => {
    CUSTOMER_WIZARD.draft.approvals[a.id] = a.default || 'ask';
  });
  _renderCustomerWizardBody();
  _saveCurrentStepDraft();
  _showWizardNotice('✓ Action permissions reset to recommended security defaults and saved.', 'success');
}

// ── Step F: Persona & Knowledge Base (SOUL, USER, MEMORY & Obsidian) ─────────

function _renderStepKnowledge(el) {
  const k = CUSTOMER_WIZARD.draft.knowledge || {};
  const activeTab = k.activeTab || 'team';
  const teamMembers = k.team_members || [];

  el.innerHTML = `
    <div class="cw-step-pane">
      <div class="cw-pane-head">
        <h3>Assistant Persona & Knowledge Architecture</h3>
        <p>Customize Lisa's core identity, organization context, institutional memory, team members directory, and connect your team's Obsidian vault.</p>
      </div>

      <div class="cw-tabs-bar" style="display:grid; grid-template-columns:repeat(auto-fit, minmax(115px, 1fr)); gap:8px; margin-bottom:18px; padding:6px; background:rgba(0,0,0,0.28); border:1px solid rgba(255,255,255,0.08); border-radius:10px; overflow:visible;">
        <button type="button" class="cw-tab-btn ${activeTab === 'team' ? 'active' : ''}" onclick="_switchKnowledgeTab('team')" style="display:flex; flex-direction:column; align-items:center; justify-content:center; gap:3px; padding:10px 6px; border-radius:8px; cursor:pointer; text-align:center; min-width:0; box-sizing:border-box;">
          <span class="cw-tab-icon" style="font-size:16px; line-height:1;">👥</span>
          <span class="cw-tab-title" style="font-size:12px; font-weight:600; line-height:1.25; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; max-width:100%;">Team Directory</span>
          <span class="cw-tab-sub" style="font-size:10px; opacity:0.75; font-family:monospace; line-height:1.2; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; max-width:100%;">identity-mapping</span>
        </button>
        <button type="button" class="cw-tab-btn ${activeTab === 'soul' ? 'active' : ''}" onclick="_switchKnowledgeTab('soul')" style="display:flex; flex-direction:column; align-items:center; justify-content:center; gap:3px; padding:10px 6px; border-radius:8px; cursor:pointer; text-align:center; min-width:0; box-sizing:border-box;">
          <span class="cw-tab-icon" style="font-size:16px; line-height:1;">🔮</span>
          <span class="cw-tab-title" style="font-size:12px; font-weight:600; line-height:1.25; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; max-width:100%;">Lisa Soul</span>
          <span class="cw-tab-sub" style="font-size:10px; opacity:0.75; font-family:monospace; line-height:1.2; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; max-width:100%;">SOUL.md</span>
        </button>
        <button type="button" class="cw-tab-btn ${activeTab === 'user' ? 'active' : ''}" onclick="_switchKnowledgeTab('user')" style="display:flex; flex-direction:column; align-items:center; justify-content:center; gap:3px; padding:10px 6px; border-radius:8px; cursor:pointer; text-align:center; min-width:0; box-sizing:border-box;">
          <span class="cw-tab-icon" style="font-size:16px; line-height:1;">👤</span>
          <span class="cw-tab-title" style="font-size:12px; font-weight:600; line-height:1.25; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; max-width:100%;">User Profile</span>
          <span class="cw-tab-sub" style="font-size:10px; opacity:0.75; font-family:monospace; line-height:1.2; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; max-width:100%;">USER.md</span>
        </button>
        <button type="button" class="cw-tab-btn ${activeTab === 'memory' ? 'active' : ''}" onclick="_switchKnowledgeTab('memory')" style="display:flex; flex-direction:column; align-items:center; justify-content:center; gap:3px; padding:10px 6px; border-radius:8px; cursor:pointer; text-align:center; min-width:0; box-sizing:border-box;">
          <span class="cw-tab-icon" style="font-size:16px; line-height:1;">🧠</span>
          <span class="cw-tab-title" style="font-size:12px; font-weight:600; line-height:1.25; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; max-width:100%;">Institutional Memory</span>
          <span class="cw-tab-sub" style="font-size:10px; opacity:0.75; font-family:monospace; line-height:1.2; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; max-width:100%;">MEMORY.md</span>
        </button>
        <button type="button" class="cw-tab-btn ${activeTab === 'obsidian' ? 'active' : ''}" onclick="_switchKnowledgeTab('obsidian')" style="display:flex; flex-direction:column; align-items:center; justify-content:center; gap:3px; padding:10px 6px; border-radius:8px; cursor:pointer; text-align:center; min-width:0; box-sizing:border-box;">
          <span class="cw-tab-icon" style="font-size:16px; line-height:1;">📓</span>
          <span class="cw-tab-title" style="font-size:12px; font-weight:600; line-height:1.25; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; max-width:100%;">Obsidian Vault</span>
          <span class="cw-tab-sub" style="font-size:10px; opacity:0.75; font-family:monospace; line-height:1.2; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; max-width:100%;">Handbook & Notes</span>
        </button>
      </div>

      <div class="cw-tab-content">
        ${activeTab === 'team' ? `
          <div class="cw-tab-pane">
            <div class="cw-tab-intro">
              <div>
                <strong>Organization Team & Stakeholder Directory (identity-mapping.json)</strong>
                <p>Register team members with their ClickUp user ID, Telegram ID, email, and role. Lisa uses this mapping for task assignment, Telegram notifications, and meeting action items.</p>
              </div>
              <div class="cw-presets-row">
                <button type="button" class="btn primary sm-btn" onclick="_showAddMemberForm()">➕ Add Team Member</button>
                <button type="button" class="btn sm-btn" onclick="_syncTeamMembersNow()">⚡ Sync Team Now</button>
              </div>
            </div>

            <div id="cw_member_form_container" style="display:none; margin: 12px 0; padding: 16px; border: 1px solid var(--border-color, #444); border-radius: 8px; background: rgba(0,0,0,0.3);">
              <h4 id="cw_member_form_title" style="margin-top:0; margin-bottom:14px; color:var(--text-bright,#fff); font-size:15px;">Add New Team Member</h4>
              <input type="hidden" id="cw_m_person_id" value="" />
              <input type="hidden" id="cw_m_edit_index" value="-1" />
              <div class="cw-form-row">
                <div class="cw-form-group">
                  <label>Canonical / Full Name <span class="cw-req">*</span></label>
                  <input type="text" id="cw_m_name" placeholder="e.g. Davood Fouladi" />
                  <small class="cw-hint">Primary full name recognized in all documents and tasks.</small>
                </div>
                <div class="cw-form-group">
                  <label>Role / Position</label>
                  <input type="text" id="cw_m_role" placeholder="e.g. CEO & Founder" />
                </div>
              </div>
              <div class="cw-form-group" style="margin-bottom:12px;">
                <label>Email Addresses (Multiple supported) <span class="cw-req">*</span></label>
                <input type="text" id="cw_m_emails" placeholder="e.g. davood@matika.at, davood.thinker@gmail.com, davoodf1995@gmail.com" />
                <small class="cw-hint">Comma-separated email addresses. All emails are linked to this member for Google Workspace, Drive, and briefings.</small>
              </div>
              <div class="cw-form-row">
                <div class="cw-form-group">
                  <label>ClickUp User ID (Numeric)</label>
                  <input type="text" id="cw_m_clickup" placeholder="e.g. 57259309" />
                  <small class="cw-hint">Numeric ClickUp user ID</small>
                </div>
                <div class="cw-form-group">
                  <label>ClickUp Member Name</label>
                  <input type="text" id="cw_m_clickup_name" placeholder="e.g. davood" />
                  <small class="cw-hint">ClickUp username / handle</small>
                </div>
              </div>
              <div class="cw-form-row">
                <div class="cw-form-group">
                  <label>Telegram User ID (Numeric)</label>
                  <input type="text" id="cw_m_tele_id" placeholder="e.g. 59597486" />
                  <small class="cw-hint">Numeric Telegram user ID</small>
                </div>
                <div class="cw-form-group">
                  <label>Telegram Display Name</label>
                  <input type="text" id="cw_m_tele_display" placeholder="e.g. Davood Fouladi" />
                </div>
                <div class="cw-form-group">
                  <label>Telegram Username</label>
                  <input type="text" id="cw_m_tele_user" placeholder="e.g. @davood_thinker" />
                </div>
              </div>
              <div class="cw-form-group" style="margin-bottom:14px;">
                <label>Aliases / Nicknames (comma-separated)</label>
                <input type="text" id="cw_m_aliases" placeholder="e.g. Davood, david, Davood Fouladi" />
                <small class="cw-hint">Alternative names used by colleagues that Lisa maps to this person</small>
              </div>
              <div style="display:flex; gap:10px; margin-top:14px;">
                <button type="button" class="btn primary sm-btn" onclick="_saveMemberForm()">💾 Save Member</button>
                <button type="button" class="sm-btn" onclick="_cancelMemberForm()">Cancel</button>
              </div>
            </div>

            <div class="cw-team-table-wrap" style="overflow-x:auto; margin:12px 0;">
              <table class="cw-team-table" style="width:100%; border-collapse:collapse; font-size:13px;">
                <thead>
                  <tr style="border-bottom: 2px solid var(--border-color,#444); text-align:left;">
                    <th style="padding:10px 8px;">Name & Role</th>
                    <th style="padding:10px 8px;">Emails</th>
                    <th style="padding:10px 8px;">Telegram</th>
                    <th style="padding:10px 8px;">ClickUp</th>
                    <th style="padding:10px 8px;">Aliases</th>
                    <th style="padding:10px 8px; text-align:right;">Actions</th>
                  </tr>
                </thead>
                <tbody>
                  ${(teamMembers && teamMembers.length) ?
                    teamMembers.map((m, idx) => {
                      const name = m.canonical_name || m.name || '';
                      const role = m.role || 'Member';
                      const person_id = m.person_id || '';
                      const emails = Array.isArray(m.emails) ? m.emails : (m.email ? [m.email] : []);

                      const t_id = (m.telegram && m.telegram.user_id) || m.telegram_id || '';
                      const t_disp = (m.telegram && m.telegram.display_name) || '';
                      const t_u = (m.telegram && m.telegram.username) || m.telegram_username || '';

                      const c_id = (m.clickup && m.clickup.user_id !== undefined && m.clickup.user_id !== null) ? m.clickup.user_id : (m.clickup_id || '');
                      const c_name = (m.clickup && m.clickup.member_name) || '';

                      const aliases = m.aliases || [];
                      const aliasesList = Array.isArray(aliases) ? aliases : [aliases];

                      return `
                        <tr style="border-bottom: 1px solid var(--border-color,#333);">
                          <td style="padding:10px 8px; vertical-align:top;">
                            <strong>${_escapeHtml(name)}</strong>
                            <div style="font-size:12px; color:var(--text-muted);">${_escapeHtml(role)}</div>
                            ${person_id ? `<div style="font-size:10px; opacity:0.55; font-family:monospace; margin-top:2px;">ID: ${_escapeHtml(person_id.substring(0, 13))}...</div>` : ''}
                          </td>
                          <td style="padding:10px 8px; vertical-align:top;">
                            ${(emails && emails.length) ? emails.map(e => `
                              <div style="margin:2px 0;">
                                <code style="font-size:11px; padding:2px 6px; background:rgba(255,255,255,0.06); border-radius:4px; color:#cbd5e1;">${_escapeHtml(e)}</code>
                              </div>
                            `).join('') : '<span style="color:var(--text-muted);">—</span>'}
                          </td>
                          <td style="padding:10px 8px; vertical-align:top;">
                            ${t_id ? `<code style="font-size:11px; padding:2px 6px; background:rgba(0,180,255,0.12); border-radius:4px; color:#38bdf8;">${_escapeHtml(t_id)}</code>` : '<span style="color:var(--text-muted);">—</span>'}
                            ${t_disp ? `<div style="font-size:11px; color:var(--text-bright,#fff); margin-top:3px;">${_escapeHtml(t_disp)}</div>` : ''}
                            ${t_u ? `<div style="font-size:11px; color:var(--text-muted);">${_escapeHtml(t_u)}</div>` : ''}
                          </td>
                          <td style="padding:10px 8px; vertical-align:top;">
                            ${c_id ? `<code style="font-size:11px; padding:2px 6px; background:rgba(123,97,255,0.12); border-radius:4px; color:#c4b5fd;">${_escapeHtml(c_id)}</code>` : '<span style="color:var(--text-muted);">—</span>'}
                            ${c_name ? `<div style="font-size:11px; color:var(--text-muted); margin-top:3px;">${_escapeHtml(c_name)}</div>` : ''}
                          </td>
                          <td style="padding:10px 8px; vertical-align:top;">
                            ${aliasesList.map(a => `<span style="display:inline-block; margin:2px 3px 2px 0; padding:1px 6px; background:rgba(255,255,255,0.05); border-radius:8px; font-size:11px;">${_escapeHtml(a)}</span>`).join('')}
                          </td>
                          <td style="padding:10px 8px; text-align:right; white-space:nowrap; vertical-align:top;">
                            <button type="button" class="sm-btn" onclick="_editMember(${idx})" title="Edit" style="margin-right:4px;">✏️ Edit</button>
                            <button type="button" class="sm-btn" onclick="_removeMember(${idx})" title="Delete" style="color:var(--error,#ff5555);">🗑️</button>
                          </td>
                        </tr>
                      `;
                    }).join('')
                    : `
                      <tr>
                        <td colspan="6" style="text-align:center; padding: 24px; color: var(--text-muted);">
                          No team members registered yet. Click <strong>➕ Add Team Member</strong> above to add leadership and members.
                        </td>
                      </tr>
                    `
                  }
                </tbody>
              </table>
            </div>

            <div class="cw-team-guide-callout" style="margin-top:14px; padding:12px; border-radius:6px; background:rgba(0,110,179,0.08); font-size:13px; line-height:1.5;">
              <strong>ℹ️ How Lisa uses this directory:</strong>
              <ul style="margin:6px 0 0 18px; padding:0;">
                <li><strong>ClickUp Task Creation:</strong> In meetings and chats, when Lisa creates action items, she maps attendee names to their exact ClickUp numeric user IDs.</li>
                <li><strong>Telegram DMs & Briefings:</strong> Lisa routes private notifications and personalized morning briefings to mapped Telegram user IDs.</li>
                <li><strong>Google Workspace & Drive:</strong> Any of the registered email addresses for a team member are recognized as authorized internal collaborators.</li>
                <li><strong>Obsidian 01-People/ Links:</strong> Each member automatically receives a dedicated wiki-linked note (<code>01-People/&lt;Name&gt;.md</code>) and is listed in <code>Company_Handbook.md</code>.</li>
              </ul>
            </div>
          </div>
        ` : ''}

        ${activeTab === 'soul' ? `
          <div class="cw-tab-pane">
            <div class="cw-tab-intro">
              <div>
                <strong>Lisa's Core Persona & Behavioral Principles</strong>
                <p>Governs communication tone, directness, brevity, and decision boundaries.</p>
              </div>
              <div class="cw-presets-row">
                <span class="cw-preset-label">Templates:</span>
                <button type="button" class="sm-btn" onclick="_applySoulTemplate('executive')">👑 Executive AI</button>
                <button type="button" class="sm-btn" onclick="_applySoulTemplate('technical')">🛠️ Tech Co-Pilot</button>
                <button type="button" class="sm-btn" onclick="_applySoulTemplate('support')">🤝 Client Success</button>
              </div>
            </div>
            <textarea id="cwSoulEditor" class="cw-code-editor" style="width:100% !important; min-height:360px; box-sizing:border-box !important; display:block; font-family:ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace; font-size:13px; line-height:1.6; padding:14px; background:rgba(0,0,0,0.3); border:1px solid var(--border-color,#333); border-radius:8px; color:#e2e8f0; resize:vertical;" placeholder="Write or customize SOUL.md..." oninput="CUSTOMER_WIZARD.draft.knowledge.soul = this.value">${_escapeHtml(k.soul || '')}</textarea>
            <div class="cw-editor-footer" style="margin-top:10px;">
              <small class="cw-hint">Saved to <code>HERMES_HOME/SOUL.md</code>. Auto-saved on Continue.</small>
              <button type="button" class="btn sm-btn" onclick="_saveKnowledgeFilesNow('soul')">Save Soul Now</button>
            </div>
          </div>
        ` : ''}

        ${activeTab === 'user' ? `
          <div class="cw-tab-pane">
            <div class="cw-tab-intro">
              <div>
                <strong>Company & User Profile (USER.md)</strong>
                <p>Teaches Lisa about your leadership team, key stakeholders, working hours, and communication rules.</p>
              </div>
              <div class="cw-presets-row">
                <span class="cw-preset-label">Templates:</span>
                <button type="button" class="sm-btn" onclick="_applyUserTemplate('executive')">🏢 Executive Leadership</button>
                <button type="button" class="sm-btn" onclick="_applyUserTemplate('startup')">🚀 Product & Tech Team</button>
              </div>
            </div>
            <textarea id="cwUserEditor" class="cw-code-editor" style="width:100% !important; min-height:360px; box-sizing:border-box !important; display:block; font-family:ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace; font-size:13px; line-height:1.6; padding:14px; background:rgba(0,0,0,0.3); border:1px solid var(--border-color,#333); border-radius:8px; color:#e2e8f0; resize:vertical;" placeholder="Write or customize USER.md..." oninput="CUSTOMER_WIZARD.draft.knowledge.user = this.value">${_escapeHtml(k.user || '')}</textarea>
            <div class="cw-editor-footer" style="margin-top:10px;">
              <small class="cw-hint">Saved to <code>HERMES_HOME/memories/USER.md</code>. Auto-saved on Continue.</small>
              <button type="button" class="btn sm-btn" onclick="_saveKnowledgeFilesNow('user')">Save User Profile Now</button>
            </div>
          </div>
        ` : ''}

        ${activeTab === 'memory' ? `
          <div class="cw-tab-pane">
            <div class="cw-tab-intro">
              <div>
                <strong>Permanent Institutional Memory (MEMORY.md)</strong>
                <p>Curated company facts, approved terminology, key client notes, and recurring operational guidelines.</p>
              </div>
            </div>
            <textarea id="cwMemoryEditor" class="cw-code-editor" style="width:100% !important; min-height:360px; box-sizing:border-box !important; display:block; font-family:ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace; font-size:13px; line-height:1.6; padding:14px; background:rgba(0,0,0,0.3); border:1px solid var(--border-color,#333); border-radius:8px; color:#e2e8f0; resize:vertical;" placeholder="Write or customize MEMORY.md..." oninput="CUSTOMER_WIZARD.draft.knowledge.memory = this.value">${_escapeHtml(k.memory || '')}</textarea>
            <div class="cw-editor-footer" style="margin-top:10px;">
              <small class="cw-hint">Saved to <code>HERMES_HOME/memories/MEMORY.md</code>. Auto-saved on Continue.</small>
              <button type="button" class="btn sm-btn" onclick="_saveKnowledgeFilesNow('memory')">Save Memory Now</button>
            </div>
          </div>
        ` : ''}

        ${activeTab === 'obsidian' ? `
          <div class="cw-tab-pane">
            <div class="cw-tab-intro">
              <div>
                <strong>Obsidian Knowledge Vault & Team Graph Architecture</strong>
                <p>Connect your team's Obsidian vault. Lisa indexes verified markdown notes and briefings across all folders.</p>
              </div>
            </div>

            <div class="cw-form-group" style="margin-bottom:12px;">
              <label>Obsidian Vault Directory / Path <span class="cw-req">*</span></label>
              <input type="text" id="cwObsidianPath" value="${_escapeHtml(k.obsidian_vault_path || '/workspace/obsidian')}" placeholder="/workspace/obsidian or /path/to/vault" oninput="CUSTOMER_WIZARD.draft.knowledge.obsidian_vault_path = this.value">
              <small class="cw-hint">Local or container-mounted path where your Obsidian Markdown vault files reside.</small>
            </div>

            <div class="cw-form-group" style="margin-bottom:14px;">
              <label>Team Sync / Share Link (Optional)</label>
              <input type="text" id="cwObsidianTeamLink" value="${_escapeHtml(k.obsidian_team_link || '')}" placeholder="e.g. https://github.com/company/obsidian-vault or obsidian://sync/..." oninput="CUSTOMER_WIZARD.draft.knowledge.obsidian_team_link = this.value">
              <small class="cw-hint">Shareable link for team members to sync or clone this vault (Git, Obsidian Sync, or Network Drive).</small>
            </div>

            <div class="cw-obsidian-architecture-box">
              <div class="cw-arch-header">
                <strong>Standard Enterprise Vault Architecture:</strong>
                <button type="button" class="btn primary sm-btn" id="cwScaffoldBtn" onclick="_scaffoldObsidianVault()">
                  🏗️ Scaffold Obsidian Folders & Templates
                </button>
              </div>
              <div class="cw-folder-tree">
                <div class="cw-tree-item">📁 <strong>00-Inbox/</strong> — Quick-capture notes, voice memos, unprocessed briefings</div>
                <div class="cw-tree-item">📁 <strong>01-People/</strong> — Company members, leadership directory, individual wikilink notes</div>
                <div class="cw-tree-item">📁 <strong>02-Companies/</strong> — Clients, corporate partners, external vendor records</div>
                <div class="cw-tree-item">📁 <strong>03-Projects/</strong> — Active quarterly roadmaps, deliverables, sprint backlogs</div>
                <div class="cw-tree-item">📁 <strong>04-Meetings/</strong> — Meeting summaries, Fireflies transcription imports, action items</div>
                <div class="cw-tree-item">📁 <strong>05-Decisions/</strong> — Architecture & Business Decision Records (ADRs), policy changes</div>
                <div class="cw-tree-item">📁 <strong>06-Knowledge/</strong> — Company SOPs, architecture specs, playbooks, tech docs</div>
                <div class="cw-tree-item">📁 <strong>07-Daily/</strong> — Chronological daily logs & scratchpads</div>
                <div class="cw-tree-item">📁 <strong>08-Team-Briefing/</strong> — Daily morning briefings archive</div>
                <div class="cw-tree-item">📁 <strong>files/</strong> — Working deliverables, exported PDFs, client proposals</div>
                <div class="cw-tree-item">📄 <strong>Company_Handbook.md</strong> — Master company handbook linking to <code>[[01-People/...]]</code> and folders</div>
              </div>
            </div>

            <div class="cw-team-guide-callout" style="margin-top:14px; padding:14px; border-radius:8px; background:rgba(56, 189, 248, 0.08); border-left:3px solid #38bdf8; font-size:13px; line-height:1.6;">
              <strong style="color:#38bdf8; display:block; margin-bottom:6px;">💡 How your team updates and collaborates via Obsidian:</strong>
              <ol style="margin:0; padding-left:20px;">
                <li>Team members download the free <a href="https://obsidian.md" target="_blank" rel="noopener" class="cw-obsidian-app-link" style="color:#38bdf8 !important; text-decoration:underline !important; font-weight:600;">Obsidian app ↗</a>.</li>
                <li>Open or clone the vault directory or use the Team Sync link above.</li>
                <li>Create and link notes using standard <code>[[WikiLinks]]</code> (e.g. <code>[[01-People/Davood Fouladi|Davood Fouladi]]</code>).</li>
                <li>Lisa automatically indexes all markdown files and uses them to answer team queries, draft emails, and prepare morning briefings.</li>
              </ol>
            </div>
          </div>
        ` : ''}
      </div>
    </div>
  `;
}

function _switchKnowledgeTab(tabName) {
  _collectCurrentStepData('knowledge');
  CUSTOMER_WIZARD.draft.knowledge = CUSTOMER_WIZARD.draft.knowledge || {};
  CUSTOMER_WIZARD.draft.knowledge.activeTab = tabName;
  _renderCustomerWizardBody();
  _saveCurrentStepDraft();
}

function _showAddMemberForm() {
  const container = document.getElementById('cw_member_form_container');
  if (!container) return;
  document.getElementById('cw_member_form_title').textContent = '➕ Add New Team Member';
  document.getElementById('cw_m_person_id').value = '';
  document.getElementById('cw_m_name').value = '';
  document.getElementById('cw_m_role').value = '';
  document.getElementById('cw_m_emails').value = '';
  document.getElementById('cw_m_clickup').value = '';
  document.getElementById('cw_m_clickup_name').value = '';
  document.getElementById('cw_m_tele_id').value = '';
  document.getElementById('cw_m_tele_display').value = '';
  document.getElementById('cw_m_tele_user').value = '';
  document.getElementById('cw_m_aliases').value = '';
  document.getElementById('cw_m_edit_index').value = '-1';
  container.style.display = 'block';
  document.getElementById('cw_m_name').focus();
}

function _cancelMemberForm() {
  const container = document.getElementById('cw_member_form_container');
  if (container) container.style.display = 'none';
}

function _editMember(idx) {
  const members = (CUSTOMER_WIZARD.draft.knowledge && CUSTOMER_WIZARD.draft.knowledge.team_members) || [];
  const m = members[idx];
  if (!m) return;
  const container = document.getElementById('cw_member_form_container');
  if (!container) return;
  const name = m.canonical_name || m.name || '';
  document.getElementById('cw_member_form_title').textContent = '✏️ Edit Member: ' + name;
  document.getElementById('cw_m_person_id').value = m.person_id || '';
  document.getElementById('cw_m_name').value = name;
  document.getElementById('cw_m_role').value = m.role || '';
  const emails = Array.isArray(m.emails) ? m.emails : (m.email ? [m.email] : []);
  document.getElementById('cw_m_emails').value = emails.join(', ');
  document.getElementById('cw_m_clickup').value = (m.clickup && m.clickup.user_id !== undefined && m.clickup.user_id !== null) ? m.clickup.user_id : (m.clickup_id || '');
  document.getElementById('cw_m_clickup_name').value = (m.clickup && m.clickup.member_name) || '';
  document.getElementById('cw_m_tele_id').value = (m.telegram && m.telegram.user_id) || m.telegram_id || '';
  document.getElementById('cw_m_tele_display').value = (m.telegram && m.telegram.display_name) || '';
  document.getElementById('cw_m_tele_user').value = (m.telegram && m.telegram.username) || m.telegram_username || '';
  const aliases = m.aliases || [];
  document.getElementById('cw_m_aliases').value = Array.isArray(aliases) ? aliases.join(', ') : aliases;
  document.getElementById('cw_m_edit_index').value = idx;
  container.style.display = 'block';
  document.getElementById('cw_m_name').focus();
}

async function _saveMemberForm() {
  const name = (document.getElementById('cw_m_name') ? document.getElementById('cw_m_name').value : '').trim();
  if (!name) {
    alert('Please enter the team member name.');
    return;
  }
  const person_id = (document.getElementById('cw_m_person_id') ? document.getElementById('cw_m_person_id').value : '').trim();
  const role = (document.getElementById('cw_m_role') ? document.getElementById('cw_m_role').value : '').trim() || 'Team Member';
  
  const raw_emails = (document.getElementById('cw_m_emails') ? document.getElementById('cw_m_emails').value : '').trim();
  const emails = raw_emails ? raw_emails.split(/[\n,;]+/).map(s => s.trim().toLowerCase()).filter(Boolean) : [];

  const clickup_id_raw = (document.getElementById('cw_m_clickup') ? document.getElementById('cw_m_clickup').value : '').trim();
  const clickup_name = (document.getElementById('cw_m_clickup_name') ? document.getElementById('cw_m_clickup_name').value : '').trim();
  const clickup_id = (/^\d+$/.test(clickup_id_raw)) ? parseInt(clickup_id_raw, 10) : clickup_id_raw;

  const tele_id = (document.getElementById('cw_m_tele_id') ? document.getElementById('cw_m_tele_id').value : '').trim();
  const tele_display = (document.getElementById('cw_m_tele_display') ? document.getElementById('cw_m_tele_display').value : '').trim() || name;
  const tele_user = (document.getElementById('cw_m_tele_user') ? document.getElementById('cw_m_tele_user').value : '').trim();

  const raw_aliases = (document.getElementById('cw_m_aliases') ? document.getElementById('cw_m_aliases').value : '').trim();
  const aliases = raw_aliases ? raw_aliases.split(/[\n,;]+/).map(s => s.trim()).filter(Boolean) : [name];
  if (!aliases.includes(name)) aliases.push(name);

  const idx = parseInt(document.getElementById('cw_m_edit_index').value, 10);
  CUSTOMER_WIZARD.draft.knowledge = CUSTOMER_WIZARD.draft.knowledge || {};
  CUSTOMER_WIZARD.draft.knowledge.team_members = CUSTOMER_WIZARD.draft.knowledge.team_members || [];

  const record = {
    person_id: person_id || undefined,
    canonical_name: name,
    name: name,
    role: role,
    emails: emails,
    email: emails[0] || '',
    telegram: {
      user_id: tele_id,
      display_name: tele_display,
      username: tele_user,
    },
    clickup: {
      member_name: clickup_name || name.split(' ')[0].toLowerCase(),
      user_id: clickup_id,
    },
    aliases: aliases,
  };

  if (idx >= 0 && idx < CUSTOMER_WIZARD.draft.knowledge.team_members.length) {
    CUSTOMER_WIZARD.draft.knowledge.team_members[idx] = record;
  } else {
    CUSTOMER_WIZARD.draft.knowledge.team_members.push(record);
  }

  _cancelMemberForm();
  await _syncTeamMembersNow();
  _renderCustomerWizardBody();
}

async function _removeMember(idx) {
  const members = (CUSTOMER_WIZARD.draft.knowledge && CUSTOMER_WIZARD.draft.knowledge.team_members) || [];
  if (!confirm(`Are you sure you want to remove ${members[idx] && members[idx].name}?`)) return;
  members.splice(idx, 1);
  await _syncTeamMembersNow();
  _renderCustomerWizardBody();
}

async function _syncTeamMembersNow() {
  const members = (CUSTOMER_WIZARD.draft.knowledge && CUSTOMER_WIZARD.draft.knowledge.team_members) || [];
  try {
    const res = await fetch('/api/customer-wizard/team', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ people: members }),
    });
    const data = await res.json();
    if (data.ok) {
      _showWizardNotice(data.message, 'success');
    }
  } catch (err) {
    console.error('Failed to sync team:', err);
  }
}

function _setBriefingRecipient(val) {
  const el = document.getElementById('cwBriefingRecipient');
  if (el) {
    el.value = val;
    CUSTOMER_WIZARD.draft.briefing.recipient = val;
    _saveCurrentStepDraft();
  }
}

function _applySoulTemplate(type) {
  const company = (CUSTOMER_WIZARD.draft.company && CUSTOMER_WIZARD.draft.company.company_name) || 'our company';
  const assistant = (CUSTOMER_WIZARD.draft.company && CUSTOMER_WIZARD.draft.company.assistant_name) || 'Lisa';

  const templates = {
    executive: `# Identity & Core Principles\nYou are ${assistant}, an executive AI partner for leadership at ${company}.\n\n# Directives\n- Maximum directness: Match the length of your reply to the importance of the task.\n- Zero fluff: Never use filler ("I'd be happy to", "Great question", restating the prompt).\n- High agency: Propose concrete next steps, draft action items, and verify facts before answering.\n- Tone: Professional, discreet, calm, and dependable.\n- Confidentiality: Never expose credentials or sensitive private notes outside authorized tools.`,
    technical: `# Identity & Core Principles\nYou are ${assistant}, a technical operations and systems co-pilot at ${company}.\n\n# Directives\n- Systematic & evidence-driven: Always provide verified command lines, paths, and exact configurations.\n- Code & Architecture first: Focus on modular design, idempotent operations, and deterministic tests.\n- Tone: Analytical, precise, clear, and focused on technical excellence.`,
    support: `# Identity & Core Principles\nYou are ${assistant}, client success and operations manager at ${company}.\n\n# Directives\n- Proactive & empathetic: Anticipate client questions, schedule timely follow-ups, and ensure clear communication.\n- Clear summaries: Provide bulleted action points and clear timelines for deliverables.\n- Tone: Welcoming, courteous, solution-oriented, and reassuring.`
  };

  const chosen = templates[type] || templates.executive;
  const editor = $('cwSoulEditor');
  if (editor) editor.value = chosen;
  CUSTOMER_WIZARD.draft.knowledge.soul = chosen;
  _saveCurrentStepDraft();
  _showWizardNotice(`✓ ${type.toUpperCase()} template applied to Lisa Soul.`, 'info');
}

function _applyUserTemplate(type) {
  const company = (CUSTOMER_WIZARD.draft.company && CUSTOMER_WIZARD.draft.company.company_name) || 'Company';
  const admin = (CUSTOMER_WIZARD.draft.company && CUSTOMER_WIZARD.draft.company.admin_name) || 'Admin';

  const templates = {
    executive: `# Company & Leadership Context\n\n## Organization\n- **Company Name**: ${company}\n- **Executive Lead**: ${admin}\n- **Primary Industry**:\n- **Core Value Proposition**:\n\n## Working Guidelines\n- **Working Hours**: Monday – Friday, 09:00 – 18:00\n- **High Priority Stakeholders**: [List names and emails]\n- **Meeting Preference**: 30-minute default with 5-minute buffer\n- **Communication Channels**: Telegram for real-time alerts, Email for formal client correspondence`,
    startup: `# Team & Workstream Context\n\n## Team Structure\n- **Company**: ${company}\n- **Product Lead**: ${admin}\n- **Sprint Cycle**: Two-week agile sprints\n- **Key Repositories & Tools**: ClickUp, GitHub, Google Workspace, Obsidian\n\n## Team Rules\n- Document architectural decisions in Obsidian under \`30-Resources/\`\n- All task updates sync to ClickUp\n- Review daily briefing every morning at 08:30`
  };

  const chosen = templates[type] || templates.executive;
  const editor = $('cwUserEditor');
  if (editor) editor.value = chosen;
  CUSTOMER_WIZARD.draft.knowledge.user = chosen;
  _saveCurrentStepDraft();
  _showWizardNotice(`✓ ${type.toUpperCase()} template applied to User Profile.`, 'info');
}

async function _saveKnowledgeFilesNow(section) {
  _collectCurrentStepData('knowledge');
  const k = CUSTOMER_WIZARD.draft.knowledge || {};
  try {
    await api('/api/customer-wizard/step', {
      method: 'POST',
      body: JSON.stringify({
        step: 'knowledge',
        data: {
          ...k,
          save_files_now: true,
        }
      })
    });
    _showWizardNotice(`✓ ${section.toUpperCase()} saved immediately to server disk.`, 'success');
  } catch (err) {
    _showWizardNotice(`Failed to save ${section}: ${err.message}`, 'error');
  }
}

async function _scaffoldObsidianVault() {
  const btn = $('cwScaffoldBtn');
  const pathInp = $('cwObsidianPath');
  const vaultPath = (pathInp ? pathInp.value : '').trim();

  if (btn) btn.disabled = true;
  _showWizardNotice('Scaffolding Obsidian folders and starter templates…', 'info');

  try {
    const res = await api('/api/customer-wizard/scaffold-obsidian', {
      method: 'POST',
      body: JSON.stringify({ vault_path: vaultPath })
    });

    if (btn) btn.disabled = false;
    if (res && res.ok) {
      _showWizardNotice(`✓ Obsidian vault successfully scaffolded at "${res.vault_path}" with 10 canonical folders and member notes!`, 'success');
      CUSTOMER_WIZARD.draft.knowledge.obsidian_vault_path = res.vault_path;
      _saveCurrentStepDraft();
      _renderCustomerWizardBody();
    } else {
      _showWizardNotice(`Scaffolding failed: ${res ? res.message : 'Unknown error'}`, 'error');
    }
  } catch (err) {
    if (btn) btn.disabled = false;
    _showWizardNotice(`Scaffold error: ${err.message}`, 'error');
  }
}

// ── Step G: Notifications & Daily Briefing ────────────────────────────────────

function _renderStepBriefing(el) {
  const b = CUSTOMER_WIZARD.draft.briefing;
  const isEnabled = !!b.enabled;
  const topics = new Set(b.topics || ['calendar', 'tasks', 'meetings']);
  const teamMembers = (CUSTOMER_WIZARD.draft.knowledge && CUSTOMER_WIZARD.draft.knowledge.team_members) || [];
  const teamWithTelegram = teamMembers.filter(m => {
    const tid = (m.telegram && m.telegram.user_id) || m.telegram_id;
    return !!tid;
  });

  el.innerHTML = `
    <div class="cw-step-pane">
      <div class="cw-pane-head">
        <h3>Notifications & Daily Briefing</h3>
        <p>Schedule an automated morning executive briefing to keep your team aligned.</p>
      </div>

      <div class="cw-form-group">
        <label style="display:flex;align-items:center;gap:10px;cursor:pointer;font-weight:600">
          <input type="checkbox" id="cwBriefingEnabled" ${isEnabled ? 'checked' : ''} onchange="CUSTOMER_WIZARD.draft.briefing.enabled=this.checked;_renderCustomerWizardBody();">
          Enable Daily Morning Briefing
        </label>
      </div>

      ${isEnabled ? `
        <div class="cw-briefing-fields">
          <div class="cw-form-row">
            <div class="cw-form-group">
              <label>Delivery Channel</label>
              <select id="cwBriefingChannel">
                <option value="telegram" ${b.channel === 'telegram' ? 'selected' : ''}>Telegram</option>
                <option value="mattermost" disabled>Mattermost (Not installed)</option>
              </select>
            </div>
            <div class="cw-form-group">
              <label>Recipient / Chat ID <span class="cw-req">*</span></label>
              <input type="text" id="cwBriefingRecipient" value="${_escapeHtml(b.recipient || '')}" placeholder="e.g. 12345678 or -100123456789">
              ${teamWithTelegram.length ? `
                <div style="margin-top: 6px;">
                  <span class="cw-hint" style="display:block; margin-bottom:4px;">Quick select from Team Directory:</span>
                  <div style="display:flex; gap:6px; flex-wrap:wrap;">
                    ${teamWithTelegram.map(m => {
                      const tid = (m.telegram && m.telegram.user_id) || m.telegram_id;
                      return `
                        <button type="button" class="sm-btn" onclick="_setBriefingRecipient('${_escapeHtml(tid)}')">
                          👤 ${_escapeHtml(m.name)} (<code>${_escapeHtml(tid)}</code>)
                        </button>
                      `;
                    }).join('')}
                  </div>
                </div>
              ` : ''}
            </div>
          </div>

          <div class="cw-form-row">
            <div class="cw-form-group">
              <label>Briefing Time (24h) <span class="cw-req">*</span></label>
              <input type="time" id="cwBriefingTime" value="${_escapeHtml(b.time || '08:30')}">
            </div>
            <div class="cw-form-group">
              <label>Timezone</label>
              <input type="text" id="cwBriefingTz" value="${_escapeHtml(b.timezone || 'Europe/Vienna')}">
            </div>
          </div>

          <div class="cw-form-group">
            <label>Include Briefing Content</label>
            <div class="cw-checkbox-grid">
              <label><input type="checkbox" name="cwBriefingTopics" value="calendar" ${topics.has('calendar') ? 'checked' : ''}> Today's Calendar Schedule</label>
              <label><input type="checkbox" name="cwBriefingTopics" value="tasks" ${topics.has('tasks') ? 'checked' : ''}> ClickUp Due Tasks & Deadlines</label>
              <label><input type="checkbox" name="cwBriefingTopics" value="meetings" ${topics.has('meetings') ? 'checked' : ''}> Yesterday's Meeting Follow-ups</label>
            </div>
          </div>
        </div>
      ` : ''}
    </div>
  `;
}

// ── Step G: Validate & Finish ─────────────────────────────────────────────────

async function _renderStepFinish(el) {
  el.innerHTML = `
    <div class="cw-step-pane">
      <div class="cw-pane-head">
        <h3>Validate & Finish Setup</h3>
        <p>Running non-destructive preflight validation across all configured components…</p>
      </div>
      <div id="cwValidationResults" class="cw-validation-box">
        <span class="cw-spinner"></span> Validating configuration…
      </div>
    </div>
  `;

  try {
    const res = await api('/api/customer-wizard/validate', { method: 'POST', body: '{}' });
    CUSTOMER_WIZARD.validation = res;
    _renderValidationBox(res);
  } catch (err) {
    const box = $('cwValidationResults');
    if (box) {
      box.innerHTML = `<div class="cw-check-item failed">✕ Validation failed to run: ${err.message}</div>`;
    }
  }
}

function _renderValidationBox(res) {
  const box = $('cwValidationResults');
  if (!box) return;

  const checks = res.checks || [];
  box.innerHTML = `
    <div class="cw-checks-list">
      ${checks.map(c => {
        const icon = c.status === 'passed' ? '✓' : (c.status === 'skipped' ? '—' : '⚠️');
        const cls = `cw-check-item ${c.status}`;
        return `
          <div class="${cls}">
            <span class="cw-check-icon">${icon}</span>
            <div class="cw-check-info">
              <strong>${_escapeHtml(c.title)}</strong>
              <small>${_escapeHtml(c.detail)}</small>
            </div>
            <span class="cw-check-badge">${_escapeHtml(c.status)}</span>
          </div>
        `;
      }).join('')}
    </div>

    <div class="cw-summary-card">
      <h4>Setup Summary</h4>
      <ul>
        <li><strong>Company:</strong> ${_escapeHtml(CUSTOMER_WIZARD.draft.company.company_name || 'Matika')} (${_escapeHtml(CUSTOMER_WIZARD.draft.company.assistant_name || 'Lisa')})</li>
        <li><strong>AI Model:</strong> ${_escapeHtml(CUSTOMER_WIZARD.draft.provider.model || 'Configured')}</li>
        <li><strong>Active Capabilities:</strong> ${(CUSTOMER_WIZARD.draft.capabilities || []).join(', ') || 'None selected'}</li>
        <li><strong>Daily Briefing:</strong> ${CUSTOMER_WIZARD.draft.briefing.enabled ? `Scheduled at ${CUSTOMER_WIZARD.draft.briefing.time}` : 'Disabled'}</li>
      </ul>
    </div>
  `;
}

async function _applyCustomerWizard() {
  const nextBtn = $('customerWizardNextBtn');
  if (nextBtn) {
    nextBtn.disabled = true;
    nextBtn.innerHTML = '<span class="cw-spinner"></span> Applying…';
  }

  try {
    const res = await api('/api/customer-wizard/apply', { method: 'POST', body: '{}' });
    if (res && res.ok) {
      CUSTOMER_WIZARD.applied = true;
      _showWizardNotice('✓ Setup successfully applied and saved!', 'success');
      _renderCustomerWizardSteps();
    } else {
      _showWizardNotice('Failed to apply setup: ' + ((res && res.message) || 'Unknown error'), 'error');
    }
  } catch (err) {
    _showWizardNotice('Apply error: ' + err.message, 'error');
  } finally {
    if (nextBtn) nextBtn.disabled = false;
  }
}

// ── Utility Helpers ───────────────────────────────────────────────────────────

function _escapeHtml(str) {
  return String(str || '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function _formatStatus(st) {
  const map = {
    available: 'Available',
    requires_configuration: 'Config Needed',
    missing_dependency: 'Missing Dependency',
    unsupported: 'Unsupported',
    verified: 'Verified',
    configured: 'Configured',
    not_connected: 'Not Connected',
    error: 'Error',
  };
  return map[st] || st;
}

// ── Wire into Boot ────────────────────────────────────────────────────────────

window.addEventListener('DOMContentLoaded', () => {
  // Check customer wizard status after boot
  setTimeout(() => {
    loadCustomerWizard({ force: false });
  }, 600);
});
