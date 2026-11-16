"""Users, roles, first-run setup and audit. Every mutation writes an audit event.

Built-in roles are seeded once and then belong to the organisation: an admin may edit them, and a
later seed never overwrites those edits (it only adds roles and permissions that are missing).
The admin role manages people and roles; it does not include operator powers (host terminal, git,
profile switching) and it never grants access to another user's private profile.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from api.enterprise import security
from api.enterprise.models import (AuditEvent, Department, OneTimeToken, Organization, Role, RolePermission,
                                   RoleTemplate, User, UserRole, utcnow)

# Permission catalog. Route groups come from enterprise/phase0/webui-route-inventory.md.
PERMISSIONS = {
    "chat.use": "Chat with Lisa in their own private profile",
    "memory.own": "View and edit their own memory",
    "files.workspace": "Browse their own workspace files",
    "cron.own": "Manage their own scheduled tasks",
    "share.create": "Share their own conversations by link",
    "kanban.use": "Use the Kanban board",
    "integration.email": "Use the e-mail assistant on their own mailbox",
    "integration.google": "Use Google Calendar/Docs/Drive with their own connected Google account",
    "integration.outlook": "Use Outlook mail tools on their own mailbox (Microsoft 365)",
    "integration.clickup": "Use ClickUp with their own personal ClickUp token",
    "knowledge.read": "Search company knowledge areas they are granted",
    "knowledge.contribute": "Submit knowledge drafts",
    "knowledge.approve": "Approve and publish knowledge drafts",
    "knowledge.manage": "Create knowledge areas and decide who may read, contribute or approve",
    "org.users.manage": "Create, disable and edit users",
    "org.roles.manage": "Create and edit roles and their permissions",
    "org.audit.read": "Read the audit log",
    "org.settings.manage": "Edit organisation settings",
    **{f"operator.{k}": f"Operator: {k}" for k in (
        "terminal", "git", "mcp", "providers", "settings", "profiles", "updates", "extensions", "gateway",
        "logs", "system", "setup", "skills")},
}
_STAFF = ["chat.use", "memory.own", "files.workspace", "cron.own", "share.create", "integration.email",
          "knowledge.read", "knowledge.contribute"]
_MANAGER = _STAFF + ["knowledge.approve"]  # no kanban.use: Hermes boards are host-wide, not per user
BUILTIN_ROLES = {  # key -> (name, permissions, profile toolsets)
    "viewer": ("Viewer", ["chat.use", "memory.own", "knowledge.read"], ["memory", "session_search", "knowledge"]),
    "staff": ("Staff", _STAFF, ["memory", "todo", "session_search", "web", "knowledge"]),
    "manager": ("Manager", _MANAGER, ["memory", "todo", "session_search", "web", "delegation", "knowledge"]),
    "admin": ("Administrator", _MANAGER + ["org.users.manage", "org.roles.manage", "org.audit.read",
                                           "org.settings.manage", "knowledge.manage"],
              ["memory", "todo", "session_search", "web", "delegation", "knowledge"]),
    "operator": ("Operator (IT)", [p for p in PERMISSIONS if p.startswith("operator.")] + ["kanban.use"],
                 ["memory", "todo", "session_search", "web", "delegation"]),
}
# Toolsets an organisation role may grant. Never file/terminal/code/browser: a Hermes profile is not an
# OS boundary, so those would reach colleagues' profiles (Phase 0). Integrations arrive as plugins.
SAFE_TOOLSETS = ("memory", "todo", "session_search", "web", "delegation", "clarify", "knowledge")
_ROLE_KEY = re.compile(r"^[a-z][a-z0-9_-]{1,40}$")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
PASSWORD_LINK_TTL = timedelta(hours=48)


class DirectoryError(ValueError):
    pass


@dataclass(frozen=True)
class Actor:
    """Who is performing a change (``None`` id = the first-run wizard)."""
    id: str | None
    ip: str = ""


def audit(db: Session, actor: Actor, action: str, target: str = "", **detail) -> None:
    db.add(AuditEvent(actor_id=actor.id, action=action, target=target, detail=detail, ip=actor.ip))


def seed(db: Session) -> None:
    """Add missing built-in roles, permissions and templates; never overwrite edited ones."""
    existing = {r.key: r for r in db.scalars(select(Role))}
    for key, (name, perms, toolsets) in BUILTIN_ROLES.items():
        if key in existing:
            continue
        role = Role(key=key, name=name, builtin=True)
        role.permissions = [RolePermission(permission=p) for p in perms]
        role.template = RoleTemplate(toolsets=list(toolsets), plugins=[], skills=[])
        db.add(role)
    db.flush()


def needs_setup(db: Session) -> bool:
    return not db.scalar(select(func.count()).select_from(User))


def _normalize_email(email: str) -> str:
    email = (email or "").strip().lower()
    if not _EMAIL.match(email):
        raise DirectoryError("A valid e-mail address is required")
    return email


def _roles(db: Session, keys: list[str]) -> list[Role]:
    roles = list(db.scalars(select(Role).where(Role.key.in_(keys or []))))
    if not roles or len(roles) != len(set(keys)):
        raise DirectoryError("Unknown role")
    return roles


def first_run_setup(db: Session, *, org_name: str, email: str, password: str, display_name: str,
                    ip: str = "") -> User:
    """Create the organisation and its first administrator. Only possible while no user exists."""
    if db.bind.dialect.name == "postgresql":  # two browsers racing the wizard: exactly one wins
        db.execute(text("SELECT pg_advisory_xact_lock(7514220002)"))
    if not needs_setup(db):
        raise DirectoryError("Setup has already been completed")
    seed(db)
    db.add(Organization(name=(org_name or "").strip() or "Company", settings={}))
    user = User(email=_normalize_email(email), display_name=(display_name or "").strip(),
                password_hash=security.hash_password(password), status="active",
                roles=_roles(db, ["admin", "operator"]))
    db.add(user)
    db.flush()
    audit(db, Actor(None, ip), "setup.completed", user.id, email=user.email)
    from api.enterprise.knowledge_defaults import add_standard_areas
    add_standard_areas(db, Actor(user.id, ip))  # the standard vault folders, so nobody starts from zero
    return user


def create_user(db: Session, actor: Actor, *, email: str, display_name: str, role_keys: list[str],
                department: str | None = None) -> User:
    email = _normalize_email(email)
    if db.scalar(select(User).where(User.email == email)):
        raise DirectoryError("A user with this e-mail already exists")
    dept = None
    if department:
        dept = db.scalar(select(Department).where(Department.name == department)) or Department(name=department)
    user = User(email=email, display_name=(display_name or "").strip(), status="active",
                roles=_roles(db, role_keys), department_id=None)
    if dept is not None:
        db.add(dept)
        db.flush()
        user.department_id = dept.id
    db.add(user)
    db.flush()
    audit(db, actor, "user.created", user.id, email=email, roles=sorted(role_keys))
    return user


def get_user(db: Session, user_id: str) -> User:
    user = db.get(User, user_id)
    if user is None:
        raise DirectoryError("User not found")
    return user


# Without an active holder of each, nobody could ever add users or run the setup wizard again.
KEEP_HELD = {"org.users.manage": "manage users", "operator.setup": "run the setup wizard"}


def _ensure_org_keeps_control(db: Session) -> None:
    """Refuse a change that leaves no active user able to manage users or run setup."""
    db.flush()
    active = [u for u in db.scalars(select(User).where(User.status == "active"))]
    held = {p.permission for u in active for r in u.roles for p in r.permissions}
    lost = [label for perm, label in KEEP_HELD.items() if perm not in held]
    if lost:
        raise DirectoryError(f"At least one active user must be able to {' and '.join(lost)}. "
                             "Give that role to someone else first.")


def set_roles(db: Session, actor: Actor, user_id: str, role_keys: list[str]) -> User:
    user = get_user(db, user_id)
    user.roles = _roles(db, role_keys)
    _ensure_org_keeps_control(db)
    audit(db, actor, "user.roles_changed", user.id, roles=sorted(role_keys))
    return user


def set_status(db: Session, actor: Actor, user_id: str, status: str) -> User:
    if status not in ("active", "disabled"):
        raise DirectoryError("Status must be active or disabled")
    user = get_user(db, user_id)
    if status == "disabled" and user.id == actor.id:
        raise DirectoryError("You cannot disable your own account")
    user.status = status
    _ensure_org_keeps_control(db)
    audit(db, actor, f"user.{'enabled' if status == 'active' else 'disabled'}", user.id)
    return user


RESET_LINK_TTL = timedelta(hours=1)


def issue_password_link(db: Session, actor: Actor, user_id: str, ttl: timedelta = PASSWORD_LINK_TTL) -> str:
    """Single-use token to set a password (invitation or reset). Earlier unused links are voided."""
    user = get_user(db, user_id)
    now = utcnow()
    for old in db.scalars(select(OneTimeToken).where(OneTimeToken.user_id == user.id,
                                                    OneTimeToken.purpose == "password",
                                                    OneTimeToken.used_at.is_(None))):
        old.used_at = now
    token = security.new_token()
    db.add(OneTimeToken(token_hash=security.token_hash(token), purpose="password", user_id=user.id,
                        expires_at=now + ttl))
    audit(db, actor, "user.password_link_issued", user.id)
    return token


def redeem_password_link(db: Session, token: str, new_password: str, ip: str = "") -> User:
    row = db.get(OneTimeToken, security.token_hash(token or ""))
    now = utcnow()
    if row is None or row.purpose != "password" or row.used_at is not None or _aware(row.expires_at) < now:
        raise DirectoryError("This link is invalid or has expired")
    user = get_user(db, row.user_id)
    if user.status != "active":
        raise DirectoryError("This account is disabled")
    user.password_hash = security.hash_password(new_password)
    row.used_at = now
    audit(db, Actor(user.id, ip), "user.password_set", user.id)
    return user


def permissions_of(user: User) -> frozenset[str]:
    return frozenset(p.permission for role in user.roles for p in role.permissions)


def toolsets_of(user: User) -> list[str]:
    """Union of the user's role templates, in a stable order."""
    seen: dict[str, None] = {}
    for role in sorted(user.roles, key=lambda r: r.key):
        for ts in (role.template.toolsets if role.template else []):
            seen.setdefault(ts, None)
    return list(seen)


