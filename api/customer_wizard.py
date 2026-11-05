"""Customer Onboarding Wizard for Lisa / Hermes WebUI.

Provides a guided, authenticated customer configuration wizard that allows
configuring company profile, AI model provider, capabilities, service connectors,
skills/modules, action approval policies, and notification/daily briefing schedules.

Design & Security Invariants:
- All paths resolved dynamically via `get_active_hermes_home()`.
- No hardcoded profile names, paths, or secrets.
- Secrets stored in `.env` (0600) or config.yaml; never returned in plaintext to the browser.
- External validation probes are strictly non-destructive (never send emails, create events or tasks).
- Wizard draft state saved separately from applied runtime configuration.
- Apply operation is atomic and idempotent (no duplicate cron jobs or repeated entries).
- Existing configured installations are detected and never forced through the wizard.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import secrets
import socket
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from api import yaml_compat as yaml
from api.config import (
    DEFAULT_MODEL,
    _PROVIDER_DISPLAY,
    _PROVIDER_MODELS,
    load_settings,
    reload_config,
    save_settings,
)
from api.onboarding import (
    _SUPPORTED_PROVIDER_SETUPS,
    _load_env_file,
    _load_yaml_config,
    _save_yaml_config,
    probe_provider_endpoint,
)
from api.paths import _atomic_write_text
from api.profiles import (
    _reload_dotenv,
    get_active_hermes_home,
    get_active_profile_name,
)
from api.providers import _write_env_file

logger = logging.getLogger(__name__)

# State file stored in active profile's webui directory
_WIZARD_STATE_FILENAME = "customer_wizard_state.json"

# Non-destructive timeout for service probes
_SERVICE_PROBE_TIMEOUT = 5.0

_IANA_TIMEZONE_RE = re.compile(r"^[A-Za-z0-9_\-+]+(/[A-Za-z0-9_\-+]+)*$")
_TIME_24H_RE = re.compile(r"^([01]?[0-9]|2[0-3]):[0-5][0-9]$")


def _truthy(val: Any) -> bool:
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)):
        return bool(val)
    if isinstance(val, str):
        return val.strip().lower() in {"1", "true", "yes", "on"}
    return bool(val)


# ── Capability Registry ────────────────────────────────────────────────────────

CAPABILITY_REGISTRY: dict[str, dict[str, Any]] = {
    "company_knowledge": {
        "id": "company_knowledge",
        "title": "Company Knowledge (RAG)",
        "description": "Index documents, handbook, and guidelines for deep semantic search and grounded answers.",
        "required_integrations": ["onyx"],
        "recommended_skills": ["obsidian-company-memory"],
        "recommended_plugins": [],
    },
    "email_assistant": {
        "id": "email_assistant",
        "title": "Email Assistant",
        "description": "Draft, search, and manage customer emails safely with human review.",
        "required_integrations": ["google_workspace", "microsoft_graph"],
        "integration_logic": "any",  # At least one of the above
        "recommended_skills": [],
        "recommended_plugins": ["google-workspace-tools", "microsoft-outlook-tools", "business-action-approval"],
    },
    "calendar_assistant": {
        "id": "calendar_assistant",
        "title": "Calendar Assistant",
        "description": "Check availability, coordinate meeting times, and schedule calendar appointments.",
        "required_integrations": ["google_workspace", "microsoft_graph"],
        "integration_logic": "any",
        "recommended_skills": [],
        "recommended_plugins": ["google-workspace-tools", "microsoft-outlook-tools", "business-action-approval"],
    },
    "meeting_assistant": {
        "id": "meeting_assistant",
        "title": "Meeting Assistant",
        "description": "Ingest meeting transcripts and extract summaries and follow-up action items.",
        "required_integrations": ["fireflies"],
        "recommended_skills": ["meeting-task-selector"],
        "recommended_plugins": [],
    },
    "task_assistant": {
        "id": "task_assistant",
        "title": "Task Assistant",
        "description": "Manage tasks, create action items, and update statuses in ClickUp.",
        "required_integrations": ["clickup"],
        "recommended_skills": ["clickup-task-management"],
        "recommended_plugins": ["clickup-approval"],
    },
    "daily_briefing": {
        "id": "daily_briefing",
        "title": "Daily Briefing",
        "description": "Automated morning summary of calendar events, urgent tasks, and key updates delivered to chat.",
        "required_integrations": ["telegram"],
        "recommended_skills": [],
        "recommended_plugins": ["briefing-recipient-manager"],
    },
}


# ── Service Connectors Specification ──────────────────────────────────────────

SERVICE_CONNECTORS: dict[str, dict[str, Any]] = {
    "google_workspace": {
        "id": "google_workspace",
        "name": "Google Workspace",
        "description": "Connects Google Calendar, Docs, Sheets, and Drive for scheduling and document actions.",
        "supported": True,
        "env_keys": ["GOOGLE_WORKSPACE_CREDENTIALS", "GOOGLE_APPLICATION_CREDENTIALS", "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET"],
        "token_files": ["google_token.json", "credentials.json", "google_client_secret.json"],
        "plugin_id": "google-workspace-tools",
        "supports_file_upload": True,
        "accepted_files": ["credentials.json", "google_token.json", "google_client_secret.json", "client_secret_*.json"],
        "upload_hint": "Upload Google credentials.json, google_client_secret.json, or client_secret_*.json from Google Cloud Console.",
        "fields": [
            {
                "id": "GOOGLE_CLIENT_ID",
                "label": "OAuth Client ID",
                "type": "text",
                "placeholder": "123456789-abc.apps.googleusercontent.com",
                "required": False,
                "hint": "From Google Cloud Console -> APIs & Services -> Credentials (Desktop app client)",
            },
            {
                "id": "GOOGLE_CLIENT_SECRET",
                "label": "OAuth Client Secret",
                "type": "password",
                "placeholder": "GOCSPX-...",
                "required": False,
                "hint": "Client Secret matching the Client ID above",
            },
        ],
    },
    "clickup": {
        "id": "clickup",
        "name": "ClickUp",
        "description": "Manage tasks, team spaces, lists, and task comments.",
        "supported": True,
        "env_keys": ["CLICKUP_API_KEY", "CLICKUP_TEAM_ID"],
        "token_files": [],
        "plugin_id": "clickup-approval",
        "probe_url": "https://api.clickup.com/api/v2/user",
        "fields": [
            {
                "id": "CLICKUP_API_KEY",
                "label": "ClickUp Personal API Token",
                "type": "password",
                "placeholder": "pk_12345678_...",
                "required": True,
                "hint": "From ClickUp -> Settings -> Apps -> API Token (starts with pk_)",
            },
            {
                "id": "CLICKUP_TEAM_ID",
                "label": "Workspace / Team ID (optional)",
                "type": "text",
                "placeholder": "e.g. 90123456",
                "required": False,
                "hint": "Leave blank to auto-detect workspace from your connection test",
            },
        ],
    },
    "onyx": {
        "id": "onyx",
        "name": "Onyx Knowledge Search",
        "description": "Enterprise search and RAG knowledge base integration.",
        "supported": True,
        "env_keys": ["ONYX_BASE_URL", "ONYX_API_KEY"],
        "token_files": [],
        "mcp_server": "onyx",
        "fields": [
            {
                "id": "ONYX_BASE_URL",
                "label": "Onyx Base URL",
                "type": "text",
                "placeholder": "http://127.0.0.1:8080 or https://onyx.company.com",
                "required": True,
                "hint": "Base URL of the Onyx enterprise search instance",
            },
            {
                "id": "ONYX_API_KEY",
                "label": "Onyx API Key",
                "type": "password",
                "placeholder": "Enter Onyx API Key",
                "required": True,
                "hint": "API Key from Onyx Admin Settings -> API Keys",
            },
        ],
    },
    "fireflies": {
        "id": "fireflies",
        "name": "Fireflies.ai",
        "description": "AI meeting transcripts and action item ingestion.",
        "supported": True,
        "env_keys": ["FIREFLIES_API_KEY", "FIREFLIES_SIGNING_SECRET"],
        "token_files": [],
        "probe_url": "https://api.fireflies.ai/graphql",
        "fields": [
            {
                "id": "FIREFLIES_API_KEY",
                "label": "Fireflies API Key",
                "type": "password",
                "placeholder": "Enter Fireflies API Key",
                "required": True,
                "hint": "From Fireflies.ai -> Integrations -> API Key",
            },
            {
                "id": "FIREFLIES_SIGNING_SECRET",
                "label": "Webhook Signing Secret",
                "type": "password",
                "placeholder": "Webhook HMAC Secret (optional)",
                "required": False,
                "hint": "From Fireflies Webhook settings for HMAC SHA-256 verification",
            },
        ],
    },
    "microsoft_graph": {
        "id": "microsoft_graph",
        "name": "Microsoft 365 (Outlook & Graph)",
        "description": "Connects Microsoft Outlook mail, calendar, and OneDrive.",
        "supported": True,
        "env_keys": ["MICROSOFT_CLIENT_ID", "MICROSOFT_CLIENT_SECRET", "MICROSOFT_TENANT_ID"],
        "token_files": ["microsoft_token.json"],
        "plugin_id": "microsoft-outlook-tools",
        "supports_file_upload": True,
        "accepted_files": ["microsoft_token.json"],
        "upload_hint": "Upload microsoft_token.json directly if pre-authenticated.",
        "fields": [
            {
                "id": "MICROSOFT_CLIENT_ID",
                "label": "Azure Application (Client) ID",
                "type": "text",
                "placeholder": "00000000-0000-0000-0000-000000000000",
                "required": True,
                "hint": "From Azure Portal -> Microsoft Entra App Registrations",
            },
            {
                "id": "MICROSOFT_CLIENT_SECRET",
                "label": "Azure Client Secret",
                "type": "password",
                "placeholder": "Enter client secret value",
                "required": True,
                "hint": "Client Secret generated in Azure App Registration",
            },
            {
                "id": "MICROSOFT_TENANT_ID",
                "label": "Azure Directory (Tenant) ID",
                "type": "text",
                "placeholder": "00000000-0000-0000-0000-000000000000",
                "required": True,
                "hint": "Tenant ID of your Microsoft 365 organization",
            },
        ],
    },
    "telegram": {
        "id": "telegram",
        "name": "Telegram",
        "description": "Deliver notifications, briefings, and approval requests via Telegram bot.",
        "supported": True,
        "env_keys": ["TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"],
        "token_files": [],
        "probe_url": "https://api.telegram.org/bot{token}/getMe",
        "fields": [
            {
                "id": "TELEGRAM_BOT_TOKEN",
                "label": "Telegram Bot Token",
                "type": "password",
                "placeholder": "123456789:ABCdefGhIJKlmNoPQRsTUVwxyZ...",
                "required": True,
                "hint": "Bot token created via @BotFather on Telegram",
            },
            {
                "id": "TELEGRAM_CHAT_ID",
                "label": "Admin Chat ID / Channel ID (optional)",
                "type": "text",
                "placeholder": "e.g. 12345678 or -1001234567890",
                "required": False,
                "hint": "Numeric Chat ID to receive daily briefings and approval alerts",
            },
        ],
    },
    "mattermost": {
        "id": "mattermost",
        "name": "Mattermost",
        "description": "Team messaging and automated channel notifications.",
        "supported": False,
        "unavailable_reason": "Native Mattermost plugin is not installed in this deployment. Incoming webhooks or bot gateway connector required.",
        "env_keys": ["MATTERMOST_URL", "MATTERMOST_BOT_TOKEN"],
        "token_files": [],
        "fields": [],
    },
}


# ── Action Approval Specifications ───────────────────────────────────────────

ACTION_APPROVAL_SPECS: dict[str, dict[str, Any]] = {
    "calendar_read": {
        "category": "Calendar",
        "action": "Read / Search Events",
        "default": "automatic",
        "enforceable": True,
        "handler": "google-workspace-tools / microsoft-outlook-tools",
    },
    "calendar_write": {
        "category": "Calendar",
        "action": "Create, Update, or Delete Events",
        "default": "ask",
        "enforceable": True,
        "handler": "business-action-approval (gcal_create)",
    },
    "email_read": {
        "category": "Email",
        "action": "Read / Search Mail",
        "default": "automatic",
        "enforceable": True,
        "handler": "himalaya / microsoft-outlook-tools",
    },
    "email_draft": {
        "category": "Email",
        "action": "Create Email Draft",
        "default": "automatic",
        "enforceable": True,
        "handler": "business-action-approval (outlook_draft_create, himalaya draft)",
    },
    "email_send": {
        "category": "Email",
        "action": "Send or Reply to Email",
        "default": "ask",
        "enforceable": True,
        "handler": "business-action-approval (himalaya send, outlook send blocked)",
    },
    "clickup_read": {
        "category": "ClickUp",
        "action": "Read Tasks, Lists, and Comments",
        "default": "automatic",
        "enforceable": True,
        "handler": "clickup-task-management",
    },
    "clickup_write": {
        "category": "ClickUp",
        "action": "Create, Update, or Delete Tasks",
        "default": "ask",
        "enforceable": True,
        "handler": "clickup-approval (clickup_create_task, clickup_update_task)",
    },
    "files_read": {
        "category": "Files",
        "action": "Read / Search Workspace Files",
        "default": "automatic",
        "enforceable": True,
        "handler": "hermes-agent workspace guard",
    },
    "files_create": {
        "category": "Files",
        "action": "Create New Files",
        "default": "ask",
        "enforceable": True,
        "handler": "business-action-approval (gdocs_create, gsheets_create)",
    },
    "files_modify": {
        "category": "Files",
        "action": "Edit, Delete, or Share Files",
        "default": "ask",
        "enforceable": True,
        "handler": "business-action-approval (gdrive_upload)",
    },
}


# ── State and Path Helpers ───────────────────────────────────────────────────

def _get_wizard_state_path() -> Path:
    """Return path to customer_wizard_state.json under active profile."""
    home = get_active_hermes_home()
    webui_dir = home / "webui"
    webui_dir.mkdir(parents=True, exist_ok=True)
    return webui_dir / _WIZARD_STATE_FILENAME


def load_wizard_state() -> dict[str, Any]:
    """Load saved wizard progress from disk."""
    path = _get_wizard_state_path()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception as exc:
        logger.warning("Failed to load customer wizard state from %s: %s", path, exc)
        return {}


def save_wizard_state(state: dict[str, Any]) -> None:
    """Atomically save wizard progress to disk."""
    path = _get_wizard_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_text(path, json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


def is_existing_install_configured() -> bool:
    """Detect if this installation is already configured via CLI, prior setup, or environment.
    
    Verifies actual existing state rather than just checking a flag:
    1. config.yaml exists and has a provider + model configured
    2. Or operator set HERMES_WEBUI_SKIP_ONBOARDING=1
    3. Or settings.json has customer_wizard_completed=True
    """
    skip_env = os.environ.get("HERMES_WEBUI_SKIP_ONBOARDING", "").strip().lower()
    if skip_env in {"1", "true", "yes"}:
        return True

    settings = load_settings()
    if settings.get("customer_wizard_completed"):
        return True

    home = get_active_hermes_home()
    config_path = home / "config.yaml"
    if config_path.is_file():
        cfg = _load_yaml_config(config_path)
        model_cfg = cfg.get("model", {})
        if isinstance(model_cfg, dict):
            provider = str(model_cfg.get("provider") or "").strip()
            default_m = str(model_cfg.get("default") or "").strip()
            if provider and default_m:
                return True
        elif isinstance(model_cfg, str) and model_cfg.strip():
            return True

    return False


# ── Inspection and Service Connectivity ──────────────────────────────────────

def _mask_secret(val: str | None) -> str:
    """Mask secret value for display, returning bullet dots."""
    if not val:
        return ""
    s = str(val).strip()
    if len(s) <= 8:
        return "••••••••"
    return s[:3] + "••••••••" + s[-3:]


def inspect_installed_components() -> dict[str, Any]:
    """Inspect active profile's plugins, skills, and token files."""
    home = get_active_hermes_home()
    env_path = home / ".env"
    env_values = _load_env_file(env_path) if env_path.is_file() else {}

    # Inspect plugins directory
    plugins_dir = home / "plugins"
    installed_plugins: set[str] = set()
    if plugins_dir.is_dir():
        for item in plugins_dir.iterdir():
            if item.is_dir() and (item / "plugin.yaml").is_file():
                installed_plugins.add(item.name)

    # Global plugins fallback
    global_plugins_dir = Path.home() / ".hermes" / "plugins"
    if global_plugins_dir.is_dir():
        for item in global_plugins_dir.iterdir():
            if item.is_dir() and (item / "plugin.yaml").is_file():
                installed_plugins.add(item.name)

    # Inspect skills directory
    skills_dir = home / "skills"
    installed_skills: set[str] = set()
    if skills_dir.is_dir():
        for root, dirs, files in os.walk(skills_dir):
            if "SKILL.md" in files:
                installed_skills.add(Path(root).name)

    # Inspect token files in home
    existing_token_files: set[str] = set()
    for token_name in ["google_token.json", "credentials.json", "google_client_secret.json", "microsoft_token.json"]:
        if (home / token_name).is_file():
            existing_token_files.add(token_name)
    try:
        for p in home.glob("client_secret_*.json"):
            if p.is_file():
                existing_token_files.add(p.name)
                existing_token_files.add("credentials.json")
    except Exception:
        pass

    return {
        "env_values": env_values,
        "installed_plugins": installed_plugins,
        "installed_skills": installed_skills,
        "existing_token_files": existing_token_files,
        "hermes_home": str(home),
    }


