"""Add jira_project_config table (reservation migration:1)

Revision ID: e001jira
Revises: d4e1epicnotes
Create Date: 2026-07-02
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "e001jira"
down_revision = "d4e1epicnotes"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "jira_project_config",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("base_url", sa.Text(), nullable=False),
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column("api_token_encrypted", sa.Text(), nullable=True),
        sa.Column("jira_project_key", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("cached_transitions", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_id", name="uq_jira_config_project"),
    )
    op.create_index("ix_jira_project_config_project_id", "jira_project_config", ["project_id"])


def downgrade():
    op.drop_index("ix_jira_project_config_project_id", table_name="jira_project_config")
    op.drop_table("jira_project_config")
