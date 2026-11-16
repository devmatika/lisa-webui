"""Several e-mail accounts per person, and shared mailboxes the administrator grants.

Who may use an account is decided here, in Postgres, on every call: a person's own accounts at the
level they chose (``allow``), shared mailboxes at the granted level capped by the mailbox's ``allow``,
and nothing at all without the ``integration.email`` permission. The assistant reaches mail only
through ``/api/enterprise/mail/agent/*`` (org-authz plugin), so passwords and tokens never enter a
Hermes profile and a removed grant stops the next call.

Ownership is proven per kind: an IMAP password, a Google sign-in, or for Microsoft 365 (the
organisation's app can open any mailbox in the tenant) the person's own Lisa address only; other
Microsoft 365 mailboxes are shared mailboxes only an administrator can add.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from sqlalchemy import select

from api.enterprise import mail_backends as mb
from api.enterprise.directory import Actor, DirectoryError, audit, permissions_of
from api.enterprise.models import MailAccount, MailGrant, User, utcnow

LEVELS = {"read": 1, "draft": 2, "send": 3}
KINDS = ("imap", "google", "microsoft")
_EMAIL = re.compile(r"^[^@\s<>\"]+@[^@\s<>\"]+\.[^@\s<>\"]+$")
MAX_ACCOUNTS = 10


# ── secrets ──────────────────────────────────────────────────────────────────────────────────────

def _secret_dir() -> Path:
    from api.enterprise.knowledge import vault_root
    d = vault_root() / ".mail"
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(d, 0o700)
    return d


def _secret_path(account_id: str) -> Path:
    if not re.match(r"^[0-9a-f]{32}$", account_id or ""):
        raise DirectoryError("Unknown mail account")
    return _secret_dir() / f"{account_id}.json"


def _save_secret(account_id: str, secret: dict) -> None:
    path = _secret_path(account_id)
    path.write_text(json.dumps(secret))
    path.chmod(0o600)


def _secret(account_id: str) -> dict:
    try:
        return json.loads(_secret_path(account_id).read_text())
    except (OSError, ValueError):
        return {}


def _drop_secret(account_id: str) -> None:
    _secret_path(account_id).unlink(missing_ok=True)


# ── views and access ─────────────────────────────────────────────────────────────────────────────

def _acct(a: MailAccount) -> dict:
    return {"kind": a.kind, "address": a.address, "display_name": a.display_name, "settings": a.settings or {}}


def view(a: MailAccount, level: str | None = None) -> dict:
    s = a.settings or {}
    return {"id": a.id, "kind": a.kind, "address": a.address, "label": a.label, "display_name": a.display_name,
            "allow": a.allow, "level": level or a.allow, "shared": a.owner_id is None, "is_default": a.is_default,
            "server": s.get("imap_host", ""), "last_error": a.last_error,
            "last_checked_at": a.last_checked_at.isoformat() if a.last_checked_at else None}


def _cap(level: str, allow: str) -> str:
    return min(level, allow, key=lambda x: LEVELS.get(x, 0))


def accounts_for(db, user: User) -> list[tuple[MailAccount, str]]:
    """Every account this person may use now, with the level Lisa may act at."""
    if "integration.email" not in permissions_of(user):
        return []
    roles = {r.key for r in user.roles}
    own = list(db.scalars(select(MailAccount).where(MailAccount.owner_id == user.id).order_by(MailAccount.created_at)))
    out = [(a, a.allow) for a in own]
    best: dict[str, str] = {}
    for g in db.scalars(select(MailGrant)):
        if (g.principal_type == "user" and g.principal_id == user.id) or \
                (g.principal_type == "role" and g.principal_id in roles):
            if LEVELS.get(g.level, 0) > LEVELS.get(best.get(g.account_id, ""), 0):
                best[g.account_id] = g.level
    for aid, level in best.items():
        a = db.get(MailAccount, aid)
        if a is not None and a.owner_id is None:
            out.append((a, _cap(level, a.allow)))
    return out


def _pick(db, user: User, which: str | None, need: str) -> tuple[MailAccount, str]:
    allowed = accounts_for(db, user)
    if not allowed:
        raise DirectoryError("No e-mail account is connected. Add one under My account → E-mail accounts.")
    w = (which or "").strip().lower()
    if w:
        hits = [(a, lv) for a, lv in allowed if w in (a.id, a.address.lower(), a.label.lower())]
        if not hits:
            raise DirectoryError("No such e-mail account. Accounts: " +
                                 ", ".join(f"{a.label} <{a.address}>" for a, _ in allowed))
        if len(hits) > 1:
            raise DirectoryError("Several accounts match; name the address: " +
                                 ", ".join(a.address for a, _ in hits))
        acct, level = hits[0]
    else:
        own = [x for x in allowed if x[0].owner_id is not None]
        acct, level = next((x for x in own if x[0].is_default), None) or (own or allowed)[0]
    if LEVELS[level] < LEVELS[need]:
        verb = {"draft": "write drafts in", "send": "send from"}.get(need, "read")
        raise DirectoryError(f"Lisa may not {verb} {acct.address} (allowed: {level}). The owner can change "
                             "this under My account → E-mail accounts.")
    return acct, level


def _clean(label: str, allow: str) -> tuple[str, str]:
    label = (label or "").strip()[:60] or "Work"
    if allow not in LEVELS:
        raise DirectoryError("Choose what Lisa may do: read, draft or send")
    return label, allow


def _check_room(db, owner_id: str | None, address: str) -> None:
    rows = list(db.scalars(select(MailAccount).where(MailAccount.owner_id == owner_id))) if owner_id else \
        list(db.scalars(select(MailAccount).where(MailAccount.owner_id.is_(None))))
    if any(a.address.lower() == address.lower() for a in rows):
        raise DirectoryError(f"{address} is already connected.")
    if owner_id and len(rows) >= MAX_ACCOUNTS:
        raise DirectoryError(f"At most {MAX_ACCOUNTS} e-mail accounts per person.")


def _record(db, a: MailAccount, error: str | None) -> None:
    a.last_error, a.last_checked_at = error, utcnow()
    db.flush()


# ── adding accounts ──────────────────────────────────────────────────────────────────────────────

def _address(value: str) -> str:
    address = (value or "").strip().lower()
    if not _EMAIL.match(address):
        raise DirectoryError("Enter a valid e-mail address.")
    return address


def detect_imap(address: str, password: str, username: str = "") -> dict:
    """Find working IMAP/SMTP settings for an address by trying the known ones with the password."""
    hint = mb.provider_hint(address)
    if hint.get("blocked"):
        raise DirectoryError(hint["note"])
    last = None
    for s in mb.candidate_settings(address, username):
        try:
            mb.imap_test({"address": address, "settings": s}, {"password": password})
            return s
        except mb.MailError as exc:
            last = exc
            if "user name or password" in str(exc):
                break  # the server is right, the password is not: trying other hosts would not help
    msg = str(last) if last else "Lisa could not find the mail server."
    if hint.get("note"):
        msg += " " + hint["note"]
    raise DirectoryError(msg + " You can also enter the server settings yourself.")


def add_imap(db, actor: Actor, user: User | None, *, address: str, password: str, label: str, allow: str,
             display_name: str = "", settings: dict | None = None, grants: list[dict] | None = None) -> MailAccount:
    """A person's own IMAP account (``user``), or with ``user`` None a shared mailbox with ``grants``."""
    address = _address(address)
    label, allow = _clean(label, allow)
    if not password:
        raise DirectoryError("Enter the password (or app password) of the mailbox.")
    _check_room(db, user.id if user else None, address)
    try:
        if settings:
            s = mb.clean_settings(settings, address)
            mb.imap_test({"address": address, "settings": s}, {"password": password})
        else:
            s = detect_imap(address, password)
    except mb.MailError as exc:
        raise DirectoryError(str(exc)) from exc
    return _create(db, actor, user, kind="imap", address=address, label=label, allow=allow,
                   display_name=display_name, settings=s, secret={"password": password}, grants=grants)


