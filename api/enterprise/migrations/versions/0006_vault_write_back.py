"""Write approved Teach-Lisa notes back to the company vault (opt-in per source).

Revision ID: 0006_vault_write_back
Revises: 0005_mail_accounts
"""
import sqlalchemy as sa
from alembic import op

revision = "0006_vault_write_back"
down_revision = "0005_mail_accounts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("knowledge_sources", sa.Column("write_back", sa.Boolean, nullable=False, server_default=sa.false()))
    op.add_column("knowledge_sources", sa.Column("push_error", sa.Text, nullable=True))
    op.add_column("knowledge_drafts", sa.Column("pushed_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("knowledge_drafts", "pushed_at")
    op.drop_column("knowledge_sources", "push_error")
    op.drop_column("knowledge_sources", "write_back")
