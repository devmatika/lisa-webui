"""Lisa Enterprise schema. Portable column types only (runs on Postgres in production, SQLite in tests).

Authorization data lives here, never in Markdown frontmatter or YAML. Users are linked to exactly
one Hermes profile (``users.hermes_profile``); that link is the only way a request reaches a
profile. Changes are made through migrations in ``api/enterprise/migrations``.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def _uuid() -> str:
    return uuid.uuid4().hex


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Organization(Base):
    __tablename__ = "organization"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(200))
    settings: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Department(Base):
    __tablename__ = "departments"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(200), unique=True)


class Role(Base):
    __tablename__ = "roles"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    key: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    builtin: Mapped[bool] = mapped_column(Boolean, default=False)
    permissions: Mapped[list["RolePermission"]] = relationship(cascade="all, delete-orphan", lazy="selectin")
    template: Mapped["RoleTemplate | None"] = relationship(back_populates="role", uselist=False,
                                                           cascade="all, delete-orphan", lazy="selectin")


class RolePermission(Base):
    __tablename__ = "role_permissions"
    role_id: Mapped[str] = mapped_column(ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True)
    permission: Mapped[str] = mapped_column(String(100), primary_key=True)


class RoleTemplate(Base):
    """What a user's Hermes profile looks like for this role: toolsets, plugins, skills."""
    __tablename__ = "role_templates"
    role_id: Mapped[str] = mapped_column(ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True)
    toolsets: Mapped[list] = mapped_column(JSON, default=list)
    plugins: Mapped[list] = mapped_column(JSON, default=list)
    skills: Mapped[list] = mapped_column(JSON, default=list)
    role: Mapped[Role] = relationship(back_populates="template")


class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    email: Mapped[str] = mapped_column(String(320), unique=True)
    display_name: Mapped[str] = mapped_column(String(200), default="")
    password_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="active")  # active | disabled
    department_id: Mapped[str | None] = mapped_column(ForeignKey("departments.id", ondelete="SET NULL"),
                                                      nullable=True)
    hermes_profile: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True)
    oidc_subject: Mapped[str | None] = mapped_column(String(255), unique=True, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    roles: Mapped[list[Role]] = relationship(secondary="user_roles", lazy="selectin")


class UserRole(Base):
    __tablename__ = "user_roles"
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    role_id: Mapped[str] = mapped_column(ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True)


class AuthSession(Base):
    """A browser login. Only the SHA-256 of the token is stored."""
    __tablename__ = "auth_sessions"
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ip: Mapped[str] = mapped_column(String(64), default="")


class OneTimeToken(Base):
    """Single-use, short-lived tokens: password reset / invitation links (later: Telegram link codes)."""
    __tablename__ = "one_time_tokens"
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    purpose: Mapped[str] = mapped_column(String(32))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class LoginAttempt(Base):
    __tablename__ = "login_attempts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    key: Mapped[str] = mapped_column(String(320), index=True)  # "ip:<addr>" or "email:<addr>"
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class AuditEvent(Base):
    __tablename__ = "audit_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    actor_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    action: Mapped[str] = mapped_column(String(64), index=True)
    target: Mapped[str] = mapped_column(String(200), default="")
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    ip: Mapped[str] = mapped_column(String(64), default="")


class TelegramLink(Base):
    """A verified Telegram account (immutable numeric id) linked to one Lisa user."""
    __tablename__ = "telegram_links"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    telegram_user_id: Mapped[str] = mapped_column(String(32), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    telegram_name: Mapped[str] = mapped_column(String(200), default="")
    linked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class KnowledgeArea(Base):
    """A top-level folder of the company vault. Access comes only from ``area_grants``."""
    __tablename__ = "knowledge_areas"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))


class AreaGrant(Base):
    """principal_type: user | role | department. level: read < contribute < approve."""
    __tablename__ = "area_grants"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    area_key: Mapped[str] = mapped_column(ForeignKey("knowledge_areas.key", ondelete="CASCADE"), index=True)
    principal_type: Mapped[str] = mapped_column(String(16))
    principal_id: Mapped[str] = mapped_column(String(64))
    level: Mapped[str] = mapped_column(String(16))


class KnowledgeDraft(Base):
    __tablename__ = "knowledge_drafts"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    area_key: Mapped[str] = mapped_column(ForeignKey("knowledge_areas.key", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(300))
    body: Mapped[str] = mapped_column(Text)
    author_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending | approved | rejected
    reviewer_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    published_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class KnowledgeGap(Base):
    __tablename__ = "knowledge_gaps"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    question: Mapped[str] = mapped_column(Text)
    user_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class KnowledgeSource(Base):
    """A Git repository (e.g. the company's Obsidian vault) mirrored into knowledge areas.

    ``folders`` maps each top-level folder of the repository to an area key, or to None when the
    folder is left out; ``""`` stands for the notes at the repository root. Secrets (deploy key,
    access token) live on disk under the vault's ``.sources/<credential_id>``, never in the database."""
    __tablename__ = "knowledge_sources"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(200))
    url: Mapped[str] = mapped_column(String(500))
    branch: Mapped[str] = mapped_column(String(200), default="main")
    credential_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    folders: Mapped[dict] = mapped_column(JSON, default=dict)
    interval_minutes: Mapped[int] = mapped_column(Integer, default=15)
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_commit: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    notes: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


Index("ix_one_time_tokens_purpose_user", OneTimeToken.purpose, OneTimeToken.user_id)
class MailAccount(Base):
    """An e-mail account Lisa may use: a person's own (``owner_id``) or a shared mailbox the
    administrator grants to roles or people (``owner_id`` None, access in ``mail_grants``).

    kind: imap (IMAP + SMTP), google (Gmail API, the person's own Google sign-in) or microsoft (the
    organisation's Microsoft 365 app, pinned to ``address``). ``allow`` caps what Lisa may do with it:
    read < draft < send; every send is still confirmed by the person in the chat. Passwords and tokens
    live on disk (``<knowledge dir>/.mail/<id>.json``), never in the database."""
    __tablename__ = "mail_accounts"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    owner_id: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True)
    kind: Mapped[str] = mapped_column(String(16))
    address: Mapped[str] = mapped_column(String(320))
    label: Mapped[str] = mapped_column(String(60))
    display_name: Mapped[str] = mapped_column(String(200), default="")
    settings: Mapped[dict] = mapped_column(JSON, default=dict)
    allow: Mapped[str] = mapped_column(String(8), default="draft")
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class MailGrant(Base):
    """Who may use a shared mailbox. principal_type: user | role. level: read < draft < send."""
    __tablename__ = "mail_grants"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[str] = mapped_column(ForeignKey("mail_accounts.id", ondelete="CASCADE"), index=True)
    principal_type: Mapped[str] = mapped_column(String(16))
    principal_id: Mapped[str] = mapped_column(String(64))
    level: Mapped[str] = mapped_column(String(8))


__all__ = ["Base", "Organization", "Department", "Role", "RolePermission", "RoleTemplate", "User",
           "UserRole", "AuthSession", "OneTimeToken", "LoginAttempt", "AuditEvent", "TelegramLink", "KnowledgeArea", "AreaGrant", "KnowledgeDraft", "KnowledgeGap", "KnowledgeSource", "MailAccount", "MailGrant",
           "utcnow"]
