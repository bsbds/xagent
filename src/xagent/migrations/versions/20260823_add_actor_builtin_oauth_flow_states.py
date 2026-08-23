"""add browser-bound actor builtin oauth flow states

Revision ID: 20260823_actor_builtin_oauth_flow_state
Revises: 20260818_user_oauth_resource_owner
Create Date: 2026-08-23
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260823_actor_builtin_oauth_flow_state"
down_revision: str | None = "20260818_user_oauth_resource_owner"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "actor_builtin_oauth_flow_states"


def _existing_tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    """Create the single-use state ledger when it is absent."""
    existing_tables = _existing_tables()
    if TABLE in existing_tables:
        return
    constraints: list[sa.schema.SchemaItem] = [sa.PrimaryKeyConstraint("id")]
    if "users" in existing_tables:
        constraints.append(sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"))
    op.create_table(
        TABLE,
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("nonce", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("resource_owner_key", sa.String(length=512), nullable=False),
        sa.Column("provider", sa.String(length=50), nullable=False),
        sa.Column("app_id", sa.String(length=100), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
        ),
        *constraints,
    )
    op.create_index(
        "ix_actor_builtin_oauth_flow_states_nonce",
        TABLE,
        ["nonce"],
        unique=True,
    )
    op.create_index(
        "ix_actor_builtin_oauth_flow_states_user_id",
        TABLE,
        ["user_id"],
    )
    op.create_index(
        "ix_actor_builtin_oauth_flow_states_expires_at",
        TABLE,
        ["expires_at"],
    )


def downgrade() -> None:
    """Remove the state ledger without touching OAuth credentials."""
    if TABLE not in _existing_tables():
        return
    op.drop_index("ix_actor_builtin_oauth_flow_states_expires_at", table_name=TABLE)
    op.drop_index("ix_actor_builtin_oauth_flow_states_user_id", table_name=TABLE)
    op.drop_index("ix_actor_builtin_oauth_flow_states_nonce", table_name=TABLE)
    op.drop_table(TABLE)
