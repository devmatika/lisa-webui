"""One private Hermes profile per user, built from the user's role templates.

The profile is created empty (never cloned from the operator's profile, which would copy its
credentials and memory), then Lisa writes:
  config.yaml  the default profile's model settings, the role's toolsets for every surface the
               user can reach (WebUI chat = ``cli``, Telegram, API), and the media isolation policy
               proven in Phase 0 (``gateway.profile_isolation`` etc.)
  .env         only the model-provider keys from the default profile (no Google / ClickUp / mail
               credentials: those are per user or per grant) and a fresh per-profile API key
  lisa-owner.json  which Lisa user owns this profile, for audits
  lisa-allowlist.json + plugins/org-authz  the execution-time second line of defence: tools outside
               the role's toolsets are blocked even if they reach the agent some other way
Revocation parks the profile (``gateway.parked``) and asks the running gateway to rescan, which
stops serving it at once; re-enabling removes the marker and rescans again.
"""
from __future__ import annotations

import json
import logging
import re
import secrets
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

SURFACES = ("cli", "telegram", "api_server")
# Bump when what a profile gets changes (new tools, settings): on the next start Lisa rebuilds every
# assistant once, so an upgrade needs no "Apply" in the setup wizard. 2 = mail tools;
# 3 = Staff/Manager/Admin default Google + Outlook plugins after grant release;
# 4 = companion approval plugins + copy approvals/persona from the organisation template.
PROFILE_LAYOUT = 4
FRONT_DESK = "front-desk"
PLUGIN_SOURCE = Path("/opt/lisa/plugins")  # mounted read-only by the Lisa stack
REQUIRED_PLUGINS = ("org-authz",)
# Write-gate companions installed whenever the matching integration is granted.
APPROVAL_COMPANIONS = {
    "integration.google": ("business-action-approval",),
    "integration.outlook": ("business-action-approval",),
    "integration.email": ("business-action-approval",),
    "integration.clickup": ("clickup-approval",),
}
ALL_APPROVAL_PLUGINS = frozenset({"business-action-approval", "clickup-approval"})
# Role toolsets that are provided by Lisa plugins rather than Hermes core.
PLUGIN_TOOLSETS = {"knowledge": "lisa_knowledge"}
GOOGLE_SCRIPT = "/opt/hermes/skills/productivity/google-workspace/scripts/google_api.py"
# Integration permission -> plugin, its toolset, the secrets it may receive, per-user settings.
# A user only ever gets an integration's credentials when one of their roles grants it.
INTEGRATIONS = {
    "integration.google": {"plugin": "google-workspace-tools", "toolset": "google_workspace_tools",
                           "env": (), "user_files": ("google_token.json",), "user_env": (),
                           # shared Google libraries baked into the Lisa Hermes image; the token is the user's
                           "settings": lambda email: {"python_path": "/opt/lisa/gws-venv/bin/python",
                                                      "script_path": GOOGLE_SCRIPT, "account_label": email}},
    # the company account (setup wizard) finds each user's own tasks by e-mail; a personal token wins
    "integration.clickup": {"plugin": "clickup-personal", "toolset": "clickup_personal",
                            "env": ("CLICKUP_COMPANY_TOKEN",), "user_files": (), "user_env": ("CLICKUP_API_TOKEN",),
                            "settings": lambda email: {"account_email": email}},
    "integration.outlook": {"plugin": "microsoft-outlook-tools", "toolset": "microsoft_outlook_tools",
                            "env": ("MS_GRAPH_TENANT_ID", "MS_GRAPH_CLIENT_ID", "MS_GRAPH_CLIENT_SECRET",
                                    "MS_GRAPH_AUTH_MODE"), "user_files": (), "user_env": (),
                            # app-level Graph access can read any mailbox: pin it to the user's own
                            "settings": lambda email: {"account_address": email}},
    # several mailboxes per person; the tools ask the WebUI, which holds the secrets and checks access
    "integration.email": {"plugin": "org-authz", "toolset": "lisa_mail", "env": (), "user_files": (),
                          "user_env": (), "settings": None},
}
MEDIA_POLICY = {"profile_isolation": True, "strict": True, "trust_recent_files": False}
COPIED_CONFIG_KEYS = ("model", "providers", "custom_providers", "fallback_providers", "auxiliary",
                      "timezone", "approvals")


def companion_plugins(integrations: list[str]) -> list[str]:
    out: list[str] = []
    for key in integrations:
        out.extend(APPROVAL_COMPANIONS.get(key, ()))
    return list(dict.fromkeys(out))
