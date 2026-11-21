"""Organisation setup through the guided setup wizard.

In enterprise mode the wizard edits the default profile, the template every user's assistant is
built from (``api/enterprise/gate.py`` binds ``/api/customer-wizard/*`` to it). This module
gives the wizard its organisation checklist and, after Apply, refreshes every assistant so the
new model, provider keys and app credentials reach them. It also restarts the gateway so a new
Telegram bot token is picked up.
"""
from __future__ import annotations

import logging
import os
import threading

import yaml
from sqlalchemy import func, select

from api.enterprise import secretbox
from api.enterprise import db as edb
from api.enterprise import directory, integrations, mailer, provisioner
from api.enterprise.models import KnowledgeArea, Role, TelegramLink, User

logger = logging.getLogger(__name__)

# Organisation setup mirrors the classic onboarding surface, adapted for multi-user Lisa:
# capabilities nudge role grants + plugins; approvals are copied into every assistant and enforced
# by companion plugins; knowledge seeds company SOUL/MEMORY on the default template; team owns
# identity-mapping; briefing schedules the company morning cron on the shared bot profile.
STEPS = [
    "company", "provider", "capabilities", "services",
    "approvals", "knowledge", "orgknowledge", "team", "briefing", "finish",
]
# Company-wide connections. Google stays one sign-in per person (the OAuth client is the company's);
# the company ClickUp account shows each person the tasks assigned to their own e-mail.
SERVICES = ["telegram", "google_workspace", "microsoft_graph", "clickup"]

# Capability → permissions added to staff/manager/admin when selected (missing only; never strips).
_CAPABILITY_GRANTS = {
    "email_assistant": ("integration.google", "integration.outlook"),
    "calendar_assistant": ("integration.google", "integration.outlook"),
    "task_assistant": ("integration.clickup",),
}
_CAPABILITY_ROLES = ("staff", "manager", "admin")


def _root_config() -> dict:
    path = provisioner.root_home() / "config.yaml"
    data = yaml.safe_load(path.read_text()) if path.is_file() else None
    return data if isinstance(data, dict) else {}


def org_setup_status() -> dict:
    root_env = provisioner._read_env(provisioner.root_home() / ".env")
    model = _root_config().get("model")
    model_name = model.get("default") if isinstance(model, dict) else model
    with edb.session() as db:
        count = lambda q: db.scalar(select(func.count()).select_from(q))  # noqa: E731
        counts = {
            "users": count(User),
            "users_waiting_for_password": db.scalar(select(func.count()).where(User.password_hash.is_(None))),
            "roles": count(Role),
            "knowledge_areas": count(KnowledgeArea),
            "telegram_links": count(TelegramLink),
        }
    checks = {
        # a provider key in .env, or a custom / local endpoint configured inline (copied with ``model``)
        "model": bool(model_name) and (any(provisioner.PROVIDER_KEY.match(k) for k in root_env)
                                       or (isinstance(model, dict) and bool(model.get("api_key") or model.get("base_url")))),
        "telegram_bot": bool(root_env.get("TELEGRAM_BOT_TOKEN")),
        "google_client": integrations._google_ready(),
        "clickup_company": bool(root_env.get("CLICKUP_API_KEY")),
        "outlook_app": any(root_env.get(k) for k in ("MS_GRAPH_CLIENT_ID", "MICROSOFT_CLIENT_ID")),
        "email": mailer.configured(),
        "public_url": bool(os.environ.get("LISA_PUBLIC_URL", "").strip()),
        "voice": len(os.environ.get("LISA_VOICE_SECRET", "")) >= 32,
        "secret_key": secretbox.from_env(),
    }
    return {"steps": STEPS, "services": SERVICES, "model": model_name or "", "checks": checks,
            "counts": counts}


def apply_capability_grants(db) -> list[str]:
    """Grant missing integration permissions on staff+ roles from the wizard's selected capabilities."""
    from api.customer_wizard import load_wizard_state
    from api.enterprise.models import RolePermission

    caps = set(load_wizard_state().get("capabilities") or [])
    wanted: set[str] = set()
    for cap, perms in _CAPABILITY_GRANTS.items():
        if cap in caps:
            wanted.update(perms)
    if not wanted:
        return []
    applied: list[str] = []
    for role_key in _CAPABILITY_ROLES:
        role = db.scalar(select(Role).where(Role.key == role_key))
        if role is None:
            continue
        have = {p.permission for p in role.permissions}
        for perm in sorted(wanted - have):
            role.permissions.append(RolePermission(permission=perm))
            applied.append(f"{role_key}:{perm}")
    return applied


def after_org_setup(handler) -> dict:
    """Rebuild every assistant (and the front desk) from the new organisation settings, then
    restart the gateway gracefully. Runs in the background: each rebuild copies plugins."""
    from api.enterprise.routes import _actor, _provision
    with edb.session() as db:
        grants = apply_capability_grants(db)
        user_ids = list(db.scalars(select(User.id).where(User.hermes_profile.is_not(None))))
        directory.audit(db, _actor(handler), "org.setup.applied", "",
                        assistants=len(user_ids), capability_grants=grants)

    def refresh() -> None:
        for user_id in user_ids:
            try:
                _provision(user_id)
            except Exception:
                logger.exception("Refreshing assistant of user %s after organisation setup failed", user_id)
        provisioner.ensure_front_desk(refresh=True)
        provisioner.restart_gateway()

    threading.Thread(target=refresh, name="lisa-org-refresh", daemon=True).start()
    return {"refreshing": len(user_ids), "gateway_restart": True, "capability_grants": grants}


def refresh_after_upgrade() -> threading.Thread | None:
    """Rebuild every assistant once when this version's profile layout is newer than the one they were
    built with (e.g. new tools), in the background; then record the layout. No gateway restart:
    the rebuilt profiles are picked up by the gateway's rescan."""
    from api.enterprise.models import Organization
    from api.enterprise.routes import _provision
    with edb.session() as db:
        org = db.scalar(select(Organization))
        if org is None or (org.settings or {}).get("profile_layout") == provisioner.PROFILE_LAYOUT:
            return None
        user_ids = list(db.scalars(select(User.id).where(User.hermes_profile.is_not(None), User.status == "active")))

    def refresh() -> None:
        failed = 0
        for user_id in user_ids:
            try:
                _provision(user_id)
            except Exception:
                failed += 1
                logger.exception("Refreshing assistant of user %s after an upgrade failed", user_id)
        if failed:
            return  # tried again on the next start
        with edb.session() as db:
            org = db.scalar(select(Organization))
            org.settings = {**(org.settings or {}), "profile_layout": provisioner.PROFILE_LAYOUT}
            directory.audit(db, directory.Actor(None, "upgrade"), "org.assistants_refreshed", "",
                            assistants=len(user_ids), layout=provisioner.PROFILE_LAYOUT)

    thread = threading.Thread(target=refresh, name="lisa-upgrade-refresh", daemon=True)
    thread.start()
    return thread


def seal_stored_secrets() -> int:
    """Encrypt secrets saved before encryption at rest existed (mail accounts, vault credentials)."""
    from api.enterprise import secretbox
    from api.enterprise.knowledge import vault_root
    root = vault_root()
    files = [*(root / ".mail").glob("*.json"), *(root / ".sources" / "credentials").glob("*/token"),
             *(root / ".sources" / "credentials").glob("*/deploy_key")]
    sealed = sum(secretbox.upgrade_file(p) for p in files)
    if sealed:
        logger.info("Encrypted %d stored secret file(s)", sealed)
    return sealed
