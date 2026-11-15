"""Knowledge: areas (vault folders), per-area grants, drafts awaiting approval, knowledge gaps.

Revision ID: 0003_knowledge
Revises: 0002_telegram_links
"""
import sqlalchemy as sa
from alembic import op

revision = "0003_knowledge"
down_revision = "0002_telegram_links"
branch_labels = None
depends_on = None
TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table("knowledge_areas", sa.Column("key", sa.String(64), primary_key=True),
                    sa.Column("name", sa.String(200), nullable=False))
    op.create_table(
        "area_grants",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("area_key", sa.String(64), sa.ForeignKey("knowledge_areas.key", ondelete="CASCADE"), nullable=False),
        sa.Column("principal_type", sa.String(16), nullable=False),
        sa.Column("principal_id", sa.String(64), nullable=False),
        sa.Column("level", sa.String(16), nullable=False),
        sa.UniqueConstraint("area_key", "principal_type", "principal_id", name="uq_area_grant_principal"),
    )
    op.create_index("ix_area_grants_area_key", "area_grants", ["area_key"])
    op.create_table(
        "knowledge_drafts",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("area_key", sa.String(64), sa.ForeignKey("knowledge_areas.key", ondelete="CASCADE"), nullable=False),
        sa.Column("title", sa.String(300), nullable=False),
        sa.Column("body", sa.Text, nullable=False),
        sa.Column("author_id", sa.String(32), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("reviewer_id", sa.String(32), nullable=True),
        sa.Column("published_path", sa.String(500), nullable=True),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("decided_at", TS, nullable=True),
    )
    op.create_index("ix_knowledge_drafts_area_key", "knowledge_drafts", ["area_key"])
    op.create_table(
        "knowledge_gaps",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("question", sa.Text, nullable=False),
        sa.Column("user_id", sa.String(32), nullable=True),
        sa.Column("at", TS, nullable=False),
    )
    op.create_index("ix_knowledge_gaps_at", "knowledge_gaps", ["at"])


def downgrade() -> None:
    for table in ("knowledge_gaps", "knowledge_drafts", "area_grants", "knowledge_areas"):
        op.drop_table(table)