# The setup wizard stores the Microsoft app and the company ClickUp token under these names; the
# Outlook plugin reads MS_GRAPH_*, and CLICKUP_API_KEY in a user's profile would read as their own.
ENV_ALIASES = {"MICROSOFT_TENANT_ID": "MS_GRAPH_TENANT_ID", "MICROSOFT_CLIENT_ID": "MS_GRAPH_CLIENT_ID",
               "MICROSOFT_CLIENT_SECRET": "MS_GRAPH_CLIENT_SECRET", "CLICKUP_API_KEY": "CLICKUP_COMPANY_TOKEN"}
PROVIDER_KEY = re.compile(r"^(OPENROUTER|OPENAI|ANTHROPIC|GOOGLE|GEMINI|DEEPSEEK|MISTRAL|GROQ|XAI|TOGETHER|"
                          r"FIREWORKS|AZURE_OPENAI|NOUS|CEREBRAS|XIAOMI|GLM|NVIDIA|OLLAMA|LM)_API_KEY$|^OPENAI_BASE_URL$")


def profile_name_for(user_id: str) -> str:
    return f"u-{user_id[:12]}"


def root_home() -> Path:
    from api.profiles import get_hermes_home_for_profile
    return Path(get_hermes_home_for_profile("default"))


def _home(name: str) -> Path:
    return root_home() / "profiles" / name


