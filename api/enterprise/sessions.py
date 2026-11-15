"""Browser sessions: login, per-request verification, logout and revocation.

The cookie holds an opaque token; Postgres holds its SHA-256, the user and the expiry. Every request
re-reads the user, so disabling an account or removing a role takes effect on the next request.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import delete, func, select, update

from api.enterprise import db as edb
from api.enterprise import directory, security
from api.enterprise.directory import Actor
from api.enterprise.models import AuthSession, LoginAttempt, User, utcnow

COOKIE_NAME = "lisa_session"
SESSION_TTL = timedelta(hours=12)
WINDOW = timedelta(minutes=15)
MAX_FAILURES_PER_EMAIL = 8
MAX_FAILURES_PER_IP = 30


class LoginError(Exception):
    def __init__(self, message: str, status: int = 401):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class SessionUser:
    """The verified identity of one request."""
    user_id: str
    email: str
    display_name: str
    hermes_profile: str | None
    roles: tuple[str, ...]
    permissions: frozenset[str]
    token_hash: str

    def can(self, permission: str) -> bool:
        return permission in self.permissions


def _failures(db, key: str) -> int:
    return db.scalar(select(func.count()).select_from(LoginAttempt)
                     .where(LoginAttempt.key == key, LoginAttempt.at > utcnow() - WINDOW)) or 0


def login(email: str, password: str, ip: str) -> tuple[str, SessionUser]:
    """Failures are committed before the error is raised: raising inside the unit of work would
    roll back the attempt counter and the audit row, and throttling would never trigger."""
    email = (email or "").strip().lower()
    error: LoginError | None = None
    with edb.session() as db:
        if _failures(db, f"ip:{ip}") >= MAX_FAILURES_PER_IP or _failures(db, f"email:{email}") >= MAX_FAILURES_PER_EMAIL:
            directory.audit(db, Actor(None, ip), "login.throttled", email)
            error = LoginError("Too many attempts. Try again in a few minutes.", 429)
        else:
            user = db.scalar(select(User).where(User.email == email))
            ok = security.verify_password(user.password_hash if user else None, password)
            if not ok or user is None or user.status != "active":
                db.add_all([LoginAttempt(key=f"ip:{ip}"), LoginAttempt(key=f"email:{email}")])
                directory.audit(db, Actor(user.id if user else None, ip), "login.failed", email)
                error = LoginError("Invalid e-mail or password")
            else:
                db.execute(delete(LoginAttempt).where(LoginAttempt.key == f"email:{email}"))
                token = security.new_token()
                th = security.token_hash(token)
                db.add(AuthSession(token_hash=th, user_id=user.id, expires_at=utcnow() + SESSION_TTL, ip=ip))
                directory.audit(db, Actor(user.id, ip), "login.succeeded", user.id)
                result = token, _session_user(user, th)
    if error is not None:
        raise error
    return result


def start_session_for(user_id: str, ip: str) -> str:
    """Session right after first-run setup or a password link (the user just proved control)."""
    token = security.new_token()
    with edb.session() as db:
        db.add(AuthSession(token_hash=security.token_hash(token), user_id=user_id,
                           expires_at=utcnow() + SESSION_TTL, ip=ip))
    return token


def verify(token: str | None) -> SessionUser | None:
    if not token:
        return None
    th = security.token_hash(token)
    with edb.session() as db:
        row = db.get(AuthSession, th)
        if row is None or row.revoked_at is not None or directory._aware(row.expires_at) <= utcnow():
            return None
        user = db.get(User, row.user_id)
        if user is None or user.status != "active":
            return None
        return _session_user(user, th)


def logout(token: str | None) -> None:
    if not token:
        return
    with edb.session() as db:
        db.execute(update(AuthSession).where(AuthSession.token_hash == security.token_hash(token))
                   .values(revoked_at=utcnow()))


def revoke_all(db, user_id: str) -> int:
    result = db.execute(update(AuthSession).where(AuthSession.user_id == user_id, AuthSession.revoked_at.is_(None))
                        .values(revoked_at=utcnow()))
    return result.rowcount or 0


def _session_user(user: User, th: str) -> SessionUser:
    return SessionUser(user_id=user.id, email=user.email, display_name=user.display_name,
                       hermes_profile=user.hermes_profile, roles=tuple(sorted(r.key for r in user.roles)),
                       permissions=directory.permissions_of(user), token_hash=th)
