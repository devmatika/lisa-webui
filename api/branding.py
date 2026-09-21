"""White-label branding for one-customer-per-deployment WebUI builds.

Loads defaults from ``config/branding.json`` and overlays environment
variables. Supports both the historical ``HERMES_WEBUI_*`` / ``WEBUI_*``
names and the ``NEXT_PUBLIC_*`` aliases used in customer deploy docs.

This is intentionally process-scoped (not multi-tenant). Each customer
gets a separate container/server with its own env and Hermes install.
"""

from __future__ import annotations

import json
import logging
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parent.parent
_BRANDING_PATH = _REPO_ROOT / "config" / "branding.json"

_HEX_COLOR_RE = re.compile(r"^#([0-9a-fA-F]{3}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")

_DEFAULTS: dict[str, str] = {
    "appName": "Matika AI Assistant",
    "companyName": "Matika",
    "logo": "static/matika-logo.png",
    "favicon": "static/favicon-matika.png",
    "primaryColor": "#006eb3",
    "primaryColorDark": "#00908d",
    "supportEmail": "",
    "docsUrl": "",
    "tagline": "Your self-hosted AI assistant",
}

# Env key aliases — first non-empty wins.
_ENV_MAP: dict[str, tuple[str, ...]] = {
    "appName": (
        "NEXT_PUBLIC_APP_NAME",
        "WEBUI_APP_NAME",
        "HERMES_WEBUI_BOT_NAME",
    ),
    "companyName": (
        "NEXT_PUBLIC_COMPANY_NAME",
        "WEBUI_COMPANY_NAME",
    ),
    "logo": (
        "NEXT_PUBLIC_LOGO",
        "WEBUI_LOGO",
    ),
    "favicon": (
        "NEXT_PUBLIC_FAVICON",
        "WEBUI_FAVICON",
    ),
    "primaryColor": (
        "NEXT_PUBLIC_PRIMARY_COLOR",
        "WEBUI_PRIMARY_COLOR",
    ),
    "primaryColorDark": (
        "NEXT_PUBLIC_PRIMARY_COLOR_DARK",
        "WEBUI_PRIMARY_COLOR_DARK",
    ),
    "supportEmail": (
        "NEXT_PUBLIC_SUPPORT_EMAIL",
        "WEBUI_SUPPORT_EMAIL",
    ),
    "docsUrl": (
        "NEXT_PUBLIC_DOCS_URL",
        "WEBUI_DOCS_URL",
    ),
    "tagline": (
        "NEXT_PUBLIC_TAGLINE",
        "WEBUI_TAGLINE",
    ),
}


def _first_env(*names: str) -> str | None:
    for name in names:
        raw = os.environ.get(name)
        if raw is None:
            continue
        value = str(raw).strip()
        if value:
            return value
    return None


def _safe_color(value: str | None, fallback: str) -> str:
    if value and _HEX_COLOR_RE.match(value.strip()):
        return value.strip()
    return fallback


def _safe_path(value: str | None, fallback: str) -> str:
    """Allow relative static paths or absolute https URLs; reject schemes like javascript:."""
    if not value:
        return fallback
    cleaned = value.strip()
    if not cleaned:
        return fallback
    lower = cleaned.lower()
    if lower.startswith(("javascript:", "data:", "vbscript:")):
        return fallback
    if cleaned.startswith(("http://", "https://", "/", "./", "static/")):
        return cleaned
    # Bare filename → treat as static asset
    if "/" not in cleaned and "\\" not in cleaned:
        return f"static/{cleaned}"
    return fallback


def _load_file_defaults() -> dict[str, str]:
    merged = dict(_DEFAULTS)
    try:
        if _BRANDING_PATH.is_file():
            data = json.loads(_BRANDING_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                for key in _DEFAULTS:
                    if key in data and data[key] is not None:
                        merged[key] = str(data[key]).strip()
    except Exception:
        logger.warning("Failed to read %s; using built-in branding defaults", _BRANDING_PATH, exc_info=True)
    return merged


@lru_cache(maxsize=1)
def get_branding() -> dict[str, str]:
    """Return the effective branding dict for this process."""
    base = _load_file_defaults()
    out = dict(base)
    for key, env_names in _ENV_MAP.items():
        override = _first_env(*env_names)
        if override is None:
            continue
        if key in ("primaryColor", "primaryColorDark"):
            out[key] = _safe_color(override, base[key])
        elif key in ("logo", "favicon"):
            out[key] = _safe_path(override, base[key])
        else:
            out[key] = override
    return out


def clear_branding_cache() -> None:
    """Test helper — drop the process-scoped branding cache."""
    get_branding.cache_clear()


def default_bot_name() -> str:
    """Assistant display name used when settings.bot_name is unset/empty."""
    return get_branding()["appName"] or _DEFAULTS["appName"]


def branding_json_literal() -> str:
    """JSON literal safe to embed in a ``<script>`` tag (``</`` escaped)."""
    return json.dumps(get_branding(), ensure_ascii=False, separators=(",", ":")).replace(
        "</", "<\\/"
    )


def apply_branding_to_html(html: str) -> str:
    """Substitute branding tokens in an HTML shell string."""
    b = get_branding()
    # Escape for HTML text/attr contexts (not for the JSON script literal).
    import html as _html

    app = _html.escape(b["appName"])
    company = _html.escape(b["companyName"])
    tagline = _html.escape(b.get("tagline") or "")
    logo = _html.escape(b["logo"])
    favicon = _html.escape(b["favicon"])
    primary = _html.escape(b["primaryColor"])
    primary_dark = _html.escape(b["primaryColorDark"])

    return (
        html.replace("__BRANDING_APP_NAME__", app)
        .replace("__BRANDING_COMPANY_NAME__", company)
        .replace("__BRANDING_TAGLINE__", tagline)
        .replace("__BRANDING_LOGO__", logo)
        .replace("__BRANDING_FAVICON__", favicon)
        .replace("__BRANDING_PRIMARY__", primary)
        .replace("__BRANDING_PRIMARY_DARK__", primary_dark)
        .replace("__BRANDING_JSON__", branding_json_literal())
    )


def branded_manifest(raw: bytes | str | dict[str, Any]) -> bytes:
    """Return manifest.json bytes with name/short_name/description from branding."""
    if isinstance(raw, (bytes, bytearray)):
        data = json.loads(raw.decode("utf-8"))
    elif isinstance(raw, str):
        data = json.loads(raw)
    else:
        data = dict(raw)

    b = get_branding()
    data["name"] = b["appName"]
    data["short_name"] = b["companyName"] or b["appName"]
    data["description"] = b.get("tagline") or f"{b['appName']} Web UI"
    if b.get("favicon"):
        # Prefer configured favicon as the any-size SVG/icon when present.
        icons = data.get("icons")
        if isinstance(icons, list) and icons:
            for icon in icons:
                if isinstance(icon, dict) and icon.get("sizes") == "any":
                    icon["src"] = b["favicon"]
                    break
    # Soften shortcut copy that still says Hermes
    for shortcut in data.get("shortcuts") or []:
        if not isinstance(shortcut, dict):
            continue
        desc = shortcut.get("description")
        if isinstance(desc, str) and "Hermes" in desc:
            shortcut["description"] = desc.replace("Hermes", b["appName"])
    return json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
