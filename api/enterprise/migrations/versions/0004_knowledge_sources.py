"""Knowledge sources: Git repositories (e.g. the company Obsidian vault) mirrored into areas.

Revision ID: 0004_knowledge_sources
Revises: 0003_knowledge
"""
import sqlalchemy as sa
from alembic import op

revision = "0004_knowledge_sources"
down_revision = "0003_knowledge"
branch_labels = None
depends_on = None
TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "knowledge_sources",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("url", sa.String(500), nullable=False),
        sa.Column("branch", sa.String(200), nullable=False),
        sa.Column("credential_id", sa.String(32), nullable=True),
        sa.Column("folders", sa.JSON, nullable=False),
        sa.Column("interval_minutes", sa.Integer, nullable=False),
        sa.Column("last_sync_at", TS, nullable=True),
        sa.Column("last_commit", sa.String(64), nullable=True),
        sa.Column("last_error", sa.Text, nullable=True),
        sa.Column("notes", sa.Integer, nullable=False),
        sa.Column("created_at", TS, nullable=False),
    )


def downgrade() -> None:
    op.drop_table("knowledge_sources")
