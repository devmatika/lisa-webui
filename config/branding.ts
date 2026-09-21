/**
 * White-label branding defaults for self-hosted customer builds.
 *
 * Canonical runtime source: `config/branding.json` (read by Python).
 * Keep this file in sync when editing defaults.
 *
 * Override at deploy time with env vars (aliases accepted):
 *   NEXT_PUBLIC_APP_NAME / WEBUI_APP_NAME / HERMES_WEBUI_BOT_NAME
 *   NEXT_PUBLIC_COMPANY_NAME / WEBUI_COMPANY_NAME
 *   NEXT_PUBLIC_LOGO / WEBUI_LOGO
 *   NEXT_PUBLIC_FAVICON / WEBUI_FAVICON
 *   NEXT_PUBLIC_PRIMARY_COLOR / WEBUI_PRIMARY_COLOR
 *   NEXT_PUBLIC_PRIMARY_COLOR_DARK / WEBUI_PRIMARY_COLOR_DARK
 *   NEXT_PUBLIC_TAGLINE / WEBUI_TAGLINE
 *
 * This is NOT multi-tenant SaaS — one branded deployment per customer.
 */

export interface Branding {
  appName: string;
  companyName: string;
  logo: string;
  favicon: string;
  primaryColor: string;
  primaryColorDark: string;
  supportEmail: string;
  docsUrl: string;
  tagline: string;
}

export const branding: Branding = {
  appName: "Matika AI Assistant",
  companyName: "Matika",
  logo: "static/matika-logo.png",
  favicon: "static/favicon-matika.png",
  primaryColor: "#006eb3",
  primaryColorDark: "#00908d",
  supportEmail: "",
  docsUrl: "",
  tagline: "Your self-hosted AI assistant",
};

export default branding;
