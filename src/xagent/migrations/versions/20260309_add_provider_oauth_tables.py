"""Add provider OAuth tables

Revision ID: 20260309_add_provider_oauth_tables
Revises: 44a6d3a54c35
Create Date: 2026-03-09

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260309_add_provider_oauth_tables"
down_revision: Union[str, None] = "44a6d3a54c35"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "user_provider_auths",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("provider_id", sa.String(length=100), nullable=False),
        sa.Column("access_token", sa.String(length=4096), nullable=True),
        sa.Column("refresh_token", sa.String(length=4096), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("account_id", sa.String(length=255), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=True,
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "provider_id", name="uq_user_provider_auth"),
    )
    op.create_index(
        op.f("ix_user_provider_auths_id"), "user_provider_auths", ["id"], unique=False
    )
    op.create_index(
        op.f("ix_user_provider_auths_user_id"),
        "user_provider_auths",
        ["user_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_user_provider_auths_provider_id"),
        "user_provider_auths",
        ["provider_id"],
        unique=False,
    )

    op.create_table(
        "oauth_states",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("provider_id", sa.String(length=100), nullable=False),
        sa.Column("state", sa.String(length=512), nullable=False),
        sa.Column("code_verifier", sa.String(length=512), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=True,
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("state", name="uq_oauth_state"),
    )
    op.create_index(op.f("ix_oauth_states_id"), "oauth_states", ["id"], unique=False)
    op.create_index(
        op.f("ix_oauth_states_user_id"), "oauth_states", ["user_id"], unique=False
    )
    op.create_index(
        op.f("ix_oauth_states_provider_id"),
        "oauth_states",
        ["provider_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_oauth_states_state"), "oauth_states", ["state"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_oauth_states_state"), table_name="oauth_states")
    op.drop_index(op.f("ix_oauth_states_provider_id"), table_name="oauth_states")
    op.drop_index(op.f("ix_oauth_states_user_id"), table_name="oauth_states")
    op.drop_index(op.f("ix_oauth_states_id"), table_name="oauth_states")
    op.drop_table("oauth_states")

    op.drop_index(
        op.f("ix_user_provider_auths_provider_id"), table_name="user_provider_auths"
    )
    op.drop_index(
        op.f("ix_user_provider_auths_user_id"), table_name="user_provider_auths"
    )
    op.drop_index(op.f("ix_user_provider_auths_id"), table_name="user_provider_auths")
    op.drop_table("user_provider_auths")
