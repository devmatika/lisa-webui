"""Telegram: one company bot, each employee routed to their own profile.

Linking reuses Hermes' DM pairing (hashed one-hour codes, rate limits, lockout): an unknown user
who writes to the bot gets a pairing code; signed in to Lisa, they enter it here. Approving the
code yields the immutable numeric Telegram id, which Lisa records against the signed-in user.

Lisa is the source of truth afterwards. ``apply()`` makes the gateway match Postgres:
  * ``gateway.profile_routes`` in the shared bot's (default) profile: one ``user_id + chat_id``
    route per active link -> that user's profile, then a catch-all -> ``front-desk`` (no tools, no data)
  * ``TELEGRAM_ALLOWED_USERS`` in the shared bot's ``.env`` = the linked, active users. Hermes decides
    authorization for a routed message with the RECEIVING bot's allowlist, so this is the authority.
    Pairing approvals are not: under multiplexing the pairing store consulted is the routed profile's
    (front-desk for strangers, the user's own once linked), so Lisa uses pairing only as proof of the
    Telegram id and revokes every approval it finds in the shared stores
  * ``config/identity-mapping.json`` is merged and published to every assistant profile
    (see ``api.enterprise.identity_mapping``)
  * the gateway restarts gracefully (control socket, drains running turns) only when routes changed;
    Hermes reads routes at boot. Until then a newly approved user lands on the front desk.
Disabling a user parks their profile first (immediate) and unlinks Telegram.
"""
from __future__ import annotations

import logging
from pathlib import Path

import yaml
from sqlalchemy import select

from api.enterprise import identity_mapping, provisioner
from api.enterprise.directory import Actor, DirectoryError, audit
from api.enterprise.models import TelegramLink, User, utcnow

logger = logging.getLogger(__name__)
PLATFORM = "telegram"
FRONT_DESK = "front-desk"

PAIRING_STORES = (FRONT_DESK, "default")  # strangers are routed to front-desk once routes exist


def _store(profile: str):
    from gateway.pairing import PairingStore
    return PairingStore(profile=profile)


def _approve(code: str) -> dict | None:
    """The code the bot gave this person, wherever the gateway filed it."""
    for profile in PAIRING_STORES:
        result = _store(profile).approve_code(PLATFORM, code)
        if result:
            return result
    return None


def active_link(db, user_id: str) -> TelegramLink | None:
    return db.scalar(select(TelegramLink).where(TelegramLink.user_id == user_id, TelegramLink.revoked_at.is_(None)))


def link(db, actor: Actor, user_id: str, code: str) -> TelegramLink:
    """Approve the pairing code the user received from the bot and link that Telegram id to them."""
    result = _approve(code or "")
    if not result:
        raise DirectoryError("This code is invalid, expired or already used. Write to the bot again for a new one.")
    tg = str(result["user_id"])
    other = db.scalar(select(TelegramLink).where(TelegramLink.telegram_user_id == tg,
                                                 TelegramLink.revoked_at.is_(None)))
    if other is not None and other.user_id != user_id:
        audit(db, actor, "telegram.link_conflict", user_id, telegram_user_id=tg)
        raise DirectoryError("This Telegram account is already linked to another Lisa user.")
    if other is not None:
        return other
    current = active_link(db, user_id)
    if current is not None:
        current.revoked_at = utcnow()
        db.flush()
    row = TelegramLink(telegram_user_id=tg, user_id=user_id, telegram_name=str(result.get("user_name") or ""))
    db.add(row)
    db.flush()
    audit(db, actor, "telegram.linked", user_id, telegram_user_id=tg, telegram_name=row.telegram_name)
    return row


def unlink(db, actor: Actor, user_id: str) -> bool:
    current = active_link(db, user_id)
    if current is None:
        return False
    current.revoked_at = utcnow()
    audit(db, actor, "telegram.unlinked", user_id, telegram_user_id=current.telegram_user_id)
    return True


def _active_pairs(db) -> list[tuple[TelegramLink, User]]:
    rows = db.execute(select(TelegramLink, User).join(User, User.id == TelegramLink.user_id)
                      .where(TelegramLink.revoked_at.is_(None), User.status == "active",
                             User.hermes_profile.is_not(None))
                      .order_by(TelegramLink.telegram_user_id))
    return [(lnk, user) for lnk, user in rows]


def desired_routes(pairs) -> list[dict]:
    routes = [{"name": f"lisa-{lnk.telegram_user_id}", "platform": PLATFORM, "user_id": lnk.telegram_user_id,
               "chat_id": lnk.telegram_user_id, "profile": user.hermes_profile} for lnk, user in pairs]
    routes.append({"name": "lisa-front-desk", "platform": PLATFORM, "profile": FRONT_DESK})
    return routes


def _write_routes(root: Path, routes: list[dict]) -> bool:
    """Replace the Telegram routes Lisa owns in the default profile's config; True when they changed."""
    path = root / "config.yaml"
    cfg = (yaml.safe_load(path.read_text()) if path.is_file() else None) or {}
    gw = cfg.setdefault("gateway", {})
    kept = [r for r in gw.get("profile_routes") or [] if r.get("platform") != PLATFORM]
    new = kept + routes
    if gw.get("profile_routes") == new and gw.get("multiplex_profiles") is True:
        return False
    gw["profile_routes"] = new
    gw["multiplex_profiles"] = True
    path.write_text(yaml.safe_dump(cfg, sort_keys=False))
    return True


def _write_identity_mapping(root: Path, db) -> None:
    """Upsert active Lisa users into identity-mapping.json and publish into every profile."""
    identity_mapping.sync(db, root)


def _revoke_pairing_approvals() -> list[str]:
    """Pairing never grants access by itself in Lisa; the allowlist does."""
    revoked = []
    for profile in PAIRING_STORES:
        store = _store(profile)
        for row in store.list_approved(PLATFORM):
            store.revoke(PLATFORM, str(row.get("user_id")))
            revoked.append(str(row.get("user_id")))
    return revoked


def _write_allowlist(root: Path, ids: list[str]) -> bool:
    """Set TELEGRAM_ALLOWED_USERS in the shared bot's .env; True when it changed."""
    path = root / ".env"
    lines = path.read_text().splitlines() if path.is_file() else []
    value = ",".join(ids)
    kept = [ln for ln in lines if not ln.startswith("TELEGRAM_ALLOWED_USERS=")]
    old = next((ln.split("=", 1)[1] for ln in lines if ln.startswith("TELEGRAM_ALLOWED_USERS=")), None)
    if old == value:
        return False
    path.write_text("\n".join(kept + [f"TELEGRAM_ALLOWED_USERS={value}"]) + "\n")
    path.chmod(0o600)
    return True


def apply(db) -> dict:
    """Make the gateway match Postgres. Safe to call after every change."""
    provisioner.ensure_front_desk()
    root = provisioner.root_home()
    pairs = _active_pairs(db)
    routes_changed = _write_routes(root, desired_routes(pairs))
    allow_changed = _write_allowlist(root, sorted(lnk.telegram_user_id for lnk, _ in pairs))
    revoked = _revoke_pairing_approvals()
    _write_identity_mapping(root, db)
    changed = routes_changed or allow_changed
    restarted = provisioner.restart_gateway() if changed else None
    return {"routes_changed": changed, "approvals_revoked": revoked, "restart": restarted}
