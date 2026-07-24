"""Add changes (per-project change-log; UI-DELTA-3)

Revision ID: g1changes
Revises: f0mergeheads
Create Date: 2026-07-24

Lands the ``changes`` table backing the UI-DELTA incremental-loading change-log.
Every UI-relevant mutation writes one entry inside the SAME transaction as the
mutation (so the entity write and its change entry are all-or-nothing). ``seq`` is
a per-project monotonic cursor allocated by the atomic ``counters`` upsert under
namespace ``changelog`` (never read-max-plus-one); UNIQUE(project_id, seq) is the
belt-and-braces backstop and the (project_id, seq) index serves the ascending
delta query. Mirrors the DynamoDB change item (PK=P#<slug>, SK=CHANGE#<padded
seq>) + GSI7 on the other backend. Additive and inert — nothing reads it yet.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "g1changes"
down_revision = "f0mergeheads"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "changes",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("seq", sa.BigInteger(), nullable=False),
        sa.Column("entity_type", sa.Text(), nullable=False),
        sa.Column("entity_pubid", sa.Text(), nullable=False),
        sa.Column("op", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=True),
        sa.Column("occurred_at", sa.DateTime(), nullable=False),
        sa.Column("snapshot", JSONB(), nullable=True),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_id", "seq", name="uq_change_project_seq"),
    )
    op.create_index("ix_changes_project_seq", "changes", ["project_id", "seq"])


def downgrade():
    op.drop_index("ix_changes_project_seq", table_name="changes")
    op.drop_table("changes")
