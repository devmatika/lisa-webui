"""Per-member getting-started wizard (not the operator setup wizard).

Each person gets a private guided tour: what Lisa is for them, how chat / Files /
Knowledge work, then optional Connect Google, Microsoft 365 mailbox, and Telegram.
Prefs live in that person's Hermes profile (``lisa-prefs.json``), never in the shared org DB.
"""
from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy.orm import Session

from api.enterprise import directory, integrations, mail, mail_backends, provisioner, telegram
from api.enterprise.directory import DirectoryError
from api.enterprise.models import utcnow
from api.enterprise.sessions import SessionUser

PREFS_FILE = "lisa-prefs.json"
# Legacy key from the thin Connect-only overlay — still honoured as dismiss.
DISMISS_KEY = "connect_onboarding_dismissed_at"
COMPLETED_KEY = "member_onboarding_completed_at"
SKIPPED_KEY = "member_onboarding_skipped"
STEP_KEY = "member_onboarding_step"


def _prefs_path(profile: str) -> Path:
    return provisioner._home(profile) / PREFS_FILE


def load_prefs(profile: str) -> dict:
    path = _prefs_path(profile)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_prefs(profile: str, prefs: dict) -> None:
    home = provisioner._home(profile)
    if not home.is_dir():
        raise DirectoryError("Your assistant profile is not ready yet.")
    path = home / PREFS_FILE
    path.write_text(json.dumps(prefs, indent=2, sort_keys=True) + "\n")
    path.chmod(0o600)


def dismiss(profile: str) -> None:
    prefs = load_prefs(profile)
    prefs[DISMISS_KEY] = utcnow().isoformat()
    save_prefs(profile, prefs)


def complete(profile: str) -> None:
    prefs = load_prefs(profile)
    prefs[COMPLETED_KEY] = utcnow().isoformat()
    prefs.pop(DISMISS_KEY, None)
    save_prefs(profile, prefs)


def set_step(profile: str, step_id: str) -> None:
    prefs = load_prefs(profile)
    prefs[STEP_KEY] = str(step_id or "")
    save_prefs(profile, prefs)


def skip_action(profile: str, step_id: str) -> None:
    prefs = load_prefs(profile)
    skipped = list(prefs.get(SKIPPED_KEY) or [])
    if step_id and step_id not in skipped:
        skipped.append(step_id)
    prefs[SKIPPED_KEY] = skipped
    save_prefs(profile, prefs)


def reopen(profile: str) -> None:
    """Clear dismiss/complete so the wizard shows again (My account → Getting started)."""
    prefs = load_prefs(profile)
    prefs.pop(DISMISS_KEY, None)
    prefs.pop(COMPLETED_KEY, None)
    prefs.pop(SKIPPED_KEY, None)
    prefs[STEP_KEY] = "welcome"
    save_prefs(profile, prefs)


def _microsoft_connected(db: Session, user: SessionUser) -> bool:
    if not user.can("integration.email"):
        return False
    db_user = directory.get_user(db, user.user_id)
    return any(a.kind == "microsoft" and a.owner_id == db_user.id for a, _ in mail.accounts_for(db, db_user))


def _telegram_linked(db: Session, user_id: str) -> bool:
    return telegram.active_link(db, user_id) is not None