def add_microsoft(db, actor: Actor, user: User | None, *, address: str, label: str, allow: str,
                  display_name: str = "", grants: list[dict] | None = None) -> MailAccount:
    """Microsoft 365 through the organisation's app: a person only their own address, the
    administrator any shared mailbox of the tenant."""
    if mb.microsoft_app() is None:
        raise DirectoryError("Microsoft 365 is not set up for this organisation yet. Ask IT.")
    address = _address(address or (user.email if user else ""))
    if user is not None and address != user.email.lower():
        raise DirectoryError("With Microsoft 365 you can only add your own work mailbox. Ask an administrator "
                             "to share a team mailbox with you.")
    label, allow = _clean(label, allow)
    _check_room(db, user.id if user else None, address)
    try:
        mb.microsoft_test({"address": address}, {})
    except mb.MailError as exc:
        raise DirectoryError(str(exc)) from exc
    return _create(db, actor, user, kind="microsoft", address=address, label=label, allow=allow,
                   display_name=display_name, settings={}, secret={}, grants=grants)


def _create(db, actor: Actor, user: User | None, *, kind, address, label, allow, display_name, settings, secret,
            grants) -> MailAccount:
    first = user is not None and not db.scalar(select(MailAccount.id).where(MailAccount.owner_id == user.id).limit(1))
    a = MailAccount(owner_id=user.id if user else None, kind=kind, address=address, label=label, allow=allow,
                    display_name=(display_name or (user.display_name if user else "") or "").strip()[:200],
                    settings=settings, is_default=first, last_checked_at=utcnow())
    db.add(a)
    db.flush()
    _save_secret(a.id, secret)
    if user is None:
        _set_grants(db, a.id, grants or [])
    audit(db, actor, "mail.account_added", a.id, address=address, kind=kind, label=label, allow=allow,
          shared=user is None)
    return a


