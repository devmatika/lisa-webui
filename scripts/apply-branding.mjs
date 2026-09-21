#!/usr/bin/env node
/**
 * White-label "build" step for hermes-webui / lisa-webui.
 *
 * There is no JS bundler. This script validates config/branding.json and
 * prints the effective branding that will be used when the Python server
 * starts (file defaults + process env overrides).
 *
 * Usage:
 *   NEXT_PUBLIC_APP_NAME="Profax AI Assistant" npm run build
 */
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(__dirname, '..');
const brandingPath = path.join(root, 'config', 'branding.json');

const ENV_MAP = {
  appName: ['NEXT_PUBLIC_APP_NAME', 'WEBUI_APP_NAME', 'HERMES_WEBUI_BOT_NAME'],
  companyName: ['NEXT_PUBLIC_COMPANY_NAME', 'WEBUI_COMPANY_NAME'],
  logo: ['NEXT_PUBLIC_LOGO', 'WEBUI_LOGO'],
  favicon: ['NEXT_PUBLIC_FAVICON', 'WEBUI_FAVICON'],
  primaryColor: ['NEXT_PUBLIC_PRIMARY_COLOR', 'WEBUI_PRIMARY_COLOR'],
  primaryColorDark: ['NEXT_PUBLIC_PRIMARY_COLOR_DARK', 'WEBUI_PRIMARY_COLOR_DARK'],
  supportEmail: ['NEXT_PUBLIC_SUPPORT_EMAIL', 'WEBUI_SUPPORT_EMAIL'],
  docsUrl: ['NEXT_PUBLIC_DOCS_URL', 'WEBUI_DOCS_URL'],
  tagline: ['NEXT_PUBLIC_TAGLINE', 'WEBUI_TAGLINE'],
};

function firstEnv(names) {
  for (const name of names) {
    const v = process.env[name];
    if (v && String(v).trim()) return String(v).trim();
  }
  return null;
}

if (!fs.existsSync(brandingPath)) {
  console.error('Missing config/branding.json');
  process.exit(1);
}

let branding;
try {
  branding = JSON.parse(fs.readFileSync(brandingPath, 'utf8'));
} catch (err) {
  console.error('Invalid config/branding.json:', err.message);
  process.exit(1);
}

const required = ['appName', 'companyName', 'logo', 'favicon', 'primaryColor'];
for (const key of required) {
  if (!branding[key] || !String(branding[key]).trim()) {
    console.error(`branding.json missing required key: ${key}`);
    process.exit(1);
  }
}

const effective = { ...branding };
for (const [key, names] of Object.entries(ENV_MAP)) {
  const override = firstEnv(names);
  if (override) effective[key] = override;
}

const outPath = path.join(root, 'static', 'branding.generated.json');
fs.writeFileSync(outPath, JSON.stringify(effective, null, 2) + '\n');

console.log('White-label branding OK');
console.log(JSON.stringify(effective, null, 2));
console.log(`Wrote ${path.relative(root, outPath)}`);
console.log('(Python server still reads config/branding.json + live env at runtime.)');
