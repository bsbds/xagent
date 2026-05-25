"""add runtime kind to tasks

Revision ID: 20260525_add_task_runtime_kind
Revises: 20260521_merge_alembic_heads
Create Date: 2026-05-25 00:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision: str = "20260525_add_task_runtime_kind"
down_revision: Union[str, None] = "20260521_merge_alembic_heads"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if "tasks" not in inspector.get_table_names():
        return

    existing_columns = {col["name"] for col in inspector.get_columns("tasks")}
    if "runtime_kind" not in existing_columns:
        op.add_column(
            "tasks",
            sa.Column(
                "runtime_kind",
                sa.String(length=30),
                server_default="normal",
                nullable=True,
            ),
        )
    op.execute("UPDATE tasks SET runtime_kind = 'normal' WHERE runtime_kind IS NULL")

    existing_indexes = {idx["name"] for idx in inspector.get_indexes("tasks")}
    if "ix_tasks_runtime_kind" not in existing_indexes:
        op.create_index("ix_tasks_runtime_kind", "tasks", ["runtime_kind"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if "tasks" not in inspector.get_table_names():
        return

    existing_indexes = {idx["name"] for idx in inspector.get_indexes("tasks")}
    if "ix_tasks_runtime_kind" in existing_indexes:
        op.drop_index("ix_tasks_runtime_kind", table_name="tasks")

    existing_columns = {col["name"] for col in inspector.get_columns("tasks")}
    if "runtime_kind" in existing_columns:
        op.drop_column("tasks", "runtime_kind")