def _set_grants(db, account_id: str, grants: list[dict]) -> None:
    for old in db.scalars(select(MailGrant).where(MailGrant.account_id == account_id)):
        db.delete(old)
    db.flush()
    for g in grants:
        if g.get("principal_type") not in ("user", "role") or g.get("level") not in LEVELS \
                or not str(g.get("principal_id") or "").strip():
            raise DirectoryError("Each grant needs principal_type (user/role), principal_id and level")
        db.add(MailGrant(account_id=account_id, principal_type=g["principal_type"],
                         principal_id=str(g["principal_id"]), level=g["level"]))


# ── Google sign-in for Gmail ─────────────────────────────────────────────────────────────────────

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]


def google_start(user_id: str, label: str, allow: str) -> str:
    from api.enterprise import integrations
    label, allow = _clean(label, allow)
    return integrations.google_start(user_id, scopes=GMAIL_SCOPES, purpose={"mail": True, "label": label,
                                                                             "allow": allow})


def google_finish(db, actor: Actor, user: User, tokens: dict, purpose: dict) -> MailAccount:
    secret = {"refresh_token": tokens["refresh_token"]}
    try:
        address = _address(mb.gmail_test({"address": ""}, secret).get("address", ""))
    except mb.MailError as exc:
        raise DirectoryError(str(exc)) from exc
    existing = db.scalar(select(MailAccount).where(MailAccount.owner_id == user.id, MailAccount.address == address))
    if existing is not None:  # signing in again renews the access
        _save_secret(existing.id, secret)
        _record(db, existing, None)
        audit(db, actor, "mail.account_renewed", existing.id, address=address)
        return existing
    _check_room(db, user.id, address)
    return _create(db, actor, user, kind="google", address=address, label=purpose.get("label") or "Personal",
                   allow=purpose.get("allow") or "draft", display_name="", settings={}, secret=secret, grants=None)


# ── changing and removing ────────────────────────────────────────────────────────────────────────

def _owned(db, user: User, account_id: str, admin: bool = False) -> MailAccount:
    a = db.get(MailAccount, account_id or "")
    if a is None or (a.owner_id != user.id and not (admin and a.owner_id is None)):
        raise DirectoryError("Unknown mail account")
    return a


def update(db, actor: Actor, user: User, account_id: str, *, label=None, allow=None, display_name=None,
           is_default=None, grants=None, admin: bool = False) -> MailAccount:
    a = _owned(db, user, account_id, admin)
    if label is not None or allow is not None:
        a.label, a.allow = _clean(label if label is not None else a.label, allow or a.allow)
    if display_name is not None:
        a.display_name = str(display_name).strip()[:200]
    if is_default and a.owner_id:
        for other in db.scalars(select(MailAccount).where(MailAccount.owner_id == a.owner_id)):
            other.is_default = other.id == a.id
    if grants is not None and a.owner_id is None:
        _set_grants(db, a.id, grants)
    db.flush()
    audit(db, actor, "mail.account_changed", a.id, address=a.address, label=a.label, allow=a.allow)
    return a


