# White-label deployment (one customer = one isolated instance)

This WebUI is **not** multi-tenant SaaS. Each customer gets:

- their own server / VM / container
- their own domain
- their own Hermes Agent backend
- their own branded WebUI process (same repo, different env)

There is **no** tenant switcher, customer admin, or billing.

> **Stack note:** this project is Python + vanilla JS (no Next.js bundler).
> `NEXT_PUBLIC_*` env names are supported as deploy-friendly aliases; the
> server applies branding at **runtime** when serving HTML/manifest.

---

## Branding source of truth

| File | Role |
|------|------|
| `config/branding.json` | Runtime defaults (Python reads this) |
| `config/branding.ts` | Typed mirror for docs / IDE |
| `api/branding.py` | Loads JSON + env overrides |
| `static/branding.js` | Applies logo / favicon / accent / i18n brandify in the browser |

Default (this fork): **Matika AI Assistant**.

---

## Environment variables

Any of these override `config/branding.json` (first non-empty wins):

| Purpose | Preferred | Aliases |
|---------|-----------|---------|
| App / assistant name | `NEXT_PUBLIC_APP_NAME` | `WEBUI_APP_NAME`, `HERMES_WEBUI_BOT_NAME` |
| Company / short name | `NEXT_PUBLIC_COMPANY_NAME` | `WEBUI_COMPANY_NAME` |
| Logo URL/path | `NEXT_PUBLIC_LOGO` | `WEBUI_LOGO` |
| Favicon URL/path | `NEXT_PUBLIC_FAVICON` | `WEBUI_FAVICON` |
| Primary accent | `NEXT_PUBLIC_PRIMARY_COLOR` | `WEBUI_PRIMARY_COLOR` |
| Dark accent | `NEXT_PUBLIC_PRIMARY_COLOR_DARK` | `WEBUI_PRIMARY_COLOR_DARK` |
| Tagline | `NEXT_PUBLIC_TAGLINE` | `WEBUI_TAGLINE` |

Logo/favicon may be:

- a relative static path: `static/matika-logo.png`
- an absolute HTTPS URL: `https://cdn.example.com/logo.svg`

---

## Customer build examples

### Matika (default)

```bash
cd lisa-webui
npm install
npm run lint
npm run build

# run (uses config/branding.json defaults)
./start.sh
# or: python3 server.py
```

### Profax

```bash
export NEXT_PUBLIC_APP_NAME="Profax AI Assistant"
export NEXT_PUBLIC_COMPANY_NAME="Profax"
export NEXT_PUBLIC_LOGO="static/matika-logo.png"
export NEXT_PUBLIC_FAVICON="static/favicon-matika.png"
export NEXT_PUBLIC_PRIMARY_COLOR="#1B4F72"
export NEXT_PUBLIC_PRIMARY_COLOR_DARK="#148F77"

npm run build
./start.sh
```

### BeerCraft

```bash
export NEXT_PUBLIC_APP_NAME="BeerCraft Assistant"
export NEXT_PUBLIC_COMPANY_NAME="BeerCraft"
export NEXT_PUBLIC_PRIMARY_COLOR="#8B4513"

npm run build
./start.sh
```

`npm run build` validates branding and writes `static/branding.generated.json`
(for ops visibility). The live server still reads `config/branding.json` + **live
env** on each process start.

---

## Docker

### Build a branded image

```bash
docker build -t profax-hermes-webui \
  --build-arg NEXT_PUBLIC_APP_NAME="Profax AI Assistant" \
  --build-arg NEXT_PUBLIC_COMPANY_NAME="Profax" \
  --build-arg NEXT_PUBLIC_PRIMARY_COLOR="#1B4F72" \
  .
```

### Or bake defaults and override at runtime

```bash
docker build -t matika-hermes-webui .

docker run --rm -p 8787:8787 \
  -e NEXT_PUBLIC_APP_NAME="Profax AI Assistant" \
  -e NEXT_PUBLIC_COMPANY_NAME="Profax" \
  -e HERMES_WEBUI_PASSWORD='change-me' \
  matika-hermes-webui
```

Compose snippet:

```yaml
services:
  webui:
    image: profax-hermes-webui
    ports:
      - "8787:8787"
    environment:
      NEXT_PUBLIC_APP_NAME: "Profax AI Assistant"
      NEXT_PUBLIC_COMPANY_NAME: "Profax"
      NEXT_PUBLIC_PRIMARY_COLOR: "#1B4F72"
      HERMES_WEBUI_PASSWORD: ${HERMES_WEBUI_PASSWORD}
```

Open: `http://127.0.0.1:8787`

---

## What gets rebranded

- Browser title / PWA manifest name
- Favicon + optional logo (titlebar + empty state)
- Login screen title (uses assistant `bot_name` / branding app name)
- Sidebar / titlebar chrome
- Composer placeholder (“Message …”)
- Loading / restart / offline / onboarding copy (via i18n brandify)
- Help/settings strings that mentioned “Hermes*” product names

Backend Hermes Agent wiring is unchanged — only visible product chrome.

---

## Custom logo assets

1. Drop customer files under `static/` (e.g. `static/customers/profax-logo.svg`)
2. Point `NEXT_PUBLIC_LOGO` / `NEXT_PUBLIC_FAVICON` at those paths
3. Rebuild image or restart the process with the new env

---

## Explicit non-goals

- Multi-tenant routing / customer switching
- Shared SaaS control plane
- Per-request tenant branding from a central DB
- Billing / seat management

One repo → many branded deployments.