def _aware(dt):
    """SQLite returns naive datetimes; Postgres returns aware ones. Compare as UTC."""
    from datetime import timezone
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def save_role(db: Session, actor: Actor, *, key: str, name: str, permissions: list[str],
              toolsets: list[str]) -> Role:
    """Create or update a role. Unknown permissions and unsafe toolsets are refused."""
    key = (key or "").strip().lower()
    if not _ROLE_KEY.match(key):
        raise DirectoryError("Role key: lowercase letters, digits, - or _")
    bad_perm = sorted(set(permissions) - set(PERMISSIONS))
    if bad_perm:
        raise DirectoryError(f"Unknown permission(s): {', '.join(bad_perm)}")
    bad_ts = sorted(set(toolsets) - set(SAFE_TOOLSETS))
    if bad_ts:
        raise DirectoryError(f"Toolset(s) not allowed for organisation roles: {', '.join(bad_ts)}")
    role = db.scalar(select(Role).where(Role.key == key))
    created = role is None
    if created:
        role = Role(key=key, name=name or key, builtin=False)
        db.add(role)
    role.name = (name or role.name).strip()
    role.permissions = [RolePermission(permission=p) for p in sorted(set(permissions))]
    if role.template is None:
        role.template = RoleTemplate(toolsets=[], plugins=[], skills=[])
    role.template.toolsets = [t for t in SAFE_TOOLSETS if t in set(toolsets)]
    _ensure_org_keeps_control(db)
    audit(db, actor, "role.created" if created else "role.updated", key,
          permissions=sorted(set(permissions)), toolsets=role.template.toolsets)
    return role


def delete_role(db: Session, actor: Actor, key: str) -> None:
    role = db.scalar(select(Role).where(Role.key == key))
    if role is None:
        raise DirectoryError("Role not found")
    if role.builtin:
        raise DirectoryError("Built-in roles can be edited but not deleted")
    if db.scalar(select(func.count()).select_from(UserRole).where(UserRole.role_id == role.id)):
        raise DirectoryError("Role is still assigned to users")
    db.delete(role)
    audit(db, actor, "role.deleted", key)


def users_with_role(db: Session, key: str) -> list[User]:
    return [u for u in db.scalars(select(User)) if any(r.key == key for r in u.roles)]
