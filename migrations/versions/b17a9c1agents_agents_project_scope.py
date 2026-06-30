"""Scope the agent registry to a project

Agents become per-project: drop the global UNIQUE(slug), add a NOT NULL
project_id FK, and a UNIQUE(project_id, slug). The registry holds only metadata
(tasks reference owners by slug string, not by FK), so the existing global rows
are cleared here and re-registered per project after the migration.

Revision ID: b17a9c1agents
Revises: 2d783ead6088
Create Date: 2026-06-30
"""
from alembic import op
import sqlalchemy as sa

revision = "b17a9c1agents"
down_revision = "2d783ead6088"
branch_labels = None
depends_on = None


def upgrade():
    # Registry is metadata only and FK-free on the inbound side — safe to clear.
    op.execute("DELETE FROM agents")
    op.drop_constraint("agents_slug_key", "agents", type_="unique")
    op.add_column("agents", sa.Column("project_id", sa.Integer(), nullable=False))
    op.create_foreign_key(
        "agents_project_id_fkey", "agents", "projects",
        ["project_id"], ["id"], ondelete="CASCADE",
    )
    op.create_index("ix_agents_project_id", "agents", ["project_id"])
    op.create_unique_constraint(
        "uq_agent_project_slug", "agents", ["project_id", "slug"]
    )


def downgrade():
    op.drop_constraint("uq_agent_project_slug", "agents", type_="unique")
    op.drop_index("ix_agents_project_id", table_name="agents")
    op.drop_constraint("agents_project_id_fkey", "agents", type_="foreignkey")
    op.drop_column("agents", "project_id")
    op.create_unique_constraint("agents_slug_key", "agents", ["slug"])
