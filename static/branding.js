/**
 * Client-side white-label branding applicator.
 *
 * Expects ``window.__BRANDING__`` (injected by the server from
 * config/branding.json + env overrides). Falls back to Matika defaults.
 *
 * Loaded early (sync, before deferred app scripts) so ``t()`` and
 * ``assistantDisplayName()`` can read branded values on first paint.
 */
(function (global) {
  'use strict';

  var DEFAULTS = {
    appName: 'Matika AI Assistant',
    companyName: 'Matika',
    logo: 'static/matika-logo.png',
    favicon: 'static/favicon-matika.png',
    primaryColor: '#006eb3',
    primaryColorDark: '#00908d',
    supportEmail: '',
    docsUrl: '',
    tagline: 'Your self-hosted AI assistant',
  };

  function mergeBranding(raw) {
    var out = {};
    var key;
    for (key in DEFAULTS) {
      if (Object.prototype.hasOwnProperty.call(DEFAULTS, key)) {
        out[key] = DEFAULTS[key];
      }
    }
    if (raw && typeof raw === 'object') {
      for (key in raw) {
        if (Object.prototype.hasOwnProperty.call(raw, key) && raw[key] != null && String(raw[key]).trim() !== '') {
          out[key] = String(raw[key]);
        }
      }
    }
    return out;
  }

  var branding = mergeBranding(global.__BRANDING__);
  global.__BRANDING__ = branding;

  function defaultBotName() {
    return branding.appName || DEFAULTS.appName;
  }

  /**
   * Replace upstream "Hermes*" product strings with the customer brand.
   * Longer phrases first so "Hermes Web UI" is not partially mangled.
   */
  function brandifyText(str) {
    if (str == null) return str;
    if (typeof str !== 'string') return str;
    var app = branding.appName || DEFAULTS.appName;
    var company = branding.companyName || app;
    return str
      .replace(/Hermes Web UI/g, app)
      .replace(/Hermes WebUI/g, app)
      .replace(/Hermes Agent/g, app)
      .replace(/Hermes/g, company);
  }

  function setFavicon(href) {
    if (!href || !document.head) return;
    var links = document.querySelectorAll('link[rel="icon"], link[rel="shortcut icon"]');
    if (!links.length) {
      var link = document.createElement('link');
      link.rel = 'icon';
      link.type = href.indexOf('.png') !== -1 ? 'image/png' : '';
      link.href = href;
      document.head.appendChild(link);
      return;
    }
    for (var i = 0; i < links.length; i++) {
      links[i].href = href;
      if (href.indexOf('.png') !== -1) {
        links[i].type = 'image/png';
      }
    }
  }

  function setLogoImages() {
    var logo = branding.logo;
    var mark = branding.favicon || logo;
    if (!logo && !mark) return;

    function paint(el, src, opts) {
      if (el.querySelector && el.querySelector('img[data-brand-img]')) return;
      var img = document.createElement('img');
      img.src = src;
      img.alt = branding.appName || '';
      img.setAttribute('data-brand-img', '1');
      img.style.display = 'block';
      img.style.width = 'auto';
      img.style.height = opts.height;
      img.style.maxWidth = opts.maxWidth || '100%';
      img.style.objectFit = 'contain';
      if (opts.borderRadius) img.style.borderRadius = opts.borderRadius;
      el.innerHTML = '';
      el.appendChild(img);
      el.setAttribute('aria-label', branding.appName || 'logo');
    }

    var emptyNodes = document.querySelectorAll('[data-brand-logo], .empty-logo');
    for (var i = 0; i < emptyNodes.length; i++) {
      if (!logo) break;
      paint(emptyNodes[i], logo, { height: '56px', maxWidth: 'min(280px, 80vw)' });
    }

    var titleNodes = document.querySelectorAll('.app-titlebar-icon');
    for (var j = 0; j < titleNodes.length; j++) {
      paint(titleNodes[j], mark || logo, { height: '18px', maxWidth: '48px' });
    }
  }

  function applyPrimaryColors() {
    var root = document.documentElement;
    if (!root || !root.style) return;
    if (branding.primaryColor) {
      root.style.setProperty('--brand-primary', branding.primaryColor);
      // Soft default-skin accent until the user picks another skin.
      if (!root.dataset.skin || root.dataset.skin === 'default') {
        root.style.setProperty('--accent', branding.primaryColor);
        root.style.setProperty('--accent-hover', branding.primaryColorDark || branding.primaryColor);
        root.style.setProperty('--accent-text', branding.primaryColor);
      }
    }
    if (branding.primaryColorDark) {
      root.style.setProperty('--brand-primary-dark', branding.primaryColorDark);
    }
  }

  function applyChromeTitles() {
    var app = defaultBotName();
    var titleEl = document.getElementById('appTitlebarTitle');
    if (titleEl && (!titleEl.textContent || /^(Hermes|Matika)/i.test(titleEl.textContent.trim()))) {
      // Only replace the shell default — session titles are owned elsewhere.
      if (!global.S || !global.S.session) {
        titleEl.textContent = branding.companyName || app;
      }
    }
    var apple = document.querySelector('meta[name="apple-mobile-web-app-title"]');
    if (apple) apple.setAttribute('content', branding.companyName || app);
    if (!global.S || !global.S.session) {
      if (!document.title || document.title === 'Hermes' || document.title === '__BRANDING_APP_NAME__') {
        document.title = app;
      }
    }
    var msg = document.getElementById('msg');
    if (msg && (!msg.placeholder || /Message Hermes/i.test(msg.placeholder))) {
      msg.placeholder = 'Message ' + app + '\u2026';
    }
  }

  function applyBranding() {
    applyPrimaryColors();
    setFavicon(branding.favicon);
    setLogoImages();
    applyChromeTitles();
  }

  // Public API
  global.Branding = {
    get: function () { return branding; },
    defaultBotName: defaultBotName,
    brandify: brandifyText,
    apply: applyBranding,
  };

  // Seed bot name before settings load so first paint is branded.
  if (!global._botName) {
    global._botName = defaultBotName();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', applyBranding);
  } else {
    applyBranding();
  }
})(typeof window !== 'undefined' ? window : this);
