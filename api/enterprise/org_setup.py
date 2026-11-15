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

from api.enterprise import db as edb
from api.enterprise import directory, integrations, mailer, provisioner
from api.enterprise.models import KnowledgeArea, Role, TelegramLink, User

logger = logging.getLogger(__name__)

# The wizard steps that mean something for a whole organisation. Capabilities come from roles,
# persona/briefings belong to each user's own assistant, and approval policies have no consumer.
# ``orgknowledge`` and ``team`` work directly on the directory (areas, users), not on the draft.
STEPS = ["company", "provider", "services", "orgknowledge", "team", "finish"]
# Company-wide connections. Google stays one sign-in per person (the OAuth client is the company's);
# the company ClickUp account shows each person the tasks assigned to their own e-mail.
SERVICES = ["telegram", "google_workspace", "microsoft_graph", "clickup"]


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
    }
    return {"steps": STEPS, "services": SERVICES, "model": model_name or "", "checks": checks,
            "counts": counts}


def after_org_setup(handler) -> dict:
    """Rebuild every assistant (and the front desk) from the new organisation settings, then
    restart the gateway gracefully. Runs in the background: each rebuild copies plugins."""
    from api.enterprise.routes import _actor, _provision
    with edb.session() as db:
        user_ids = list(db.scalars(select(User.id).where(User.hermes_profile.is_not(None))))
        directory.audit(db, _actor(handler), "org.setup.applied", "", assistants=len(user_ids))

    def refresh() -> None:
        for user_id in user_ids:
            try:
                _provision(user_id)
            except Exception:
                logger.exception("Refreshing assistant of user %s after organisation setup failed", user_id)
        provisioner.ensure_front_desk(refresh=True)
        provisioner.restart_gateway()

    threading.Thread(target=refresh, name="lisa-org-refresh", daemon=True).start()
    return {"refreshing": len(user_ids), "gateway_restart": True}
