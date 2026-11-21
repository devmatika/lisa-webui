"""Organisation-wide people directory for ClickUp / Telegram / meetings / briefings.

Source of truth: ``{HERMES_HOME}/config/identity-mapping.json`` (the default / root home the
setup wizard edits). Each assistant profile also needs a copy under
``profiles/<name>/config/`` — skills and plugins resolve the file relative to that profile's
``HERMES_HOME``, not the organisation root.

``sync(db)`` upserts active Lisa users by e-mail (Telegram ids from active links win) while
preserving ClickUp ids, aliases, roles, extra e-mails, and people who have no Lisa login.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from sqlalchemy import select

from api.enterprise import provisioner
from api.enterprise.models import TelegramLink, User

logger = logging.getLogger(__name__)


def mapping_path(root: Path | None = None) -> Path:
    return (root or provisioner.root_home()) / "config" / "identity-mapping.json"


def person_emails(person: dict) -> set[str]:
    emails: list[str] = []
    raw = person.get("emails")
    if isinstance(raw, list):
        emails.extend(str(e).strip() for e in raw if e and str(e).strip())
    if person.get("email"):
        emails.append(str(person["email"]).strip())
    return {e.lower() for e in emails if e}


def load(path: Path | None = None) -> dict:
    path = path or mapping_path()
    if not path.is_file():
        return {"people": [], "aliases": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        logger.warning("Could not parse %s; starting a fresh merge", path, exc_info=True)
        return {"people": [], "aliases": {}}
    if not isinstance(data, dict):
        return {"people": [], "aliases": {}}
    people = data.get("people") if isinstance(data.get("people"), list) else []
    aliases = data.get("aliases") if isinstance(data.get("aliases"), dict) else {}
    return {"people": [p for p in people if isinstance(p, dict)], "aliases": aliases}


def rebuild_aliases(people: list[dict], previous: dict | None = None) -> dict:
    aliases: dict[str, str] = {}
    valid = {str(p.get("person_id")) for p in people if p.get("person_id")}
    for p in people:
        pid = str(p.get("person_id") or "").strip()
        if not pid:
            continue
        for key in person_emails(p):
            aliases[key] = pid
        name = str(p.get("canonical_name") or p.get("name") or "").strip()
        if name:
            aliases[name.lower()] = pid
        for alias in (p.get("aliases") or [] if isinstance(p.get("aliases"), list) else []):
            a = str(alias or "").strip()
            if a:
                aliases[a.lower()] = pid
    for key, pid in (previous or {}).items():
        if str(pid) in valid and key and key not in aliases:
            aliases[str(key)] = str(pid)
    return aliases


def _active_telegram_links(db) -> dict[str, str]:
    rows = db.execute(
        select(TelegramLink, User)
        .join(User, User.id == TelegramLink.user_id)
        .where(TelegramLink.revoked_at.is_(None), User.status == "active")
    )
    return {lnk.user_id: lnk.telegram_user_id for lnk, _ in rows}


def merge_users(existing: dict, users: list[User], linked: dict[str, str]) -> dict:
    by_email: dict[str, dict] = {}
    for person in existing.get("people") or []:
        for email in person_emails(person):
            by_email.setdefault(email, person)

    matched: set[int] = set()
    people: list[dict] = []
    for user in users:
        email = (user.email or "").strip().lower()
        prev = by_email.get(email)
        person = dict(prev) if prev is not None else {}
        if prev is not None:
            matched.add(id(prev))

        person["person_id"] = user.id
        display = (user.display_name or user.email or "").strip()
        name = str(person.get("canonical_name") or person.get("name") or display).strip() or display
        person["name"] = name
        person["canonical_name"] = str(person.get("canonical_name") or name).strip() or name

        emails = [str(e).strip() for e in (person.get("emails") or []) if e and str(e).strip()]
        if not emails and person.get("email"):
            emails = [str(person["email"]).strip()]
        if user.email and user.email.strip().lower() not in {e.lower() for e in emails}:
            emails = [user.email.strip(), *emails]
        if emails:
            person["emails"] = emails
            person["email"] = emails[0]
        else:
            person["email"] = user.email

        tele = dict(person["telegram"]) if isinstance(person.get("telegram"), dict) else {}
        if user.id in linked:
            tele["user_id"] = linked[user.id]
            person["telegram"] = tele
        elif tele:
            person["telegram"] = tele
        else:
            person.pop("telegram", None)

        people.append(person)

    for person in existing.get("people") or []:
        if id(person) not in matched:
            people.append(person)

    return {
        "generated_by": "lisa-enterprise",
        "people": people,
        "aliases": rebuild_aliases(people, existing.get("aliases")),
    }


def publish(payload: dict, root: Path | None = None) -> list[str]:
    """Write the mapping at the organisation root and copy it into every profile config/."""
    root = root or provisioner.root_home()
    text = json.dumps(payload, indent=1, ensure_ascii=False) + "\n"
    written: list[str] = []
    targets = [root / "config"]
    profiles = root / "profiles"
    if profiles.is_dir():
        targets.extend(p / "config" for p in profiles.iterdir() if p.is_dir())
    for cfg in targets:
        try:
            cfg.mkdir(parents=True, exist_ok=True)
            dest = cfg / "identity-mapping.json"
            dest.write_text(text, encoding="utf-8")
            written.append(str(dest))
        except Exception:
            logger.warning("Could not publish identity-mapping to %s", cfg, exc_info=True)
    return written


def sync(db, root: Path | None = None) -> dict:
    """Upsert active Lisa users into identity-mapping.json and publish to every profile."""
    root = root or provisioner.root_home()
    out = mapping_path(root)
    existing = load(out)
    users = list(db.scalars(select(User).where(User.status == "active").order_by(User.email)))
    payload = merge_users(existing, users, _active_telegram_links(db))
    publish(payload, root)
    return payload


def publish_existing(root: Path | None = None) -> list[str]:
    """Re-copy the current root mapping into every profile (e.g. after a wizard Save)."""
    root = root or provisioner.root_home()
    return publish(load(mapping_path(root)), root)
