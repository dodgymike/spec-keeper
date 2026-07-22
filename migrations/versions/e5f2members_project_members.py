"""Add project_members (project membership; ISO-1, dormant)

Revision ID: e5f2members
Revises: d4e1epicnotes
Create Date: 2026-07-22

Adds the project-membership table backing the ISO-1 data model. The entity is
DORMANT: no route reads it and nothing enforces authorization from it yet, so
this migration only lands the table (+ unique key and principal index) that the
``ProjectMember`` model and the Postgres storage adapter address. Mirrors the
DynamoDB member item (PK=P#<slug>, SK=MEMBER#<sub>) + GSI6 on the other backend.
"""
from alembic import op
import sqlalchemy as sa

revision = "e5f2members"
down_revision = "d4e1epicnotes"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "project_members",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("principal_sub", sa.Text(), nullable=False),
        sa.Column("principal_name", sa.Text(), nullable=True),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "project_id", "principal_sub", name="uq_member_project_principal"
        ),
    )
    op.create_index(
        "ix_project_members_project_id", "project_members", ["project_id"]
    )
    op.create_index("ix_member_principal", "project_members", ["principal_sub"])


def downgrade():
    op.drop_index("ix_member_principal", table_name="project_members")
    op.drop_index("ix_project_members_project_id", table_name="project_members")
    op.drop_table("project_members")
