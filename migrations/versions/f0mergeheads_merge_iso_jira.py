"""merge the ISO (project_members) and JIRA migration heads

Both ``e5f2members`` (ISO-1 project_members, this session) and ``e001jira`` →
``e002jira`` (the merged origin/main JIRA epic) branch from ``d4e1epicnotes``,
creating two Alembic heads. This is an empty merge revision that unifies them so
``alembic upgrade head`` resolves to a single head again. No schema change.

Revision ID: f0mergeheads
Revises: e5f2members, e002jira
"""
from __future__ import annotations

# revision identifiers, used by Alembic.
revision = "f0mergeheads"
down_revision = ("e5f2members", "e002jira")
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass
