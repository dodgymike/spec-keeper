"""Add epic_notes (notes on an epic)

Revision ID: d4e1epicnotes
Revises: c3a7notes
Create Date: 2026-06-30
"""
from alembic import op
import sqlalchemy as sa

revision = "d4e1epicnotes"
down_revision = "c3a7notes"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "epic_notes",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("epic_id", sa.Integer(), nullable=False),
        sa.Column("author", sa.Text(), nullable=True),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["epic_id"], ["epics.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_epic_notes_epic_id", "epic_notes", ["epic_id"])


def downgrade():
    op.drop_index("ix_epic_notes_epic_id", table_name="epic_notes")
    op.drop_table("epic_notes")