def status(db: Session, user: SessionUser) -> dict:
    """Full member wizard payload for the chat overlay."""
    profile = user.hermes_profile or ""
    prefs = load_prefs(profile) if profile else {}
    dismissed = bool(prefs.get(DISMISS_KEY))
    completed = bool(prefs.get(COMPLETED_KEY))
    skipped = set(prefs.get(SKIPPED_KEY) or [])
    saved_step = str(prefs.get(STEP_KEY) or "") or None

    google_allowed = user.can("integration.google")
    google_ready = integrations._google_ready()
    google_connected = bool(profile) and integrations.google_connected(profile)

    email_allowed = user.can("integration.email")
    outlook_allowed = user.can("integration.outlook")
    microsoft_ready = mail_backends.microsoft_app() is not None
    microsoft_connected = _microsoft_connected(db, user)

    telegram_linked = _telegram_linked(db, user.user_id)

    google = {
        "id": "google",
        "allowed": google_allowed,
        "ready": google_ready,
        "connected": google_connected,
        "skipped": "google" in skipped,
        "needed": google_allowed and google_ready and not google_connected and "google" not in skipped,
        "available": google_allowed,  # show the page even if company app not ready yet
    }
    microsoft = {
        "id": "microsoft",
        "allowed": email_allowed,
        "outlook": outlook_allowed,
        "ready": microsoft_ready,
        "connected": microsoft_connected,
        "skipped": "microsoft" in skipped,
        "needed": email_allowed and microsoft_ready and not microsoft_connected and "microsoft" not in skipped,
        "available": email_allowed,
        "own_email": user.email,
    }
    tg = {
        "id": "telegram",
        "linked": telegram_linked,
        "skipped": "telegram" in skipped,
        "needed": False,  # optional — never blocks finishing
        "optional": True,
        "available": True,
    }

    # Ordered pages the UI walks through.
    pages = [
        {"id": "welcome", "kind": "info", "title": "Welcome", "desc": "Your private assistant"},
        {"id": "basics", "kind": "info", "title": "How it works", "desc": "Chat, Files, Knowledge"},
    ]
    if google["available"]:
        pages.append({"id": "google", "kind": "action", "title": "Google", "desc": "Calendar, Docs, Drive",
                      "optional": not google_ready})
    if microsoft["available"]:
        pages.append({"id": "microsoft", "kind": "action", "title": "Microsoft 365", "desc": "Work mailbox",
                      "optional": not microsoft_ready})
    pages.append({"id": "telegram", "kind": "action", "title": "Telegram", "desc": "Optional · phone",
                  "optional": True})
    pages.append({"id": "finish", "kind": "info", "title": "You're set", "desc": "Start chatting"})

    action_done = {
        "google": google_connected or "google" in skipped or not google_allowed,
        "microsoft": microsoft_connected or "microsoft" in skipped or not email_allowed,
        "telegram": telegram_linked or "telegram" in skipped,
    }
    for p in pages:
        if p["kind"] == "info":
            p["done"] = True
        else:
            p["done"] = bool(action_done.get(p["id"]))

    needed = [name for name, step in (("google", google), ("microsoft", microsoft)) if step.get("needed")]
    show = (not completed and not dismissed)

    def _check(label, *, status, detail=""):
        return {"label": label, "status": status, "detail": detail}

    checklist = []
    if google_allowed:
        if google_connected:
            checklist.append(_check("Google", status="done", detail="Connected"))
        elif not google_ready:
            checklist.append(_check("Google", status="waiting", detail="Ask IT to add Google in Setup"))
        elif "google" in skipped:
            checklist.append(_check("Google", status="skipped", detail="Skipped — connect later in My account"))
        else:
            checklist.append(_check("Google", status="todo", detail="Connect your account"))
    if email_allowed:
        if microsoft_connected:
            checklist.append(_check("Microsoft 365", status="done", detail="Work mailbox ready"))
        elif not microsoft_ready:
            checklist.append(_check("Microsoft 365", status="waiting", detail="Ask IT to add Microsoft 365 in Setup"))
        elif "microsoft" in skipped:
            checklist.append(_check("Microsoft 365", status="skipped", detail="Skipped — connect later in My account"))
        else:
            checklist.append(_check("Microsoft 365", status="todo", detail="Connect your work mailbox"))
    if telegram_linked:
        checklist.append(_check("Telegram", status="done", detail="Linked"))
    elif "telegram" in skipped:
        checklist.append(_check("Telegram", status="skipped", detail="Optional — skipped"))
    else:
        checklist.append(_check("Telegram", status="todo", detail="Optional — link from your phone"))

    return {
        "show": show,
        "dismissed": dismissed,
        "completed": completed,
        "needed": needed,
        "display_name": user.display_name or user.email,
        "email": user.email,
        "roles": list(user.roles),
        "can_run_setup": user.can("operator.setup"),
        "step": saved_step,
        "pages": pages,
        "google": google,
        "microsoft": microsoft,
        "telegram": tg,
        "checklist": checklist,
        "company": {"google_ready": google_ready, "microsoft_ready": microsoft_ready},
        "progress": {
            "done": sum(1 for p in pages if p.get("done") and p["kind"] == "action"),
            "actions": sum(1 for p in pages if p["kind"] == "action"),
            "required_left": len(needed),
        },
    }


__all__ = ["status", "dismiss", "complete", "set_step", "skip_action", "reopen",
           "load_prefs", "save_prefs", "DISMISS_KEY", "COMPLETED_KEY"]
