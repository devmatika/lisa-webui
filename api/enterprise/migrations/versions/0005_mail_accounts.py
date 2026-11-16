"""Mail accounts: several e-mail accounts per person, and shared mailboxes granted by the admin.

Revision ID: 0005_mail_accounts
Revises: 0004_knowledge_sources
"""
import sqlalchemy as sa
from alembic import op

revision = "0005_mail_accounts"
down_revision = "0004_knowledge_sources"
branch_labels = None
depends_on = None
TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "mail_accounts",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("owner_id", sa.String(32), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=True),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("address", sa.String(320), nullable=False),
        sa.Column("label", sa.String(60), nullable=False),
        sa.Column("display_name", sa.String(200), nullable=False),
        sa.Column("settings", sa.JSON, nullable=False),
        sa.Column("allow", sa.String(8), nullable=False),
        sa.Column("is_default", sa.Boolean, nullable=False),
        sa.Column("last_error", sa.Text, nullable=True),
        sa.Column("last_checked_at", TS, nullable=True),
        sa.Column("created_at", TS, nullable=False),
    )
    op.create_index("ix_mail_accounts_owner_id", "mail_accounts", ["owner_id"])
    op.create_table(
        "mail_grants",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("account_id", sa.String(32), sa.ForeignKey("mail_accounts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("principal_type", sa.String(16), nullable=False),
        sa.Column("principal_id", sa.String(64), nullable=False),
        sa.Column("level", sa.String(8), nullable=False),
    )
    op.create_index("ix_mail_grants_account_id", "mail_grants", ["account_id"])


def downgrade() -> None:
    op.drop_table("mail_grants")
    op.drop_table("mail_accounts")
