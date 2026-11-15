/**
 * Lisa Enterprise menu entries (loaded only when LISA_ENTERPRISE is on).
 *
 * Adds "My account" and the Organization pages (Users, Roles, Knowledge, Audit log) to the icon
 * rail, the mobile nav and the settings side menu, each shown only when the signed-in user's
 * permissions allow it. Hides operator-only settings and the setup wizard from everyone else.
 * The server enforces every permission on its own; this only keeps the menu honest.
 */
(function () {
  'use strict';
  window.LISA_ENTERPRISE = true;
  // Each person has exactly one private profile, named by an internal id: no profile switcher.
  const style = document.createElement('style');
  style.textContent = '#profileChipWrap{display:none!important}';
  document.head.appendChild(style);

  const svg = (d) => `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${d}</svg>`;
  const ICONS = {
    org: svg('<rect x="4" y="2" width="16" height="20" rx="2"/><path d="M9 22v-4h6v4"/><path d="M8 6h.01M16 6h.01M12 6h.01M8 10h.01M16 10h.01M12 10h.01M8 14h.01M16 14h.01M12 14h.01"/>'),
    account: svg('<path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/>'),
    users: svg('<path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/>'),
    roles: svg('<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/>'),
    knowledge: svg('<path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/>'),
    audit: svg('<path d="M22 12h-4l-3 9L9 3l-3 9H2"/>'),
    guide: svg('<circle cx="12" cy="12" r="10"/><path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3M12 17h.01"/>'),
  };
  // [admin page tab, label, permission or null for every signed-in user]
  const PAGES = [
    ['account', 'My account', null],
    ['users', 'Users', 'org.users.manage'],
    ['roles', 'Roles', 'org.users.manage'],
    ['knowledge', 'Knowledge', 'knowledge.read'],
    ['audit', 'Status & audit log', 'org.audit.read'],
    ['guide', 'Guide', null],
  ];
  // Settings sections and buttons that only work for operators.
  const OPERATOR_ONLY = [
    ['[data-settings-section="providers"]', 'operator.providers'],
    ['[data-settings-section="plugins"]', 'operator.extensions'],
    ['[data-settings-section="extensions"]', 'operator.extensions'],
    ['[data-settings-section="system"]', 'operator.system'],
    ['#sidebarSetupWizardBtn', 'operator.setup'],
    ['#btnRunSetupWizardAgain', 'operator.setup'],
  ];

  const go = (tab) => { location.href = '/lisa#' + tab; };

  function railButton(tab, label, mobile) {
    const b = document.createElement('button');
    b.type = 'button';
    b.className = mobile ? 'nav-tab has-tooltip has-tooltip--bottom lisa-org-nav' : 'rail-btn nav-tab has-tooltip lisa-org-nav';
    b.dataset.tooltip = label;
    b.setAttribute('aria-label', label);
    b.innerHTML = ICONS.org;
    b.addEventListener('click', () => go(tab));
    return b;
  }

  function addRail(home) {
    const label = home === 'users' ? 'Organization' : 'My account';
    const rail = document.querySelector('.rail');
    const spacer = rail && rail.querySelector('.rail-spacer');
    if (rail) rail.insertBefore(railButton(home, label, false), spacer || null);
    const nav = document.querySelector('.sidebar-nav');
    const settings = nav && nav.querySelector('[data-panel="settings"]');
    if (nav) nav.insertBefore(railButton(home, label, true), settings || null);
  }

  function addSideMenu(pages) {
    const menu = document.querySelector('.settings-menu-items');
    if (!menu) return;
    const anchor = menu.querySelector('.side-menu-divider');
    const head = document.createElement('div');
    head.textContent = 'Organization';
    head.style.cssText = 'margin:10px 10px 4px;font-size:11px;font-weight:600;letter-spacing:.06em;text-transform:uppercase;color:var(--muted)';
    menu.insertBefore(head, anchor);
    pages.forEach(([tab, label]) => {
      const b = document.createElement('button');
      b.type = 'button';
      b.className = 'side-menu-item';
      b.innerHTML = ICONS[tab].replace('width="18" height="18"', 'width="16" height="16"') + `<span>${label}</span>`;
      b.addEventListener('click', () => go(tab));
      menu.insertBefore(b, anchor);
    });
  }

  function hideOperatorOnly(perms) {
    OPERATOR_ONLY.forEach(([sel, perm]) => {
      if (perms.has(perm)) return;
      document.querySelectorAll(sel).forEach((el) => {
        const field = el.closest('.settings-field');
        (sel.startsWith('#btn') && field ? field : el).style.display = 'none';
      });
    });
  }

  async function init() {
    let me;
    try {
      const r = await fetch('/api/enterprise/me', { credentials: 'include' });
      if (!r.ok) return;
      me = await r.json();
    } catch (_) {
      return;
    }
    window.LISA_ME = me;
    if (typeof applyBotName === 'function') applyBotName();  // boot may have named the assistant before this ran
    const perms = new Set(me.permissions || []);
    const pages = PAGES.filter(([, , perm]) => !perm || perms.has(perm));
    addRail(perms.has('org.users.manage') ? 'users' : 'account');
    addSideMenu(pages);
    hideOperatorOnly(perms);
    // /#setup (linked from the Organization page) opens the setup wizard where it was left.
    if (location.hash === '#setup' && perms.has('operator.setup') && typeof loadCustomerWizard === 'function') {
      history.replaceState(null, '', location.pathname + location.search);
      loadCustomerWizard({ force: true });
    }
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
