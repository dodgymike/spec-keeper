"""Add task_notes (timestamped comments on a task)

Revision ID: c3a7notes
Revises: b17a9c1agents
Create Date: 2026-06-30
"""
from alembic import op
import sqlalchemy as sa

revision = "c3a7notes"
down_revision = "b17a9c1agents"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "task_notes",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("task_id", sa.Integer(), nullable=False),
        sa.Column("author", sa.Text(), nullable=True),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_task_notes_task_id", "task_notes", ["task_id"])


def downgrade():
    op.drop_index("ix_task_notes_task_id", table_name="task_notes")
    op.drop_table("task_notes")
