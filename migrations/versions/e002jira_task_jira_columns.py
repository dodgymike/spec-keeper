"""Add jira_issue_key and jira_sync_error columns to tasks (reservation migration:2)

Revision ID: e002jira
Revises: e001jira
Create Date: 2026-07-02
"""
from alembic import op
import sqlalchemy as sa

revision = "e002jira"
down_revision = "e001jira"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("tasks", sa.Column("jira_issue_key", sa.Text(), nullable=True))
    op.add_column("tasks", sa.Column("jira_sync_error", sa.Text(), nullable=True))


def downgrade():
    op.drop_column("tasks", "jira_sync_error")
    op.drop_column("tasks", "jira_issue_key")
