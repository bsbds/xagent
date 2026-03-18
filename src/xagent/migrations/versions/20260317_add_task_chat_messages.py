from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260317_add_task_chat_messages"
down_revision: Union[str, None] = "44a6d3a54c35"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "task_chat_messages",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("task_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column(
            "message_type",
            sa.String(length=64),
            nullable=False,
            server_default="message",
        ),
        sa.Column("interactions", sa.JSON(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_task_chat_messages_id"), "task_chat_messages", ["id"], unique=False
    )
    op.create_index(
        op.f("ix_task_chat_messages_task_id"),
        "task_chat_messages",
        ["task_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_task_chat_messages_user_id"),
        "task_chat_messages",
        ["user_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_task_chat_messages_user_id"), table_name="task_chat_messages"
    )
    op.drop_index(
        op.f("ix_task_chat_messages_task_id"), table_name="task_chat_messages"
    )
    op.drop_index(op.f("ix_task_chat_messages_id"), table_name="task_chat_messages")
    op.drop_table("task_chat_messages")