def remove(db, actor: Actor, user: User, account_id: str, admin: bool = False) -> None:
    a = _owned(db, user, account_id, admin)
    owner, was_default, address = a.owner_id, a.is_default, a.address
    _drop_secret(a.id)
    db.delete(a)
    db.flush()
    if owner and was_default:
        nxt = db.scalar(select(MailAccount).where(MailAccount.owner_id == owner).order_by(MailAccount.created_at))
        if nxt is not None:
            nxt.is_default = True
    audit(db, actor, "mail.account_removed", account_id, address=address)


def check(db, user: User, account_id: str, admin: bool = False) -> MailAccount:
    a = _owned(db, user, account_id, admin)
    try:
        mb.call(a.kind, "test", _acct(a), _secret(a.id))
        _record(db, a, None)
    except mb.MailError as exc:
        _record(db, a, str(exc))
    return a


def shared(db) -> list[dict]:
    grants = list(db.scalars(select(MailGrant)))
    return [{**view(a), "grants": [{"principal_type": g.principal_type, "principal_id": g.principal_id,
                                    "level": g.level} for g in grants if g.account_id == a.id]}
            for a in db.scalars(select(MailAccount).where(MailAccount.owner_id.is_(None)).order_by(MailAccount.address))]


# ── what the assistant may do ────────────────────────────────────────────────────────────────────

def _run(db, a: MailAccount, op: str, *args, **kwargs):
    try:
        out = mb.call(a.kind, op, _acct(a), _secret(a.id), *args, **kwargs)
    except mb.MailError as exc:
        _record(db, a, str(exc))
        raise DirectoryError(f"{a.address}: {exc}") from exc
    if a.last_error:
        _record(db, a, None)
    return out


def agent_accounts(db, user: User) -> dict:
    accts = accounts_for(db, user)
    return {"accounts": [{"account": a.address, "label": a.label, "kind": a.kind, "shared": a.owner_id is None,
                          "default": a.is_default, "lisa_may": a_level} for a, a_level in accts],
            "note": "Name the account by address or label; without one the default account is used."
            if accts else "No e-mail account is connected. The person can add one under My account → E-mail accounts."}


def agent_search(db, user: User, b: dict) -> dict:
    a, _ = _pick(db, user, b.get("account"), "read")
    limit = max(1, min(int(b.get("limit") or 10), mb.MAX_RESULTS))
    found = _run(db, a, "search", str(b.get("query") or ""), str(b.get("folder") or "inbox"), limit,
                 bool(b.get("unread_only")))
    return {"account": a.address, "label": a.label, "messages": found}


def agent_read(db, user: User, b: dict) -> dict:
    a, _ = _pick(db, user, b.get("account"), "read")
    return {"account": a.address, **_run(db, a, "read", str(b.get("id") or ""))}


def _message(db, a: MailAccount, b: dict):
    reply_id = str(b.get("reply_to_id") or "")
    original = _run(db, a, "read", reply_id) if reply_id else None
    try:
        msg = mb.compose(_acct(a), to=b.get("to"), cc=b.get("cc"), subject=str(b.get("subject") or ""),
                         body=str(b.get("body") or ""), reply_to=original)
    except mb.MailError as exc:
        raise DirectoryError(str(exc)) from exc
    extra = {}
    if original and a.kind == "google":
        extra["thread_id"] = original.get("thread_id", "")
    elif original and a.kind == "microsoft":
        extra["reply_id"] = reply_id
    return msg, extra


def agent_draft(db, user: User, b: dict) -> dict:
    a, _ = _pick(db, user, b.get("account"), "draft")
    msg, extra = _message(db, a, b)
    out = _run(db, a, "draft", msg, **extra)
    audit(db, Actor(user.id, "agent"), "mail.draft_created", a.id, address=a.address, to=str(msg["To"]),
          subject=str(msg["Subject"]))
    return {"account": a.address, **out, "note": "Saved as a draft; nothing was sent."}


def agent_send(db, user: User, b: dict) -> dict:
    a, _ = _pick(db, user, b.get("account"), "send")
    msg, extra = _message(db, a, b)
    out = _run(db, a, "send", msg, **extra)
    audit(db, Actor(user.id, "agent"), "mail.sent", a.id, address=a.address, to=str(msg["To"]),
          subject=str(msg["Subject"]))
    return {"account": a.address, **out}


__all__ = ["accounts_for", "add_imap", "add_microsoft", "google_start", "google_finish", "update", "remove",
           "check", "shared", "view", "agent_accounts", "agent_search", "agent_read", "agent_draft", "agent_send"]