def _read_env(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if path.is_file():
        for line in path.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, value = line.split("=", 1)
                out[key.strip()] = value.strip()
    return out


def gateway_url() -> str:
    """Where the Hermes gateway's API listens inside the stack (deployment wiring, not behaviour)."""
    import os
    return os.environ.get("LISA_GATEWAY_URL", "http://hermes:8642").rstrip("/")


def webui_internal_url() -> str:
    """Where the gateway's knowledge tools reach this WebUI inside the stack (deployment wiring)."""
    import os
    return os.environ.get("LISA_WEBUI_INTERNAL_URL", "http://webui:8787").rstrip("/")


def assert_own_gateway_route(profile_name: str | None, url: str) -> None:
    """A Lisa user profile's turns must run on its own ``/p/<profile>`` route. Anything else (a
    launcher-exported shared URL outranks profile config upstream) would run the employee's chat in
    another profile, so the turn fails instead."""
    name = str(profile_name or "").strip()
    if name and (_home(name) / "lisa-owner.json").exists() and not url.rstrip("/").endswith(f"/p/{name}"):
        raise RuntimeError(f"Refusing to run profile {name!r} on {url!r}: not its own gateway route")


def ensure_root_gateway_url() -> None:
    """The default profile's (shared) gateway URL lives in its config.yaml, never in the process env."""
    path = root_home() / "config.yaml"
    cfg = (yaml.safe_load(path.read_text()) if path.is_file() else None) or {}
    if cfg.get("webui_gateway_base_url") != gateway_url():
        cfg["webui_gateway_base_url"] = gateway_url()
        path.write_text(yaml.safe_dump(cfg, sort_keys=False))


def _surface_toolsets(toolsets: list[str], integrations: list[str]) -> list[str]:
    core = [PLUGIN_TOOLSETS.get(t, t) for t in toolsets]
    return core + [INTEGRATIONS[i]["toolset"] for i in integrations]


def _google_plugin_settings(email: str, permissions) -> dict:
    """Google plugin settings for this user: write/upload only when the role grants drive_write."""
    base = INTEGRATIONS["integration.google"]["settings"](email) or {}
    settings = dict(base)
    caps = dict(settings.get("capabilities") or {})
    caps["drive_read"] = True
    caps["drive_upload"] = "integration.google.drive_write" in set(permissions or ())
    settings["capabilities"] = caps
    return settings


def render_config(default_cfg: dict, toolsets: list[str], profile_name: str | None = None,
                  integrations: list[str] = (), email: str = "", permissions=()) -> dict:
    cfg = {k: default_cfg[k] for k in COPIED_CONFIG_KEYS if k in default_cfg}
    if profile_name:
        # WebUI chat runs on the gateway under this profile's own route and key, never in-process.
        cfg["webui_gateway_base_url"] = f"{gateway_url()}/p/{profile_name}"
    cfg["platform_toolsets"] = {surface: _surface_toolsets(toolsets, list(integrations)) for surface in SURFACES}
    cfg["gateway"] = dict(MEDIA_POLICY)
    plugins = list(dict.fromkeys(
        list(REQUIRED_PLUGINS)
        + [INTEGRATIONS[i]["plugin"] for i in integrations]
        + companion_plugins(list(integrations))
    ))
    cfg["plugins"] = {"enabled": plugins}
    entries = {}
    for i in integrations:
        if not INTEGRATIONS[i]["settings"]:
            continue
        if i == "integration.google":
            entries[INTEGRATIONS[i]["plugin"]] = {"settings": _google_plugin_settings(email, permissions)}
        else:
            entries[INTEGRATIONS[i]["plugin"]] = {"settings": INTEGRATIONS[i]["settings"](email)}
    if entries:
        cfg["plugins"]["entries"] = entries
    return cfg


def allowed_tools(toolsets: list[str]) -> list[str]:
    """Tool names behind the role's core toolsets, resolved with Hermes' own toolset definitions."""
    from toolsets import resolve_toolset
    return sorted({tool for ts in toolsets if ts not in PLUGIN_TOOLSETS for tool in resolve_toolset(ts)})


def _install_plugins(home: Path, integrations: list[str] = ()) -> None:
    import shutil
    wanted = list(dict.fromkeys(
        list(REQUIRED_PLUGINS)
        + [INTEGRATIONS[i]["plugin"] for i in integrations]
        + companion_plugins(integrations)
    ))
    removable = ({v["plugin"] for v in INTEGRATIONS.values()} | ALL_APPROVAL_PLUGINS) - set(REQUIRED_PLUGINS)
    for stale in (home / "plugins").glob("*") if (home / "plugins").is_dir() else []:
        if stale.name not in wanted and stale.name in removable:
            shutil.rmtree(stale, ignore_errors=True)  # grant or companion no longer needed
    for name in wanted:
        src = PLUGIN_SOURCE / name
        if not (src / "plugin.yaml").is_file():
            raise RuntimeError(f"Lisa plugin '{name}' is missing at {src}; refusing to provision without it")
        dst = home / "plugins" / name
        shutil.rmtree(dst, ignore_errors=True)
        shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__", "tests", "test_*"))


def _seed_persona_from_default(home: Path, root: Path) -> None:
    """Copy company SOUL/MEMORY from the default template; seed USER.md only on first provision."""
    soul_src = root / "SOUL.md"
    if soul_src.is_file():
        (home / "SOUL.md").write_text(soul_src.read_text(encoding="utf-8"), encoding="utf-8")
    mem_src = root / "memories"
    mem_dst = home / "memories"
    mem_dst.mkdir(parents=True, exist_ok=True)
    memory_src = mem_src / "MEMORY.md"
    if memory_src.is_file():
        (mem_dst / "MEMORY.md").write_text(memory_src.read_text(encoding="utf-8"), encoding="utf-8")
    user_dst = mem_dst / "USER.md"
    user_src = mem_src / "USER.md"
    if not user_dst.is_file() and user_src.is_file():
        user_dst.write_text(user_src.read_text(encoding="utf-8"), encoding="utf-8")


def _write_allowlist(home: Path, toolsets: list[str], integrations: list[str] = ()) -> None:
    plugin_toolsets = [PLUGIN_TOOLSETS[t] for t in toolsets if t in PLUGIN_TOOLSETS]
    plugin_toolsets += [INTEGRATIONS[i]["toolset"] for i in integrations]
    (home / "lisa-allowlist.json").write_text(json.dumps({"toolsets": list(toolsets),
                                                          "tools": allowed_tools(toolsets),
                                                          "plugin_toolsets": plugin_toolsets}, indent=1))


def _write_private(path: Path, text: str) -> None:
    path.write_text(text)
    path.chmod(0o600)


def provision(user_id: str, toolsets: list[str], permissions=(), email: str = "") -> str:
    """Create (or refresh) the user's profile; returns its name. Idempotent: run it again after any
    role change and the profile matches the roles exactly (grants added, revoked ones removed)."""
    name = profile_name_for(user_id)
    perms = set(permissions or ())
    integrations = [p for p in INTEGRATIONS if p in perms]
    _build(name, toolsets, owner=user_id, integrations=integrations, email=email, permissions=perms)
    return name


def ensure_front_desk(refresh: bool = False) -> None:
    """Catch-all profile for Telegram senders who are not linked: no tools, no memory of anyone.
    ``refresh`` rebuilds it after the organisation's model settings changed."""
    if refresh or not (_home(FRONT_DESK) / "lisa-owner.json").exists():
        _build(FRONT_DESK, [], owner=FRONT_DESK)


def _build(name: str, toolsets: list[str], *, owner: str, integrations: list[str] = (),
           email: str = "", permissions=()) -> None:
    home = _home(name)
    if not home.exists():
        from hermes_cli.profiles import create_profile
        create_profile(name, no_skills=True, no_alias=True)
    root = root_home()
    default_cfg = yaml.safe_load((root / "config.yaml").read_text()) if (root / "config.yaml").is_file() else {}
    (home / "config.yaml").write_text(yaml.safe_dump(
        render_config(default_cfg or {}, toolsets, name, integrations, email, permissions), sort_keys=False))
    root_env = _read_env(root / ".env")
    for alias, canonical in ENV_ALIASES.items():
        if root_env.get(alias) and not root_env.get(canonical):
            root_env[canonical] = root_env[alias]
    if root_env.get("MS_GRAPH_CLIENT_SECRET"):
        # each user's mailbox is pinned by address, which only app (client-credentials) auth allows
        root_env.setdefault("MS_GRAPH_AUTH_MODE", "app")
    granted_env = {k for i in integrations for k in INTEGRATIONS[i]["env"]}
    env = {k: v for k, v in root_env.items() if PROVIDER_KEY.match(k) or k in granted_env}
    # Credentials the user connected themselves survive re-provisioning only while still granted.
    own_env = _read_env(home / ".env")
    kept_user_env = {k for i in integrations for k in INTEGRATIONS[i]["user_env"]}
    env.update({k: v for k, v in own_env.items() if k in kept_user_env})
    kept_files = {f for i in integrations for f in INTEGRATIONS[i]["user_files"]}
    for spec in INTEGRATIONS.values():
        for f in spec["user_files"]:
            if f not in kept_files:
                (home / f).unlink(missing_ok=True)
    env["API_SERVER_KEY"] = _read_env(home / ".env").get("API_SERVER_KEY") or secrets.token_urlsafe(32)
    if "knowledge" in toolsets:  # where the knowledge tools ask; the WebUI checks the user's area grants
        env["LISA_KNOWLEDGE_URL"] = webui_internal_url()
    if "integration.email" in integrations:  # where the mail tools ask; the WebUI checks the accounts
        env["LISA_WEBUI_URL"] = webui_internal_url()
    _write_private(home / ".env", "".join(f"{k}={v}\n" for k, v in env.items()))
    _install_plugins(home, integrations)
    _write_allowlist(home, toolsets, integrations)
    try:
        _seed_persona_from_default(home, root)
    except Exception:
        logger.warning("Could not seed persona files for profile %s", name, exc_info=True)
    (home / "lisa-owner.json").write_text(json.dumps({"user_id": owner}))  # last: enforcement starts here
    try:  # ClickUp / meetings read config/identity-mapping.json relative to this profile
        from api.enterprise import identity_mapping
        identity_mapping.publish_existing(root)
    except Exception:
        logger.warning("Could not publish identity-mapping into profiles", exc_info=True)
    _invalidate_webui_profile_cache()
    rescan()


def restart_gateway() -> dict | None:
    """Graceful gateway restart (drains running turns; the supervisor starts it again) so it reads
    new Telegram routes. Uses the control socket, so Lisa never needs the Docker socket."""
    try:
        from gateway.control_socket import pause_gateway_for_update
        return pause_gateway_for_update(root_home())
    except Exception:
        logger.warning("Gateway restart request failed; routes apply on the next gateway restart", exc_info=True)
        return None


def park(name: str) -> None:
    (_home(name) / "gateway.parked").write_text("disabled by Lisa\n")
    rescan()


def unpark(name: str) -> None:
    (_home(name) / "gateway.parked").unlink(missing_ok=True)
    rescan()


def rescan() -> dict | None:
    """Ask the gateway serving this Hermes root to reconcile profiles now (park/unpark/new)."""
    try:
        from gateway.control_socket import rescan_gateway_profiles
        return rescan_gateway_profiles(root_home())
    except Exception:
        logger.warning("Gateway rescan failed; the gateway picks the change up on its next 30 s scan",
                       exc_info=True)
        return None


def _invalidate_webui_profile_cache() -> None:
    from api.profiles import _invalidate_list_profiles_cache
    _invalidate_list_profiles_cache()


__all__ = ["PROFILE_LAYOUT", "profile_name_for", "provision", "ensure_front_desk", "restart_gateway", "root_home",
           "park", "unpark", "rescan", "render_config", "allowed_tools",
           "assert_own_gateway_route", "ensure_root_gateway_url"]