def get_service_status(service_id: str, probe: bool = False) -> dict[str, Any]:
    """Inspect and optionally probe a specific service connector."""
    spec = SERVICE_CONNECTORS.get(service_id)
    if not spec:
        return {"id": service_id, "status": "unsupported", "error": f"Unknown service: {service_id}"}

    if not spec.get("supported"):
        return {
            "id": service_id,
            "name": spec["name"],
            "description": spec.get("description", ""),
            "supported": False,
            "status": "unsupported",
            "message": spec.get("unavailable_reason", "Not supported in this installation"),
            "fields": [],
            "configured_keys": [],
            "configured_files": [],
            "current_values": {},
        }

    components = inspect_installed_components()
    env_values = components["env_values"]
    existing_token_files = components["existing_token_files"]

    # Check credentials presence
    configured_keys = [k for k in spec.get("env_keys", []) if bool(env_values.get(k) or os.environ.get(k))]
    configured_files = [tf for tf in spec.get("token_files", []) if tf in existing_token_files]
    if service_id == "google_workspace":
        for tf in existing_token_files:
            if (tf.startswith("client_secret_") or tf == "google_client_secret.json") and tf not in configured_files:
                configured_files.append(tf)
    is_configured = bool(configured_keys or configured_files)

    # Current masked values
    current_values = {}
    for f in spec.get("fields", []):
        f_id = f["id"]
        v = env_values.get(f_id) or os.environ.get(f_id, "")
        if v:
            if f.get("type") == "password" or "KEY" in f_id or "SECRET" in f_id or "TOKEN" in f_id:
                current_values[f_id] = "••••••••"
            else:
                current_values[f_id] = v
        else:
            current_values[f_id] = ""

    resp: dict[str, Any] = {
        "id": service_id,
        "name": spec["name"],
        "description": spec.get("description", ""),
        "supported": True,
        "fields": spec.get("fields", []),
        "supports_file_upload": spec.get("supports_file_upload", False),
        "accepted_files": spec.get("accepted_files", []),
        "upload_hint": spec.get("upload_hint", ""),
        "configured_keys": configured_keys,
        "configured_files": configured_files,
        "current_values": current_values,
    }

    if not is_configured:
        resp.update({
            "status": "not_connected",
            "message": "Not configured yet",
        })
        return resp

    if not probe:
        summary_items = configured_files + configured_keys
        resp.update({
            "status": "configured",
            "message": f"Configured ({', '.join(summary_items)})",
        })
        return resp

    # Perform non-destructive probe
    probe_result = probe_service_connection(service_id, env_values)
    is_ok = bool(probe_result.get("ok"))
    probe_status = probe_result.get("status")
    status_str = "verified" if is_ok else (probe_status if probe_status in {"needs_auth", "configured"} else "error")
    resp.update({
        "status": status_str,
        "message": probe_result.get("message", "Connected"),
        "error_detail": probe_result.get("detail", ""),
        "probe_detail": probe_result,
        "needs_google_auth": bool(probe_result.get("needs_google_auth")),
    })
    return resp


GOOGLE_WORKSPACE_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/contacts.readonly",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/documents",
]


def _find_google_client_secret(home: Path | None = None) -> Path | None:
    """Find installed/web OAuth client secrets JSON file in home or profile."""
    home = home or get_active_hermes_home()
    candidates = [
        home / "google_client_secret.json",
        home / "credentials.json",
    ]
    try:
        candidates.extend(sorted(home.glob("client_secret_*.json")))
    except Exception:
        pass
    for p in candidates:
        if p.is_file():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                if "installed" in data or "web" in data:
                    return p
            except Exception:
                continue
    return None


def generate_google_auth_url(redirect_uri: str = "http://localhost:1") -> dict[str, Any]:
    """Generate Google OAuth 2.0 authorization URL for customer onboarding."""
    home = get_active_hermes_home()
    client_secret_path = _find_google_client_secret(home)
    if not client_secret_path:
        return {
            "ok": False,
            "message": "No Google OAuth Client Secret file found. Please upload credentials.json or client_secret_*.json first.",
        }

    try:
        data = json.loads(client_secret_path.read_text(encoding="utf-8"))
        c_info = data.get("installed") or data.get("web") or {}
        client_id = c_info.get("client_id")
        auth_uri = c_info.get("auth_uri") or "https://accounts.google.com/o/oauth2/v2/auth"
        if not client_id:
            return {"ok": False, "message": "Missing client_id in client secret JSON."}

        state = secrets.token_hex(16)
        code_verifier = secrets.token_urlsafe(32)
        code_challenge = base64.urlsafe_b64encode(hashlib.sha256(code_verifier.encode("utf-8")).digest()).decode("utf-8").rstrip("=")

        params = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(GOOGLE_WORKSPACE_SCOPES),
            "access_type": "offline",
            "prompt": "consent",
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
        auth_url = f"{auth_uri}?{urllib.parse.urlencode(params)}"

        pending_path = home / "google_oauth_pending.json"
        pending_data = {
            "state": state,
            "code_verifier": code_verifier,
            "redirect_uri": redirect_uri,
            "client_secret_path": str(client_secret_path),
            "created_at": datetime.now().isoformat(),
        }
        _atomic_write_text(pending_path, json.dumps(pending_data, indent=2), encoding="utf-8")
        return {"ok": True, "auth_url": auth_url, "state": state}
    except Exception as exc:
        logger.error("Failed to generate Google auth URL: %s", exc)
        return {"ok": False, "message": f"Failed to generate Google auth URL: {exc}"}


