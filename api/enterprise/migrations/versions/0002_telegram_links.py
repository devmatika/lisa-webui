"""Telegram links: verified numeric Telegram id -> Lisa user (one active link per id and per user).

Revision ID: 0002_telegram_links
Revises: 0001_identity
"""
import sqlalchemy as sa
from alembic import op

revision = "0002_telegram_links"
down_revision = "0001_identity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "telegram_links",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("telegram_user_id", sa.String(32), nullable=False),
        sa.Column("user_id", sa.String(32), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("telegram_name", sa.String(200), nullable=False),
        sa.Column("linked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_telegram_links_telegram_user_id", "telegram_links", ["telegram_user_id"])
    op.create_index("ix_telegram_links_user_id", "telegram_links", ["user_id"])
    # At most one ACTIVE link per Telegram account and per user (revoked rows are history).
    op.create_index("uq_telegram_links_active_tg", "telegram_links", ["telegram_user_id"], unique=True,
                    postgresql_where=sa.text("revoked_at IS NULL"), sqlite_where=sa.text("revoked_at IS NULL"))
    op.create_index("uq_telegram_links_active_user", "telegram_links", ["user_id"], unique=True,
                    postgresql_where=sa.text("revoked_at IS NULL"), sqlite_where=sa.text("revoked_at IS NULL"))


def downgrade() -> None:
    op.drop_table("telegram_links")
