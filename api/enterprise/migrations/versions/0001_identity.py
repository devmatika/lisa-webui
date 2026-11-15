"""Identity: organization, departments, roles, permissions, templates, users, sessions, audit.

Revision ID: 0001_identity
Revises:
"""
import sqlalchemy as sa
from alembic import op

revision = "0001_identity"
down_revision = None
branch_labels = None
depends_on = None

TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "organization",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("settings", sa.JSON, nullable=False),
        sa.Column("created_at", TS, nullable=False),
    )
    op.create_table(
        "departments",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False, unique=True),
    )
    op.create_table(
        "roles",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("key", sa.String(64), nullable=False, unique=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("builtin", sa.Boolean, nullable=False),
    )
    op.create_table(
        "role_permissions",
        sa.Column("role_id", sa.String(32), sa.ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("permission", sa.String(100), primary_key=True),
    )
    op.create_table(
        "role_templates",
        sa.Column("role_id", sa.String(32), sa.ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("toolsets", sa.JSON, nullable=False),
        sa.Column("plugins", sa.JSON, nullable=False),
        sa.Column("skills", sa.JSON, nullable=False),
    )
    op.create_table(
        "users",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("email", sa.String(320), nullable=False, unique=True),
        sa.Column("display_name", sa.String(200), nullable=False),
        sa.Column("password_hash", sa.Text, nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("department_id", sa.String(32), sa.ForeignKey("departments.id", ondelete="SET NULL"),
                  nullable=True),
        sa.Column("hermes_profile", sa.String(64), nullable=True, unique=True),
        sa.Column("oidc_subject", sa.String(255), nullable=True, unique=True),
        sa.Column("created_at", TS, nullable=False),
    )
    op.create_table(
        "user_roles",
        sa.Column("user_id", sa.String(32), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("role_id", sa.String(32), sa.ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True),
    )
    op.create_table(
        "auth_sessions",
        sa.Column("token_hash", sa.String(64), primary_key=True),
        sa.Column("user_id", sa.String(32), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("expires_at", TS, nullable=False),
        sa.Column("revoked_at", TS, nullable=True),
        sa.Column("ip", sa.String(64), nullable=False),
    )
    op.create_index("ix_auth_sessions_user_id", "auth_sessions", ["user_id"])
    op.create_table(
        "one_time_tokens",
        sa.Column("token_hash", sa.String(64), primary_key=True),
        sa.Column("purpose", sa.String(32), nullable=False),
        sa.Column("user_id", sa.String(32), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("expires_at", TS, nullable=False),
        sa.Column("used_at", TS, nullable=True),
    )
    op.create_index("ix_one_time_tokens_user_id", "one_time_tokens", ["user_id"])
    op.create_index("ix_one_time_tokens_purpose_user", "one_time_tokens", ["purpose", "user_id"])
    op.create_table(
        "login_attempts",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("key", sa.String(320), nullable=False),
        sa.Column("at", TS, nullable=False),
    )
    op.create_index("ix_login_attempts_key", "login_attempts", ["key"])
    op.create_index("ix_login_attempts_at", "login_attempts", ["at"])
    op.create_table(
        "audit_events",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("at", TS, nullable=False),
        sa.Column("actor_id", sa.String(32), nullable=True),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("target", sa.String(200), nullable=False),
        sa.Column("detail", sa.JSON, nullable=False),
        sa.Column("ip", sa.String(64), nullable=False),
    )
    op.create_index("ix_audit_events_at", "audit_events", ["at"])
    op.create_index("ix_audit_events_actor_id", "audit_events", ["actor_id"])
    op.create_index("ix_audit_events_action", "audit_events", ["action"])


def downgrade() -> None:
    for table in ("audit_events", "login_attempts", "one_time_tokens", "auth_sessions", "user_roles", "users",
                  "role_templates", "role_permissions", "roles", "departments", "organization"):
        op.drop_table(table)