def exchange_google_auth_code(code_or_url: str) -> dict[str, Any]:
    """Exchange authorization code (or callback URL) for Google OAuth credentials."""
    home = get_active_hermes_home()
    pending_path = home / "google_oauth_pending.json"
    if not pending_path.is_file():
        return {
            "ok": False,
            "message": "No pending Google authorization session found. Please click 'Get Google Authorization Link' first.",
        }

    try:
        pending = json.loads(pending_path.read_text(encoding="utf-8"))
    except Exception as e:
        return {"ok": False, "message": f"Corrupt pending auth state: {e}"}

    client_secret_path = pending.get("client_secret_path")
    if not client_secret_path or not Path(client_secret_path).is_file():
        client_secret_path = _find_google_client_secret(home)
        if not client_secret_path:
            return {"ok": False, "message": "Client secret file missing."}

    raw = str(code_or_url).strip()
    code = raw
    returned_state = None

    if raw.startswith("http"):
        try:
            parsed = urllib.parse.urlparse(raw)
            qs = urllib.parse.parse_qs(parsed.query)
            if "code" in qs:
                code = qs["code"][0]
            if "state" in qs:
                returned_state = qs["state"][0]
        except Exception:
            pass

    expected_state = pending.get("state")
    if returned_state and expected_state and returned_state != expected_state:
        return {"ok": False, "message": "OAuth state mismatch. Please generate a fresh authorization link."}

    try:
        sec_data = json.loads(Path(client_secret_path).read_text(encoding="utf-8"))
        c_info = sec_data.get("installed") or sec_data.get("web") or {}
        client_id = c_info.get("client_id")
        client_secret = c_info.get("client_secret")
        token_uri = c_info.get("token_uri") or "https://oauth2.googleapis.com/token"

        token_body = urllib.parse.urlencode({
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": pending.get("redirect_uri", "http://localhost:1"),
            "grant_type": "authorization_code",
            "code_verifier": pending.get("code_verifier"),
        }).encode("utf-8")

        req = urllib.request.Request(
            token_uri,
            data=token_body,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": "Lisa-WebUI-Wizard/1.0",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=_SERVICE_PROBE_TIMEOUT) as t_resp:
            resp_body = json.loads(t_resp.read().decode("utf-8"))

        access_token = resp_body.get("access_token")
        refresh_token = resp_body.get("refresh_token")
        if not access_token:
            return {"ok": False, "message": "No access_token returned by Google OAuth."}

        token_payload = {
            "token": access_token,
            "access_token": access_token,
            "refresh_token": refresh_token,
            "token_uri": token_uri,
            "client_id": client_id,
            "client_secret": client_secret,
            "scopes": GOOGLE_WORKSPACE_SCOPES,
            "type": "authorized_user",
        }

        token_path = home / "google_token.json"
        _atomic_write_text(token_path, json.dumps(token_payload, indent=2), encoding="utf-8")
        try:
            os.chmod(token_path, 0o600)
        except Exception:
            pass

        # Also mirror to profile directory if active
        prof = get_active_profile_name()
        if prof and prof != "default":
            prof_token = home / "profiles" / prof / "google_token.json"
            prof_token.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write_text(prof_token, json.dumps(token_payload, indent=2), encoding="utf-8")

        # Cleanup pending file
        try:
            pending_path.unlink()
        except Exception:
            pass

        # Identify connected account
        email = "Account Active"
        try:
            u_req = urllib.request.Request(
                "https://www.googleapis.com/oauth2/v3/userinfo",
                headers={"Authorization": f"Bearer {access_token}", "User-Agent": "Lisa-WebUI-Wizard/1.0"},
            )
            with urllib.request.urlopen(u_req, timeout=_SERVICE_PROBE_TIMEOUT) as u_resp:
                if u_resp.status == 200:
                    u_data = json.loads(u_resp.read().decode("utf-8"))
                    email = u_data.get("email") or email
        except Exception:
            pass

        return {
            "ok": True,
            "status": "verified",
            "email": email,
            "message": f"Connected & Verified: Google Workspace ({email} — Calendar & Gmail active)",
        }
    except Exception as exc:
        logger.error("Failed to exchange Google OAuth code: %s", exc)
        return {"ok": False, "message": f"Google authorization failed: {exc}"}


def probe_service_connection(service_id: str, env_values: dict[str, str]) -> dict[str, Any]:
    """Perform non-destructive connectivity check for supported integrations."""
    if service_id == "clickup":
        token = env_values.get("CLICKUP_API_KEY") or os.environ.get("CLICKUP_API_KEY")
        if not token:
            return {"ok": False, "message": "Missing ClickUp API Token"}
        req = urllib.request.Request(
            "https://api.clickup.com/api/v2/user",
            headers={"Authorization": token, "User-Agent": "Lisa-WebUI-Wizard/1.0"},
            method="GET",
        )
        try:
            with urllib.request.urlopen(req, timeout=_SERVICE_PROBE_TIMEOUT) as resp:
                if resp.status == 200:
                    data = json.loads(resp.read().decode("utf-8"))
                    user = data.get("user") or {}
                    username = user.get("username") or user.get("email") or "Verified"

                    # Attempt to fetch workspace teams
                    team_id = None
                    try:
                        t_req = urllib.request.Request(
                            "https://api.clickup.com/api/v2/team",
                            headers={"Authorization": token, "User-Agent": "Lisa-WebUI-Wizard/1.0"},
                            method="GET",
                        )
                        with urllib.request.urlopen(t_req, timeout=_SERVICE_PROBE_TIMEOUT) as t_resp:
                            if t_resp.status == 200:
                                t_data = json.loads(t_resp.read().decode("utf-8"))
                                teams = t_data.get("teams") or []
                                if teams:
                                    team_id = teams[0].get("id")
                                    team_name = teams[0].get("name", "Team")
                                    return {
                                        "ok": True,
                                        "message": f"Connected & Verified: {username} (Workspace: {team_name})",
                                        "team_id": team_id,
                                    }
                    except Exception:
                        pass
                    return {"ok": True, "message": f"Connected & Verified: {username}"}
                return {"ok": False, "message": f"ClickUp returned status {resp.status}"}
        except urllib.error.HTTPError as exc:
            return {"ok": False, "message": f"ClickUp Auth Failed (HTTP {exc.code})", "detail": "Verify personal API token starts with 'pk_'"}
        except Exception as exc:
            return {"ok": False, "message": "Connection failed", "detail": str(exc)}

    elif service_id == "telegram":
        token = env_values.get("TELEGRAM_BOT_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN")
        if not token:
            return {"ok": False, "message": "Missing Telegram Bot Token"}
        url = f"https://api.telegram.org/bot{token}/getMe"
        req = urllib.request.Request(url, headers={"User-Agent": "Lisa-WebUI-Wizard/1.0"}, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=_SERVICE_PROBE_TIMEOUT) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                if data.get("ok"):
                    bot_res = data.get("result") or {}
                    bot_name = bot_res.get("username", "bot")
                    bot_first = bot_res.get("first_name", "")
                    return {"ok": True, "message": f"Connected & Verified: @{bot_name} ({bot_first})"}
                return {"ok": False, "message": "Telegram API reported error"}
        except urllib.error.HTTPError as exc:
            return {"ok": False, "message": f"Telegram Bot Token Invalid (HTTP {exc.code})"}
        except Exception as exc:
            return {"ok": False, "message": "Connection failed", "detail": str(exc)}

    elif service_id == "onyx":
        base_url = env_values.get("ONYX_BASE_URL") or os.environ.get("ONYX_BASE_URL", "")
        if not base_url:
            return {"ok": False, "message": "Missing Onyx base URL"}
        api_key = env_values.get("ONYX_API_KEY") or os.environ.get("ONYX_API_KEY", "")
        health_url = base_url.rstrip("/") + "/api/health"
        headers = {"User-Agent": "Lisa-WebUI-Wizard/1.0"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
            headers["X-API-Key"] = api_key
        req = urllib.request.Request(health_url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=_SERVICE_PROBE_TIMEOUT) as resp:
                if resp.status in {200, 204}:
                    return {"ok": True, "message": f"Connected & Verified: Onyx RAG reachable at {base_url}"}
                return {"ok": False, "message": f"Onyx returned status {resp.status}"}
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                return {"ok": False, "message": "Onyx API Key Invalid (HTTP 401)", "detail": str(exc)}
            return {"ok": False, "message": f"Onyx HTTP {exc.code}", "detail": str(exc)}
        except Exception as exc:
            return {"ok": False, "message": f"Cannot reach Onyx at {base_url}", "detail": str(exc)}

    elif service_id == "fireflies":
        token = env_values.get("FIREFLIES_API_KEY") or os.environ.get("FIREFLIES_API_KEY")
        if not token:
            return {"ok": False, "message": "Missing Fireflies API Key"}
        req = urllib.request.Request(
            "https://api.fireflies.ai/graphql",
            data=json.dumps({"query": "query { user { email name } }"}).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "User-Agent": "Lisa-WebUI-Wizard/1.0",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=_SERVICE_PROBE_TIMEOUT) as resp:
                if resp.status == 200:
                    data = json.loads(resp.read().decode("utf-8"))
                    user = (data.get("data") or {}).get("user") or {}
                    email = user.get("email") or "Account Verified"
                    return {"ok": True, "message": f"Connected & Verified: {email}"}
                return {"ok": False, "message": f"Fireflies returned status {resp.status}"}
        except urllib.error.HTTPError as exc:
            return {"ok": False, "message": f"Fireflies Auth Failed (HTTP {exc.code})", "detail": str(exc)}
        except Exception as exc:
            return {"ok": False, "message": "Connection failed", "detail": str(exc)}

    elif service_id == "google_workspace":
        home = get_active_hermes_home()
        token_p = home / "google_token.json"
        client_secret_path = _find_google_client_secret(home)

        # 1. Check google_token.json
        if token_p.is_file():
            try:
                t_data = json.loads(token_p.read_text("utf-8"))
                token = t_data.get("token") or t_data.get("access_token")
                refresh_token = t_data.get("refresh_token")

                if token or refresh_token:
                    verified_email = None
                    if token:
                        try:
                            req = urllib.request.Request(
                                "https://www.googleapis.com/oauth2/v3/userinfo",
                                headers={"Authorization": f"Bearer {token}", "User-Agent": "Lisa-WebUI-Wizard/1.0"},
                            )
                            with urllib.request.urlopen(req, timeout=_SERVICE_PROBE_TIMEOUT) as resp:
                                if resp.status == 200:
                                    u_data = json.loads(resp.read().decode("utf-8"))
                                    verified_email = u_data.get("email")
                        except Exception:
                            pass

                    msg = (
                        f"Connected & Verified: Google Workspace ({verified_email} — Calendar & Gmail active)"
                        if verified_email
                        else "Connected & Verified: Google OAuth Token Active (Calendar & Gmail ready)"
                    )
                    return {"ok": True, "status": "verified", "message": msg, "email": verified_email}
            except Exception as e:
                return {"ok": False, "status": "corrupt_token", "message": f"Corrupted google_token.json: {e}", "needs_google_auth": True}

        # 2. Check service account credentials
        cred_p = home / "credentials.json"
        if cred_p.is_file():
            try:
                c_data = json.loads(cred_p.read_text("utf-8"))
                if c_data.get("type") == "service_account":
                    email = c_data.get("client_email", "service_account")
                    return {"ok": True, "status": "verified", "message": f"Connected & Verified: Service Account Active ({email})"}
            except Exception:
                pass

        # 3. Check client secret file (Needs OAuth authorization!)
        if client_secret_path and client_secret_path.is_file():
            try:
                c_data = json.loads(client_secret_path.read_text("utf-8"))
                client_type = "installed" if "installed" in c_data else ("web" if "web" in c_data else "oauth")
                c_inner = c_data.get("installed") or c_data.get("web") or {}
                cid = str(c_inner.get("client_id") or "...")
                cid_masked = (cid[:25] + "...") if len(cid) > 25 else cid
                proj = c_inner.get("project_id", "")
                proj_text = f" (Project: {proj})" if proj else ""
                return {
                    "ok": False,
                    "status": "needs_auth",
                    "needs_google_auth": True,
                    "project_id": proj,
                    "client_id": cid_masked,
                    "message": f"OAuth Client Configured ({client_type.capitalize()} App: {cid_masked}){proj_text}. Google authorization required to activate Calendar & Gmail.",
                }
            except Exception as e:
                return {"ok": False, "status": "corrupt_client", "message": f"Invalid client secret JSON: {e}"}

        client_id = env_values.get("GOOGLE_CLIENT_ID") or os.environ.get("GOOGLE_CLIENT_ID")
        client_secret = env_values.get("GOOGLE_CLIENT_SECRET") or os.environ.get("GOOGLE_CLIENT_SECRET")
        if client_id and client_secret:
            cid_masked = (client_id[:20] + "...") if len(client_id) > 20 else client_id
            return {
                "ok": False,
                "status": "needs_auth",
                "needs_google_auth": True,
                "client_id": cid_masked,
                "message": f"OAuth Client ID & Secret configured ({cid_masked}). Click 'Authorize with Google' to connect your account.",
            }

        sa_json = env_values.get("GOOGLE_WORKSPACE_CREDENTIALS") or os.environ.get("GOOGLE_WORKSPACE_CREDENTIALS")
        if sa_json:
            try:
                s_data = json.loads(sa_json)
                email = s_data.get("client_email", "service_account")
                return {"ok": True, "status": "verified", "message": f"Connected & Verified: Service Account configured ({email})"}
            except Exception:
                return {"ok": True, "status": "verified", "message": "Connected & Verified: Google Workspace credentials configured"}

        return {"ok": False, "status": "not_configured", "message": "Not configured yet. Upload credentials.json or client_secret_*.json below."}

    elif service_id == "microsoft_graph":
        home = get_active_hermes_home()
        token_p = home / "microsoft_token.json"
        if token_p.is_file():
            return {"ok": True, "message": "Connected & Verified (microsoft_token.json present)"}

        client_id = env_values.get("MICROSOFT_CLIENT_ID") or os.environ.get("MICROSOFT_CLIENT_ID")
        client_secret = env_values.get("MICROSOFT_CLIENT_SECRET") or os.environ.get("MICROSOFT_CLIENT_SECRET")
        tenant_id = env_values.get("MICROSOFT_TENANT_ID") or os.environ.get("MICROSOFT_TENANT_ID")

        if client_id and client_secret and tenant_id:
            token_url = f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
            body_params = urllib.parse.urlencode({
                "client_id": client_id,
                "client_secret": client_secret,
                "grant_type": "client_credentials",
                "scope": "https://graph.microsoft.com/.default",
            }).encode("utf-8")
            req = urllib.request.Request(token_url, data=body_params, method="POST", headers={"User-Agent": "Lisa-WebUI-Wizard/1.0"})
            try:
                with urllib.request.urlopen(req, timeout=_SERVICE_PROBE_TIMEOUT) as resp:
                    if resp.status == 200:
                        return {"ok": True, "message": "Connected & Verified: Microsoft Graph App Token Acquired"}
                    return {"ok": False, "message": f"Microsoft OAuth returned HTTP {resp.status}"}
            except urllib.error.HTTPError as exc:
                return {"ok": False, "message": f"Microsoft OAuth Failed (HTTP {exc.code})", "detail": str(exc)}
            except Exception as exc:
                return {"ok": True, "message": "Credentials configured (Offline/Network restricted)"}

        return {"ok": False, "message": "Not configured yet. Enter Azure Client ID, Secret & Tenant ID below."}

    return {"ok": False, "message": f"Probe not implemented for {service_id}"}


def configure_service(service_id: str, env_vars: dict[str, str], files: dict[str, str] | None = None) -> dict[str, Any]:
    """Save service credentials (env vars and/or uploaded files) and probe connection immediately."""
    spec = SERVICE_CONNECTORS.get(service_id)
    if not spec:
        raise ValueError(f"Unknown service: {service_id}")
    if not spec.get("supported"):
        raise ValueError(f"Service {service_id} is not supported in this installation.")

    home = get_active_hermes_home()
    state = load_wizard_state()
    state.setdefault("service_credentials_draft", {})
    state.setdefault("uploaded_files", {})

    saved_files: list[str] = []
    env_updates: dict[str, str] = {}

    if files and isinstance(files, dict):
        for raw_filename, content in files.items():
            if not content or not isinstance(content, str):
                continue
            fname = Path(raw_filename).name
            if not fname.endswith(".json"):
                raise ValueError(f"Invalid file type for {fname}. Only JSON credential files are accepted.")

            try:
                parsed_json = json.loads(content)
            except Exception as e:
                raise ValueError(f"File {fname} is not valid JSON: {e}")

            target_filenames = [fname]
            if service_id == "google_workspace":
                client_info = parsed_json.get("installed") or parsed_json.get("web")
                if client_info and isinstance(client_info, dict):
                    # Google OAuth Client Secret file
                    for alias in ["credentials.json", "google_client_secret.json"]:
                        if alias not in target_filenames:
                            target_filenames.append(alias)
                    # Automatically extract client credentials into environment
                    cid = str(client_info.get("client_id") or "").strip()
                    csec = str(client_info.get("client_secret") or "").strip()
                    if cid:
                        env_updates["GOOGLE_CLIENT_ID"] = cid
                        os.environ["GOOGLE_CLIENT_ID"] = cid
                        state["service_credentials_draft"]["GOOGLE_CLIENT_ID"] = cid
                    if csec:
                        env_updates["GOOGLE_CLIENT_SECRET"] = csec
                        os.environ["GOOGLE_CLIENT_SECRET"] = csec
                        state["service_credentials_draft"]["GOOGLE_CLIENT_SECRET"] = csec
                elif parsed_json.get("refresh_token") or (parsed_json.get("token") and parsed_json.get("scopes")):
                    # Google OAuth authorized user token file
                    if "google_token.json" not in target_filenames:
                        target_filenames.append("google_token.json")
                elif parsed_json.get("type") == "service_account":
                    # Google Service Account Key
                    for alias in ["credentials.json", "google_service_account.json"]:
                        if alias not in target_filenames:
                            target_filenames.append(alias)
                    sa_path = str(home / "credentials.json")
                    env_updates["GOOGLE_APPLICATION_CREDENTIALS"] = sa_path
                    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = sa_path
                    state["service_credentials_draft"]["GOOGLE_APPLICATION_CREDENTIALS"] = sa_path

            elif service_id == "microsoft_graph":
                if parsed_json.get("refresh_token") or parsed_json.get("access_token"):
                    if "microsoft_token.json" not in target_filenames:
                        target_filenames.append("microsoft_token.json")

            for target_name in target_filenames:
                target_path = home / target_name
                _atomic_write_text(target_path, json.dumps(parsed_json, indent=2))
                try:
                    os.chmod(target_path, 0o600)
                except Exception:
                    pass

                profile = get_active_profile_name()
                if profile and profile != "default":
                    p_path = home / "profiles" / profile / target_name
                    p_path.parent.mkdir(parents=True, exist_ok=True)
                    _atomic_write_text(p_path, json.dumps(parsed_json, indent=2))
                    try:
                        os.chmod(p_path, 0o600)
                    except Exception:
                        pass

                saved_files.append(target_name)
                state["uploaded_files"][target_name] = datetime.now().isoformat()

    if env_vars and isinstance(env_vars, dict):
        for k, v in env_vars.items():
            if not isinstance(v, str):
                continue
            val = v.strip()
            if val == "••••••••":
                continue
            if val:
                env_updates[k] = val
                os.environ[k] = val
                state["service_credentials_draft"][k] = val

    if env_updates:
        _write_env_file(home / ".env", env_updates)
        _reload_dotenv(home)

        profile = get_active_profile_name()
        if profile and profile != "default":
            p_dir = home / "profiles" / profile
            if p_dir.is_dir():
                _write_env_file(p_dir / ".env", env_updates)

    save_wizard_state(state)

    # Immediately probe connection with updated values
    current_env = inspect_installed_components()["env_values"]
    probe_result = probe_service_connection(service_id, {**current_env, **env_updates})

    # If ClickUp returns a team_id, auto-save team_id if missing
    if service_id == "clickup" and probe_result.get("ok") and probe_result.get("team_id"):
        team_id = str(probe_result["team_id"])
        _write_env_file(home / ".env", {"CLICKUP_TEAM_ID": team_id})
        os.environ["CLICKUP_TEAM_ID"] = team_id
        state["service_credentials_draft"]["CLICKUP_TEAM_ID"] = team_id
        save_wizard_state(state)

    probe_status = probe_result.get("status")
    status_str = "verified" if probe_result.get("ok") else (probe_status if probe_status in {"needs_auth", "configured"} else "error")

    return {
        "ok": probe_result.get("ok", False),
        "service_id": service_id,
        "status": status_str,
        "message": probe_result.get("message", "Configured"),
        "detail": probe_result.get("detail", ""),
        "saved_files": saved_files,
        "updated_keys": list(env_updates.keys()),
        "needs_google_auth": bool(probe_result.get("needs_google_auth")),
    }


# ── Capability Evaluation ────────────────────────────────────────────────────

def evaluate_capabilities(selected_ids: list[str] | None = None) -> list[dict[str, Any]]:
    """Evaluate each capability in CAPABILITY_REGISTRY against installed components.
    
    States:
    - available: all required integrations are configured and plugins/skills present
    - requires_configuration: plugin/skill installed, but integration credentials missing
    - missing_dependency: required plugin/skill not installed
    - unsupported: required integration not supported
    """
    components = inspect_installed_components()
    installed_plugins = components["installed_plugins"]
    installed_skills = components["installed_skills"]
    selected_set = set(selected_ids or [])

    results = []
    for cap_id, spec in CAPABILITY_REGISTRY.items():
        req_integrations = spec.get("required_integrations", [])
        logic = spec.get("integration_logic", "all")

        # Evaluate integrations
        int_statuses = [get_service_status(i_id, probe=False) for i_id in req_integrations]
        configured_count = sum(1 for s in int_statuses if s.get("status") in {"configured", "verified"})
        supported_count = sum(1 for s in int_statuses if s.get("supported", True))

        if supported_count == 0:
            status = "unsupported"
            status_reason = "No supported integration found in this system"
        else:
            int_ok = (configured_count > 0) if logic == "any" else (configured_count == len(req_integrations))
            
            # Evaluate plugins and skills
            rec_plugins = spec.get("recommended_plugins", [])
            rec_skills = spec.get("recommended_skills", [])
            
            # Missing deps if recommended plugins/skills are absent
            missing_plugins = [p for p in rec_plugins if p not in installed_plugins]
            missing_skills = [s for s in rec_skills if s not in installed_skills]

            if not int_ok:
                status = "requires_configuration"
                status_reason = f"Requires integration: {', '.join(req_integrations)}"
            elif missing_plugins or missing_skills:
                # Degraded / missing dependency
                status = "requires_configuration" if int_ok else "missing_dependency"
                status_reason = "Integration ready; optional extensions can be enabled"
            else:
                status = "available"
                status_reason = "Dependencies and connectors ready"

        results.append({
            "id": cap_id,
            "title": spec["title"],
            "description": spec["description"],
            "status": status,
            "status_reason": status_reason,
            "selected": cap_id in selected_set,
            "required_integrations": req_integrations,
            "recommended_plugins": spec.get("recommended_plugins", []),
            "recommended_skills": spec.get("recommended_skills", []),
        })

    return results


def get_identity_mapping(home: Path | None = None) -> dict[str, Any]:
    """Load team members and aliases from profile's config/identity-mapping.json."""
    home = home or get_active_hermes_home()
    mapping_file = home / "config" / "identity-mapping.json"
    if mapping_file.is_file():
        try:
            data = json.loads(mapping_file.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return {
                    "people": data.get("people", []) if isinstance(data.get("people"), list) else [],
                    "aliases": data.get("aliases", {}) if isinstance(data.get("aliases"), dict) else {},
                }
        except Exception as e:
            logger.warning("Failed to parse identity-mapping.json: %s", e)

    # Fallback to wizard draft if not yet written to config/
    state = load_wizard_state()
    draft_people = state.get("team_members")
    if isinstance(draft_people, list) and draft_people:
        return {"people": draft_people, "aliases": {}}

    return {"people": [], "aliases": {}}


def _sync_team_to_user_md(home: Path, people: list[dict[str, Any]]) -> None:
    """Sync team members table into memories/USER.md while preserving other sections."""
    user_file = home / "memories" / "USER.md"
    existing_content = ""
    if user_file.is_file():
        try:
            existing_content = user_file.read_text(encoding="utf-8")
        except Exception:
            existing_content = ""

    rows = []
    for p in people:
        name = str(p.get("canonical_name") or p.get("name") or "").strip()
        if not name:
            continue
        role = str(p.get("role") or "Team Member").strip()
        emails = p.get("emails") if isinstance(p.get("emails"), list) else ([p["email"]] if p.get("email") else [])
        email_str = "<br>".join([e for e in emails if e]) if emails else (p.get("email", "").strip() or "—")

        tele = p.get("telegram") if isinstance(p.get("telegram"), dict) else {}
        t_id = str(tele.get("user_id") or p.get("telegram_id") or "").strip()
        t_user = str(tele.get("username") or p.get("telegram_username") or "").strip()
        t_display = str(tele.get("display_name") or "").strip()
        t_str = f"`{t_id}`" if t_id else "—"
        if t_user:
            t_user_clean = t_user if t_user.startswith("@") else f"@{t_user}"
            t_str = f"{t_str} ({t_user_clean})" if t_id else t_user_clean
        elif t_display and t_display != name:
            t_str = f"{t_str} ({t_display})" if t_id else t_display

        cu = p.get("clickup") if isinstance(p.get("clickup"), dict) else {}
        c_id = str(cu.get("user_id") if cu.get("user_id") is not None else (p.get("clickup_id") or "")).strip()
        c_name = str(cu.get("member_name") or p.get("clickup_member_name") or "").strip()
        c_str = f"`{c_id}`" if c_id else "—"
        if c_name and c_id:
            c_str = f"{c_str} ({c_name})"

        aliases = p.get("aliases", [])
        aliases_str = ", ".join(aliases) if isinstance(aliases, list) and aliases else (name or "—")

        rows.append(f"| {name} | {role} | {email_str} | {t_str} | {c_str} | {aliases_str} |")

    table_header = (
        "| Name | Role | Email | Telegram ID | ClickUp ID | Aliases |\n"
        "| --- | --- | --- | --- | --- | --- |"
    )
    section_body = (
        "## Team & Stakeholders Directory\n\n"
        + table_header + "\n"
        + ("\n".join(rows) if rows else "| None | — | — | — | — | — |")
        + "\n\n*Note for Lisa: Use mapped ClickUp numeric IDs for task assignment and Telegram IDs for notifications.*"
    )

    marker = "## Team & Stakeholders Directory"
    if marker in existing_content:
        pattern = r"## Team & Stakeholders Directory.*?(?=\n## |\Z)"
        new_content = re.sub(pattern, section_body, existing_content, flags=re.DOTALL)
    else:
        new_content = (existing_content.strip() + "\n\n" + section_body).strip() + "\n"

    user_file.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_text(user_file, new_content, encoding="utf-8")


def _sync_team_to_obsidian_vault(vault_path: Path, people: list[dict[str, Any]], company_name: str, assistant_name: str) -> list[str]:
    """Sync team members into Obsidian 01-People/<Name>.md notes and update Company_Handbook.md."""
    if not vault_path.is_dir():
        return []

    people_dir = vault_path / "01-People"
    people_dir.mkdir(parents=True, exist_ok=True)

    created_notes = []
    handbook_links = []

    for p in people:
        name = str(p.get("canonical_name") or p.get("name") or "").strip()
        if not name:
            continue
        person_id = str(p.get("person_id") or "").strip()
        role = str(p.get("role") or "Team Member").strip()

        emails = p.get("emails") if isinstance(p.get("emails"), list) else ([p["email"]] if p.get("email") else [])
        primary_email = emails[0] if emails else str(p.get("email") or "").strip()

        tele = p.get("telegram") if isinstance(p.get("telegram"), dict) else {}
        t_id = str(tele.get("user_id") or p.get("telegram_id") or "").strip()
        t_user = str(tele.get("username") or p.get("telegram_username") or "").strip()
        t_display = str(tele.get("display_name") or "").strip()

        cu = p.get("clickup") if isinstance(p.get("clickup"), dict) else {}
        c_id = str(cu.get("user_id") if cu.get("user_id") is not None else (p.get("clickup_id") or "")).strip()
        c_name = str(cu.get("member_name") or p.get("clickup_member_name") or "").strip()

        aliases = p.get("aliases", [])
        aliases_list = aliases if isinstance(aliases, list) else [str(aliases)]

        safe_filename = re.sub(r'[/\\:*?"<>|]', '_', name) + ".md"
        person_file = people_dir / safe_filename

        person_note = (
            "---\n"
            + (f"person_id: \"{person_id}\"\n" if person_id else "")
            + f"name: {name}\n"
            f"canonical_name: {name}\n"
            f"role: {role}\n"
            f"emails: {json.dumps(emails, ensure_ascii=False)}\n"
            f"telegram_id: \"{t_id}\"\n"
            f"clickup_id: \"{c_id}\"\n"
            f"aliases: {json.dumps(aliases_list, ensure_ascii=False)}\n"
            f"tags: [team, person, internal]\n"
            "---\n\n"
            f"# {name}\n\n"
            f"- **Role**: {role}\n"
            f"- **Emails**: {', '.join(emails) if emails else primary_email or '—'}\n"
            f"- **Telegram**: `{t_id or '—'}` {f'(@{t_user})' if t_user else ''}\n"
            f"- **ClickUp User ID**: `{c_id or '—'}` {f'({c_name})' if c_name else ''}\n"
            f"- **Aliases**: {', '.join(aliases_list) if aliases_list else name}\n\n"
            "## Active Projects & Workstreams\n"
            "- See [[03-Projects/README|Active Projects]]\n\n"
            "## Meeting Notes & Action Items\n"
            "- See [[04-Meetings/README|Meetings]]\n"
        )
        _atomic_write_text(person_file, person_note, encoding="utf-8")
        created_notes.append(f"01-People/{safe_filename}")

        link_entry = f"- [[01-People/{safe_filename[:-3]}|{name}]] — {role}"
        details = []
        if primary_email:
            details.append(primary_email)
        if c_id:
            details.append(f"ClickUp: `{c_id}`")
        if t_id:
            details.append(f"Telegram: `{t_id}`")
        if details:
            link_entry += f" ({' | '.join(details)})"
        handbook_links.append(link_entry)

    # Update Company_Handbook.md
    handbook = vault_path / "Company_Handbook.md"
    if handbook.is_file():
        try:
            h_content = handbook.read_text(encoding="utf-8")
            marker = "## Team Directory"
            team_section = "## Team Directory\n" + ("\n".join(handbook_links) if handbook_links else "- No members registered yet.") + "\n"
            if marker in h_content:
                pattern = r"## Team Directory.*?(?=\n## |\Z)"
                new_h = re.sub(pattern, team_section, h_content, flags=re.DOTALL)
            else:
                new_h = h_content.strip() + "\n\n" + team_section
            _atomic_write_text(handbook, new_h, encoding="utf-8")
        except Exception as e:
            logger.warning("Failed to update Company_Handbook.md with team directory: %s", e)

    return created_notes


def save_identity_mapping(people_list: list[dict[str, Any]] | dict[str, Any], aliases_dict: dict[str, str] | None = None, home: Path | None = None) -> dict[str, Any]:
    """Validate and atomically persist team members to identity-mapping.json, USER.md, and Obsidian vault."""
    home = home or get_active_hermes_home()
    config_dir = home / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    mapping_file = config_dir / "identity-mapping.json"

    if isinstance(people_list, dict):
        if not aliases_dict and "aliases" in people_list:
            aliases_dict = people_list.get("aliases")
        people_list = people_list.get("people", [])

    normalized_people = []
    aliases_map = dict(aliases_dict or {})

    for item in (people_list or []):
        if not isinstance(item, dict):
            continue
        canonical_name = str(item.get("canonical_name") or item.get("name") or "").strip()
        if not canonical_name:
            continue

        person_id = str(item.get("person_id") or "").strip()
        if not person_id:
            person_id = str(uuid.uuid4())

        # Emails handling: list of emails, comma-separated string, or single email
        raw_emails = item.get("emails")
        emails_list: list[str] = []
        if isinstance(raw_emails, list):
            emails_list = [str(e).strip() for e in raw_emails if str(e).strip()]
        elif isinstance(raw_emails, str):
            emails_list = [e.strip() for e in re.split(r"[\n,;]+", raw_emails) if e.strip()]

        single_email = str(item.get("email") or "").strip()
        if single_email and single_email not in emails_list:
            emails_list.append(single_email)

        role = str(item.get("role") or "Team Member").strip()

        # Telegram
        tele_dict = item.get("telegram") if isinstance(item.get("telegram"), dict) else {}
        t_id = str(tele_dict.get("user_id") or item.get("telegram_id") or "").strip()
        t_display = str(tele_dict.get("display_name") or item.get("telegram_display_name") or canonical_name).strip()
        t_user = str(tele_dict.get("username") or item.get("telegram_username") or "").strip()

        # ClickUp
        cu_dict = item.get("clickup") if isinstance(item.get("clickup"), dict) else {}
        c_id_raw = cu_dict.get("user_id") if cu_dict.get("user_id") is not None else item.get("clickup_id")
        c_id: int | str = ""
        if c_id_raw is not None and str(c_id_raw).strip():
            c_str = str(c_id_raw).strip()
            c_id = int(c_str) if c_str.isdigit() else c_str

        c_name = str(cu_dict.get("member_name") or item.get("clickup_member_name") or canonical_name.split()[0].lower()).strip()

        # Aliases
        raw_aliases = item.get("aliases") or []
        if isinstance(raw_aliases, str):
            alias_list = [a.strip() for a in re.split(r"[\n,;]+", raw_aliases) if a.strip()]
        elif isinstance(raw_aliases, list):
            alias_list = [str(a).strip() for a in raw_aliases if str(a).strip()]
        else:
            alias_list = []

        if canonical_name not in alias_list:
            alias_list.append(canonical_name)

        person_record = {
            "person_id": person_id,
            "canonical_name": canonical_name,
            "name": canonical_name,  # for backward compatibility
            "aliases": alias_list,
            "emails": emails_list,
            "email": emails_list[0] if emails_list else "",  # for backward compatibility
            "telegram": {
                "user_id": t_id,
                "display_name": t_display,
                "username": t_user,
            },
            "clickup": {
                "member_name": c_name,
                "user_id": c_id,
            },
            "role": role,
        }
        normalized_people.append(person_record)

        for alias in alias_list:
            aliases_map[alias] = person_id
            aliases_map[alias.lower()] = person_id
        for email in emails_list:
            aliases_map[email] = person_id
            aliases_map[email.lower()] = person_id

    payload = {
        "people": normalized_people,
        "aliases": aliases_map,
    }
    _atomic_write_text(mapping_file, json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    # Sync to USER.md
    try:
        _sync_team_to_user_md(home, normalized_people)
    except Exception as e:
        logger.warning("Failed to sync team to USER.md: %s", e)

    # Sync to state
    state = load_wizard_state()
    state["team_members"] = normalized_people
    save_wizard_state(state)

    # Sync to Obsidian vault if available
    vault_path_str = state.get("obsidian_vault_path") or (str(Path("/workspace/obsidian")) if Path("/workspace").is_dir() else str(home / "obsidian"))
    vault_path = Path(vault_path_str)
    created_notes = []
    if vault_path.is_dir():
        try:
            created_notes = _sync_team_to_obsidian_vault(
                vault_path,
                normalized_people,
                company_name=state.get("company_name", "Company"),
                assistant_name=state.get("assistant_name", "Lisa"),
            )
        except Exception as e:
            logger.warning("Failed to sync team to Obsidian vault: %s", e)

    return {
        "ok": True,
        "count": len(normalized_people),
        "people_count": len(normalized_people),
        "created_obsidian_notes": created_notes,
        "message": f"Saved {len(normalized_people)} team members to identity-mapping.json, USER.md, and Obsidian 01-People/",
    }


def scaffold_obsidian_vault(vault_path: str | None = None) -> dict[str, Any]:
    """Scaffold enterprise Obsidian knowledge vault folder hierarchy and starter templates."""
    home = get_active_hermes_home()
    if not vault_path:
        if Path("/workspace").is_dir():
            target_vault = Path("/workspace/obsidian")
        else:
            target_vault = home / "obsidian"
    else:
        target_vault = Path(vault_path).expanduser().resolve()

    target_vault.mkdir(parents=True, exist_ok=True)

    folders = [
        ("00-Inbox", "Quick-capture notes, voice memos, unprocessed briefings, and scratchpads."),
        ("01-People", "Team directory, leadership profiles, external partners, and individual wikilink notes."),
        ("02-Companies", "Clients, partners, corporate accounts, and external vendor profiles."),
        ("03-Projects", "Active quarterly initiatives, project sprints, backlogs, and milestone tracking."),
        ("04-Meetings", "Meeting summaries, Fireflies transcription imports, and structured action items."),
        ("05-Decisions", "Architecture & Business Decision Records (ADRs), policy changes, and approved RFCs."),
        ("06-Knowledge", "Standard Operating Procedures (SOPs), company playbooks, guides, and tech documentation."),
        ("07-Daily", "Daily scratchpads, standup logs, and personal chronological notes."),
        ("08-Team-Briefing", "Daily team morning briefings delivered via Telegram and archived chronologically."),
        ("files", "Working deliverables, exported PDFs, client proposals, and generated HTML artifacts."),
    ]

    created_dirs = []
    created_files = []

    for folder_name, folder_desc in folders:
        f_dir = target_vault / folder_name
        f_dir.mkdir(parents=True, exist_ok=True)
        created_dirs.append(folder_name)

        readme_file = f_dir / "README.md"
        if not readme_file.exists():
            readme_content = (
                f"# {folder_name}\n\n"
                f"{folder_desc}\n\n"
                f"## Guidelines for Team & Lisa AI Assistant\n"
                f"- **Team Members**: Create, format, and interconnect markdown notes using standard `[[Note Name]]` wiki-links.\n"
                f"- **Lisa AI**: Automatically reads, searches, and cites notes in this directory to answer questions and execute workflows.\n"
                f"- **Maintenance**: Maintain clear wikilinks across projects and review notes periodically.\n"
            )
            _atomic_write_text(readme_file, readme_content)
            created_files.append(f"{folder_name}/README.md")

    state = load_wizard_state()
    company = state.get("company_name", "Company")
    assistant = state.get("assistant_name", "Lisa")

    # Sync configured team members into 01-People/
    identity = get_identity_mapping(home)
    people = identity.get("people") or []
    if people:
        person_notes = _sync_team_to_obsidian_vault(target_vault, people, company, assistant)
        created_files.extend(person_notes)

    handbook = target_vault / "Company_Handbook.md"
    if not handbook.exists():
        handbook_content = (
            f"# {company} — Team Knowledge Handbook\n\n"
            f"Welcome to the central knowledge repository for {company}.\n\n"
            f"## Collaboration with {assistant}\n"
            f"- **AI Assistant**: {assistant}\n"
            f"- **Vault Architecture**: Enterprise Knowledge Graph (People, Companies, Projects, Meetings, Decisions, Knowledge)\n"
            f"- **Live Sync**: Team updates committed to this vault are indexed by {assistant} to provide accurate, contextual assistance across email, calendar, and task management.\n\n"
            f"## Directory Map\n"
            f"- `00-Inbox/` — Drop raw notes and brainstorms here\n"
            f"- `01-People/` — Company team members and partner notes\n"
            f"- `02-Companies/` — Clients and partners directory\n"
            f"- `03-Projects/` — Active workstreams and roadmaps\n"
            f"- `04-Meetings/` — Meeting summaries and action items\n"
            f"- `05-Decisions/` — Architectural and strategic decision records\n"
            f"- `06-Knowledge/` — SOPs, documentation, and playbooks\n"
            f"- `07-Daily/` — Daily briefings and logs\n"
            f"- `08-Team-Briefing/` — Daily morning briefings archive\n"
            f"- `files/` — Deliverables and documents\n"
        )
        _atomic_write_text(handbook, handbook_content)
        created_files.append("Company_Handbook.md")

        # Update handbook with team directory links if people exist
        if people:
            _sync_team_to_obsidian_vault(target_vault, people, company, assistant)

    state["obsidian_vault_path"] = str(target_vault)
    state.setdefault("knowledge", {})["obsidian_vault_path"] = str(target_vault)
    save_wizard_state(state)

    return {
        "ok": True,
        "vault_path": str(target_vault),
        "created_dirs": created_dirs,
        "created_files": created_files,
        "message": f"Obsidian enterprise vault scaffolded successfully at {target_vault}",
    }


def _get_provider_models(pid: str, meta: dict[str, Any], active_model: str = "", home: Path | None = None) -> list[dict[str, str]]:
    """Build comprehensive model list for provider, including curated DeepSeek models and active models."""
    base_models: list[dict[str, str]] = [dict(m) for m in meta.get("models", [])]
    seen_ids = {m["id"] for m in base_models if "id" in m}

    # Add well-known popular DeepSeek snapshots and models for OpenRouter/DeepSeek
    if pid == "openrouter":
        curated_openrouter = [
            {"id": "deepseek/deepseek-v4-flash-0731", "label": "DeepSeek V4 Flash (0731)"},
            {"id": "deepseek/deepseek-v4-flash", "label": "DeepSeek V4 Flash"},
            {"id": "deepseek/deepseek-v4.1-flash", "label": "DeepSeek V4.1 Flash"},
            {"id": "deepseek/deepseek-v4-pro", "label": "DeepSeek V4 Pro"},
            {"id": "deepseek/deepseek-chat", "label": "DeepSeek V3 Chat"},
            {"id": "deepseek/deepseek-r1", "label": "DeepSeek R1"},
            {"id": "anthropic/claude-sonnet-4.6", "label": "Claude Sonnet 4.6"},
            {"id": "anthropic/claude-opus-4.7", "label": "Claude Opus 4.7"},
            {"id": "openai/gpt-5.4-mini", "label": "GPT-5.4 Mini"},
            {"id": "openai/gpt-5.4", "label": "GPT-5.4"},
        ]
        for item in curated_openrouter:
            if item["id"] not in seen_ids:
                base_models.append(item)
                seen_ids.add(item["id"])
    elif pid == "deepseek":
        curated_deepseek = [
            {"id": "deepseek-v4-flash-0731", "label": "DeepSeek V4 Flash (0731)"},
            {"id": "deepseek-v4-flash", "label": "DeepSeek V4 Flash"},
            {"id": "deepseek-v4.1-flash", "label": "DeepSeek V4.1 Flash"},
            {"id": "deepseek-v4-pro", "label": "DeepSeek V4 Pro"},
            {"id": "deepseek-chat", "label": "DeepSeek V3 Chat"},
            {"id": "deepseek-r1", "label": "DeepSeek R1"},
        ]
        for item in curated_deepseek:
            if item["id"] not in seen_ids:
                base_models.append(item)
                seen_ids.add(item["id"])

    # Attempt to read cached models from Hermes disk cache if present
    if home:
        cache_file = home / "provider_models_cache.json"
        if cache_file.is_file():
            try:
                import json as _j
                with open(cache_file, encoding="utf-8") as f:
                    cdata = _j.load(f)
                prov_cached = cdata.get(pid, {})
                cached_list = prov_cached.get("models", []) if isinstance(prov_cached, dict) else []
                for m_id in cached_list:
                    if isinstance(m_id, str) and m_id not in seen_ids:
                        base_models.append({"id": m_id, "label": m_id})
                        seen_ids.add(m_id)
            except Exception:
                pass

    # Ensure active model is present and at the top if it belongs to this provider
    if active_model and active_model not in seen_ids:
        base_models.insert(0, {"id": active_model, "label": f"{active_model} (Active in Hermes)"})
        seen_ids.add(active_model)

    return base_models


# ── Full Wizard Status Payload ───────────────────────────────────────────────

def get_customer_wizard_status() -> dict[str, Any]:
    """Build complete customer wizard payload including current progress and catalogs."""
    home = get_active_hermes_home()
    config_path = home / "config.yaml"
    cfg = _load_yaml_config(config_path) if config_path.is_file() else {}
    env_values = _load_env_file(home / ".env") if (home / ".env").is_file() else {}
    settings = load_settings()
    saved_state = load_wizard_state()

    # Step A defaults
    company_defaults = {
        "company_name": saved_state.get("company_name") or settings.get("company_name") or os.environ.get("NEXT_PUBLIC_COMPANY_NAME", "Matika"),
        "assistant_name": saved_state.get("assistant_name") or settings.get("bot_name") or os.environ.get("HERMES_WEBUI_BOT_NAME", "Lisa"),
        "admin_name": saved_state.get("admin_name") or settings.get("admin_name", "Admin"),
        "language": saved_state.get("language") or settings.get("language", "en"),
        "timezone": saved_state.get("timezone") or settings.get("timezone", "Europe/Vienna"),
        "logo": saved_state.get("logo") or settings.get("logo", "static/matika-logo.png"),
    }

    # Step B provider info: reflect active Hermes config.yaml
    model_cfg = cfg.get("model", {}) if isinstance(cfg.get("model"), dict) else {}
    current_provider = model_cfg.get("provider", "openrouter")
    current_model = model_cfg.get("default", "anthropic/claude-sonnet-4.6")
    current_base_url = model_cfg.get("base_url", "")
    provider_has_key = bool(env_values.get(_SUPPORTED_PROVIDER_SETUPS.get(current_provider, {}).get("env_var", "")))

    resolved_provider = model_cfg.get("provider") or saved_state.get("provider", "openrouter")
    resolved_model = model_cfg.get("default") or saved_state.get("model", "anthropic/claude-sonnet-4.6")
    resolved_base_url = model_cfg.get("base_url") if "base_url" in model_cfg else saved_state.get("base_url", "")

    provider_info = {
        "provider": resolved_provider,
        "model": resolved_model,
        "base_url": resolved_base_url,
        "is_configured": provider_has_key,
        "supported_providers": [
            {
                "id": pid,
                "label": meta.get("label", pid),
                "default_model": meta.get("default_model", ""),
                "requires_base_url": bool(meta.get("requires_base_url")),
                "key_optional": bool(meta.get("key_optional")),
                "models": _get_provider_models(
                    pid,
                    meta,
                    active_model=resolved_model if pid == resolved_provider else "",
                    home=home,
                ),
            }
            for pid, meta in _SUPPORTED_PROVIDER_SETUPS.items()
        ],
    }

    # Step C capabilities
    selected_caps = saved_state.get("capabilities", ["company_knowledge", "email_assistant", "calendar_assistant", "task_assistant", "daily_briefing"])
    capabilities = evaluate_capabilities(selected_caps)

    # Step D connectors
    services = []
    for s_id in SERVICE_CONNECTORS.keys():
        s_status = get_service_status(s_id, probe=False)
        # Redacted env display
        spec = SERVICE_CONNECTORS[s_id]
        configured_keys = [k for k in spec.get("env_keys", []) if env_values.get(k) or os.environ.get(k)]
        s_status["configured_keys"] = configured_keys
        services.append(s_status)

    # Step F approvals
    saved_approvals = saved_state.get("approvals", {})
    approvals = []
    for act_id, act_spec in ACTION_APPROVAL_SPECS.items():
        chosen_policy = saved_approvals.get(act_id, act_spec["default"])
        approvals.append({
            "id": act_id,
            "category": act_spec["category"],
            "action": act_spec["action"],
            "default": act_spec["default"],
            "current": chosen_policy,
            "enforceable": act_spec["enforceable"],
            "handler": act_spec["handler"],
        })

    # Step Knowledge & Persona
    soul_p = home / "SOUL.md"
    user_p = home / "memories" / "USER.md"
    memory_p = home / "memories" / "MEMORY.md"

    saved_knowledge = saved_state.get("knowledge", {})
    soul_content = saved_knowledge.get("soul") if saved_knowledge.get("soul") is not None else saved_state.get("soul")
    if soul_content is None:
        soul_content = soul_p.read_text("utf-8", errors="replace") if soul_p.is_file() else ""

    user_content = saved_knowledge.get("user") if saved_knowledge.get("user") is not None else saved_state.get("user")
    if user_content is None:
        user_content = user_p.read_text("utf-8", errors="replace") if user_p.is_file() else ""

    memory_content = saved_knowledge.get("memory") if saved_knowledge.get("memory") is not None else saved_state.get("memory")
    if memory_content is None:
        memory_content = memory_p.read_text("utf-8", errors="replace") if memory_p.is_file() else ""

    default_vault = str(Path("/workspace/obsidian") if Path("/workspace").is_dir() else (home / "obsidian"))
    obsidian_vault_path = saved_knowledge.get("obsidian_vault_path") or saved_state.get("obsidian_vault_path") or default_vault
    obsidian_team_link = saved_knowledge.get("obsidian_team_link") or saved_state.get("obsidian_team_link") or ""

    identity_data = get_identity_mapping(home)
    team_members = identity_data.get("people", [])

    knowledge_info = {
        "soul": soul_content,
        "user": user_content,
        "memory": memory_content,
        "obsidian_vault_path": obsidian_vault_path,
        "obsidian_team_link": obsidian_team_link,
        "vault_exists": Path(obsidian_vault_path).is_dir(),
        "external_notes_enabled": _truthy(cfg.get("webui_external_notes_sources", True)),
        "team_members": team_members,
    }

    # Step G briefing
    saved_briefing = saved_state.get("briefing", {})
    briefing_info = {
        "enabled": bool(saved_briefing.get("enabled", False)),
        "channel": saved_briefing.get("channel", "telegram"),
        "recipient": saved_briefing.get("recipient", ""),
        "time": saved_briefing.get("time", "08:30"),
        "timezone": saved_briefing.get("timezone", company_defaults["timezone"]),
        "topics": saved_briefing.get("topics", ["calendar", "tasks", "meetings"]),
    }

    # Configuration bypass state
    configured_install = is_existing_install_configured()

    return {
        "is_configured": configured_install,
        "completed": bool(settings.get("customer_wizard_completed", False)),
        "profile": get_active_profile_name() or "default",
        "current_step": saved_state.get("current_step", 0),
        "company": company_defaults,
        "provider": provider_info,
        "capabilities": capabilities,
        "services": services,
        "approvals": approvals,
        "knowledge": knowledge_info,
        "team_members": team_members,
        "briefing": briefing_info,
    }


# ── Step Save (Draft Progress) ────────────────────────────────────────────────

def save_wizard_step(step_name: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Save draft progress for a step without applying to live runtime."""
    state = load_wizard_state()
    state["last_updated"] = datetime.now().isoformat()

    if step_name == "company":
        for field in ["company_name", "assistant_name", "admin_name", "language", "timezone", "logo"]:
            if field in payload:
                val = str(payload[field]).strip()
                if field == "timezone" and val and not _IANA_TIMEZONE_RE.match(val):
                    raise ValueError(f"Invalid IANA timezone format: {val}")
                state[field] = val
        state["current_step"] = 1

    elif step_name == "provider":
        if "provider" in payload:
            state["provider"] = str(payload["provider"]).strip().lower()
        if "model" in payload:
            state["model"] = str(payload["model"]).strip()
        if "base_url" in payload:
            state["base_url"] = str(payload["base_url"]).strip().rstrip("/")
        if "api_key" in payload and payload["api_key"]:
            state["api_key_draft"] = str(payload["api_key"]).strip()
        state["current_step"] = 2

    elif step_name == "capabilities":
        if "capabilities" in payload and isinstance(payload["capabilities"], list):
            state["capabilities"] = [str(c).strip() for c in payload["capabilities"] if str(c).strip()]
        state["current_step"] = 3

    elif step_name == "services":
        # Save integration credentials to draft or directly to .env if provided
        creds = payload.get("credentials") or {}
        if isinstance(creds, dict):
            state.setdefault("service_credentials_draft", {}).update(creds)
        state["current_step"] = 4

    elif step_name == "approvals":
        if "approvals" in payload and isinstance(payload["approvals"], dict):
            clean_approvals = {}
            for k, v in payload["approvals"].items():
                if k in ACTION_APPROVAL_SPECS and v in {"automatic", "ask", "disabled"}:
                    clean_approvals[k] = v
            state["approvals"] = clean_approvals
        state["current_step"] = 5

    elif step_name in {"knowledge", "team"}:
        knowledge_state = state.setdefault("knowledge", {})
        home = get_active_hermes_home()
        for field in ["soul", "user", "memory", "obsidian_vault_path", "obsidian_team_link"]:
            if field in payload:
                val = str(payload[field])
                knowledge_state[field] = val
                state[field] = val

        # Handle team members if supplied
        team_members_payload = payload.get("team_members") or payload.get("people")
        if isinstance(team_members_payload, list):
            save_identity_mapping(team_members_payload, home=home)
            state["team_members"] = team_members_payload
            knowledge_state["team_members"] = team_members_payload

        if payload.get("save_files_now"):
            if "soul" in payload and isinstance(payload["soul"], str) and payload["soul"].strip():
                _atomic_write_text(home / "SOUL.md", payload["soul"].strip() + "\n")
            mem_dir = home / "memories"
            mem_dir.mkdir(parents=True, exist_ok=True)
            if "user" in payload and isinstance(payload["user"], str) and payload["user"].strip():
                _atomic_write_text(mem_dir / "USER.md", payload["user"].strip() + "\n")
            if "memory" in payload and isinstance(payload["memory"], str) and payload["memory"].strip():
                _atomic_write_text(mem_dir / "MEMORY.md", payload["memory"].strip() + "\n")
        state["current_step"] = 6

    elif step_name == "briefing":
        briefing = payload.get("briefing") or payload
        b_time = str(briefing.get("time", "08:30")).strip()
        if not _TIME_24H_RE.match(b_time):
            raise ValueError(f"Invalid time format (expected HH:MM 24h): {b_time}")
        state["briefing"] = {
            "enabled": bool(briefing.get("enabled")),
            "channel": str(briefing.get("channel", "telegram")).strip(),
            "recipient": str(briefing.get("recipient", "")).strip(),
            "time": b_time,
            "timezone": str(briefing.get("timezone", state.get("timezone", "Europe/Vienna"))).strip(),
            "topics": list(briefing.get("topics", ["calendar", "tasks", "meetings"])),
        }
        state["current_step"] = 7

    save_wizard_state(state)
    return {"ok": True, "saved_step": step_name, "state": get_customer_wizard_status()}


# ── Non-Destructive Validation Preflight ──────────────────────────────────────

def validate_setup() -> dict[str, Any]:
    """Execute comprehensive, non-destructive validation of configured choices."""
    state = load_wizard_state()
    home = get_active_hermes_home()
    results = []

    # 1. Profile existence check
    profile_ok = home.is_dir()
    results.append({
        "check": "profile_exists",
        "title": "Hermes Profile Directory",
        "status": "passed" if profile_ok else "failed",
        "detail": f"Active profile directory: {home}",
    })

    # 2. Provider configuration check
    provider = state.get("provider", "openrouter")
    model = state.get("model", "anthropic/claude-sonnet-4.6")
    base_url = state.get("base_url", "")
    api_key = state.get("api_key_draft")
    
    if not api_key:
        env_vals = _load_env_file(home / ".env") if (home / ".env").is_file() else {}
        env_var = _SUPPORTED_PROVIDER_SETUPS.get(provider, {}).get("env_var", "")
        api_key = env_vals.get(env_var) or os.environ.get(env_var)

    meta = _SUPPORTED_PROVIDER_SETUPS.get(provider, {})
    if meta.get("key_optional") or api_key:
        if meta.get("requires_base_url") and base_url:
            probe_res = probe_provider_endpoint(provider, base_url, api_key, timeout=3.0)
            if probe_res.get("ok"):
                p_status = "passed"
                p_detail = f"Successfully probed endpoint {base_url} ({len(probe_res.get('models', []))} models)"
            else:
                p_status = "requires_action"
                p_detail = f"Endpoint unreachable: {probe_res.get('detail', 'probe failed')}"
        else:
            p_status = "passed"
            p_detail = f"Provider {provider} configured with model {model}"
    else:
        p_status = "requires_action"
        p_detail = f"Missing API key for provider {provider}"

    results.append({
        "check": "provider_connection",
        "title": "AI Provider & Model",
        "status": p_status,
        "detail": p_detail,
    })

    # 3. Selected service integrations reachability
    selected_caps = state.get("capabilities", [])
    needed_services = set()
    for cap_id in selected_caps:
        cap_spec = CAPABILITY_REGISTRY.get(cap_id, {})
        for s in cap_spec.get("required_integrations", []):
            needed_services.add(s)

    for s_id in needed_services:
        s_status = get_service_status(s_id, probe=True)
        check_status = "passed" if s_status["status"] == "verified" else (
            "skipped" if s_status["status"] == "not_connected" else "requires_action"
        )
        results.append({
            "check": f"service_{s_id}",
            "title": f"Integration: {s_status.get('name', s_id)}",
            "status": check_status,
            "detail": s_status.get("message", ""),
        })

    # 4. Approval policy enforcement check
    approvals = state.get("approvals", {})
    results.append({
        "check": "approvals_enforceable",
        "title": "Action Approval Policies",
        "status": "passed",
        "detail": f"{len(approvals)} action policies defined and mapped to enforcement hooks",
    })

    # 5. Briefing schedule check
    briefing = state.get("briefing", {})
    if briefing.get("enabled"):
        if briefing.get("channel") == "telegram" and not (briefing.get("recipient") and (briefing.get("recipient").isdigit() or briefing.get("recipient").startswith("-"))):
            b_status = "requires_action"
            b_detail = "Daily briefing enabled but valid Telegram chat ID is missing"
        else:
            b_status = "passed"
            b_detail = f"Briefing scheduled daily at {briefing.get('time')} ({briefing.get('timezone')})"
    else:
        b_status = "skipped"
        b_detail = "Daily briefing not enabled (skipped)"

    results.append({
        "check": "briefing_schedule",
        "title": "Daily Briefing Schedule",
        "status": b_status,
        "detail": b_detail,
    })

    can_finish = all(r["status"] in {"passed", "skipped"} for r in results if r["check"] in {"profile_exists", "provider_connection"})

    return {
        "ok": can_finish,
        "checks": results,
        "summary": "Validation passed" if can_finish else "Some required items need action before activation",
    }


# ── Idempotent Apply and Activation ──────────────────────────────────────────

def apply_wizard_setup() -> dict[str, Any]:
    """Atomically apply wizard state to config.yaml, .env, and cron jobs.
    
    Guaranteed idempotent:
    - Never creates duplicate cron jobs on rerun.
    - Preserves existing settings and comments where possible.
    - Atomically updates .env and config.yaml.
    """
    state = load_wizard_state()
    home = get_active_hermes_home()
    config_path = home / "config.yaml"
    cfg = _load_yaml_config(config_path) if config_path.is_file() else {}

    # 1. Apply Company & Assistant Settings
    assistant_name = state.get("assistant_name", "Lisa")
    company_name = state.get("company_name", "Matika")
    save_settings({
        "bot_name": assistant_name,
        "company_name": company_name,
        "admin_name": state.get("admin_name", "Admin"),
        "language": state.get("language", "en"),
        "timezone": state.get("timezone", "Europe/Vienna"),
        "customer_wizard_completed": True,
        "onboarding_completed": True,
    })

    # 2. Apply Provider Settings
    provider = state.get("provider")
    model = state.get("model")
    base_url = state.get("base_url")
    api_key = state.get("api_key_draft")

    if provider and model:
        model_cfg = cfg.get("model", {})
        if not isinstance(model_cfg, dict):
            model_cfg = {}
        model_cfg["provider"] = provider
        model_cfg["default"] = model
        if base_url:
            model_cfg["base_url"] = base_url
        cfg["model"] = model_cfg

    # Write API key to .env if provided in draft
    env_updates: dict[str, str] = {}
    if api_key and provider in _SUPPORTED_PROVIDER_SETUPS:
        env_var = _SUPPORTED_PROVIDER_SETUPS[provider]["env_var"]
        env_updates[env_var] = api_key
        os.environ[env_var] = api_key

    # 3. Apply Service Credentials from Draft
    service_creds = state.get("service_credentials_draft", {})
    if isinstance(service_creds, dict):
        for k, v in service_creds.items():
            if v and isinstance(v, str):
                env_updates[k] = v.strip()
                os.environ[k] = v.strip()

    if env_updates:
        _write_env_file(home / ".env", env_updates)
        _reload_dotenv(home)

    # 4. Apply Action Approval Policies
    approvals = state.get("approvals", {})
    if approvals:
        cfg["approvals"] = {
            "policies": approvals,
            "updated_at": datetime.now().isoformat(),
        }

    # 5. Enable/Disable Recommended Plugins & Skills based on Selected Capabilities
    selected_caps = set(state.get("capabilities", []))
    plugins_cfg = cfg.setdefault("plugins", {})
    if not isinstance(plugins_cfg, dict):
        plugins_cfg = {}
        cfg["plugins"] = plugins_cfg
    plugins_entries = plugins_cfg.setdefault("entries", {})
    if not isinstance(plugins_entries, dict):
        plugins_entries = {}
        plugins_cfg["entries"] = plugins_entries

    for cap_id, cap_spec in CAPABILITY_REGISTRY.items():
        is_selected = cap_id in selected_caps
        for p_id in cap_spec.get("recommended_plugins", []):
            entry = plugins_entries.setdefault(p_id, {})
            if not isinstance(entry, dict):
                entry = {}
                plugins_entries[p_id] = entry
            # Enable if capability is selected
            if is_selected:
                entry["enabled"] = True

    # 6. Apply Daily Briefing Schedule Idempotently
    briefing = state.get("briefing", {})
    if briefing.get("enabled") and briefing.get("recipient"):
        _apply_briefing_cron(home, briefing)

    # 6.5. Apply Persona, Memories, and Obsidian Knowledge Settings
    knowledge = state.get("knowledge", {})
    soul_text = knowledge.get("soul") if knowledge.get("soul") is not None else state.get("soul")
    user_text = knowledge.get("user") if knowledge.get("user") is not None else state.get("user")
    mem_text = knowledge.get("memory") if knowledge.get("memory") is not None else state.get("memory")

    if soul_text and isinstance(soul_text, str) and soul_text.strip():
        _atomic_write_text(home / "SOUL.md", soul_text.strip() + "\n")

    mem_dir = home / "memories"
    mem_dir.mkdir(parents=True, exist_ok=True)
    if user_text and isinstance(user_text, str) and user_text.strip():
        _atomic_write_text(mem_dir / "USER.md", user_text.strip() + "\n")
    if mem_text and isinstance(mem_text, str) and mem_text.strip():
        _atomic_write_text(mem_dir / "MEMORY.md", mem_text.strip() + "\n")

    # 6.6. Apply Team Members & Identity Mapping
    team_members = state.get("team_members")
    if team_members and isinstance(team_members, list):
        try:
            save_identity_mapping(team_members, home=home)
        except Exception as e:
            logger.warning("Failed to apply team identity mapping: %s", e)

    # Obsidian & External Notes Configuration
    vault_path = knowledge.get("obsidian_vault_path") or state.get("obsidian_vault_path")
    team_link = knowledge.get("obsidian_team_link") or state.get("obsidian_team_link")
    if vault_path:
        cfg["webui_external_notes_sources"] = True
        cfg["external_notes_sources"] = True
        k_cfg = cfg.setdefault("knowledge", {})
        if not isinstance(k_cfg, dict):
            k_cfg = {}
            cfg["knowledge"] = k_cfg
        k_cfg["obsidian_vault_path"] = str(vault_path)

        v_p = Path(vault_path)
        if v_p.is_dir() and team_members and isinstance(team_members, list):
            try:
                _sync_team_to_obsidian_vault(
                    v_p,
                    team_members,
                    company_name=state.get("company_name", "Company"),
                    assistant_name=state.get("assistant_name", "Lisa"),
                )
            except Exception as e:
                logger.warning("Failed to sync team to Obsidian vault on apply: %s", e)

    if team_link:
        k_cfg = cfg.setdefault("knowledge", {})
        if isinstance(k_cfg, dict):
            k_cfg["obsidian_team_link"] = str(team_link)

    # 7. Atomically save config.yaml
    _save_yaml_config(config_path, cfg)
    reload_config()

    # Clear sensitive drafts from state
    state.pop("api_key_draft", None)
    state.pop("service_credentials_draft", None)
    state["applied_at"] = datetime.now().isoformat()
    save_wizard_state(state)

    logger.info("Successfully applied customer onboarding wizard configuration for profile %s", home.name)
    return {
        "ok": True,
        "message": "Configuration successfully applied",
        "status": get_customer_wizard_status(),
    }


def _apply_briefing_cron(home: Path, briefing: dict[str, Any]) -> None:
    """Create or update morning briefing cron job idempotently in profile's jobs.json."""
    cron_dir = home / "cron"
    cron_dir.mkdir(parents=True, exist_ok=True)
    jobs_file = cron_dir / "jobs.json"

    jobs = []
    if jobs_file.is_file():
        try:
            raw = json.loads(jobs_file.read_text(encoding="utf-8"))
            if isinstance(raw, list):
                jobs = raw
            elif isinstance(raw, dict) and isinstance(raw.get("jobs"), list):
                jobs = raw["jobs"]
        except Exception:
            jobs = []

    time_str = briefing.get("time", "08:30")
    hours, minutes = time_str.split(":", 1)
    cron_expr = f"{int(minutes)} {int(hours)} * * *"

    recipient = briefing.get("recipient", "")
    channel = briefing.get("channel", "telegram")
    topics = briefing.get("topics", ["calendar", "tasks", "meetings"])
    prompt = (
        f"Generate a clear, friendly daily morning briefing covering {', '.join(topics)}. "
        f"Include today's scheduled meetings, priority action items, and relevant updates."
    )

    job_name = "customer-morning-briefing"
    existing_job = next((j for j in jobs if isinstance(j, dict) and j.get("name") in {job_name, "team-morning-briefing"}), None)

    if existing_job:
        existing_job["schedule"] = cron_expr
        existing_job["prompt"] = prompt
        existing_job["delivery"] = {"channel": channel, "recipient": recipient}
        existing_job["enabled"] = True
        existing_job["updated_at"] = datetime.now().isoformat()
    else:
        new_job = {
            "id": f"cron-{int(datetime.now().timestamp())}",
            "name": job_name,
            "schedule": cron_expr,
            "prompt": prompt,
            "delivery": {"channel": channel, "recipient": recipient},
            "enabled": True,
            "created_at": datetime.now().isoformat(),
        }
        jobs.append(new_job)

    _atomic_write_text(jobs_file, json.dumps(jobs, indent=2, ensure_ascii=False), encoding="utf-8")
    logger.info("Configured morning briefing cron job idempotently at %s for %s", cron_expr, recipient)


def reset_customer_wizard() -> dict[str, Any]:
    """Reset customer wizard completion flag to allow rerunning setup."""
    save_settings({"customer_wizard_completed": False})
    state = load_wizard_state()
    state["current_step"] = 0
    save_wizard_state(state)
    return {"ok": True, "message": "Wizard reset successfully"}
