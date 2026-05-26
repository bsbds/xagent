"""add user identities table for OIDC login

Revision ID: 20260526_add_user_identities
Revises: 20260521_merge_alembic_heads
Create Date: 2026-05-26 00:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260526_add_user_identities"
down_revision: Union[str, None] = "20260526_seed_builtin_microsoft_graph_mcp_apps"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    from alembic import context
    from sqlalchemy import inspect

    bind = context.get_bind()
    inspector = inspect(bind)
    existing_tables = inspector.get_table_names()

    if "user_identities" not in existing_tables:
        op.create_table(
            "user_identities",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("provider", sa.String(length=50), nullable=False),
            sa.Column("provider_subject", sa.String(length=255), nullable=False),
            sa.Column("email", sa.String(length=320), nullable=True),
            sa.Column("email_verified", sa.Boolean(), nullable=False),
            sa.Column("display_name", sa.String(length=255), nullable=True),
            sa.Column("picture_url", sa.String(length=1000), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.text("(CURRENT_TIMESTAMP)"),
                nullable=True,
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                server_default=sa.text("(CURRENT_TIMESTAMP)"),
                nullable=True,
            ),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "provider",
                "provider_subject",
                name="uq_user_identity_provider_subject",
            ),
        )

    indexes = {index["name"] for index in inspector.get_indexes("user_identities")}
    if "ix_user_identities_id" not in indexes:
        op.create_index(
            op.f("ix_user_identities_id"),
            "user_identities",
            ["id"],
            unique=False,
        )
    if "ix_user_identities_provider" not in indexes:
        op.create_index(
            op.f("ix_user_identities_provider"),
            "user_identities",
            ["provider"],
            unique=False,
        )
    if "ix_user_identities_user_id" not in indexes:
        op.create_index(
            "ix_user_identities_user_id",
            "user_identities",
            ["user_id"],
            unique=False,
        )
    if "ix_user_identities_provider_subject" not in indexes:
        op.create_index(
            "ix_user_identities_provider_subject",
            "user_identities",
            ["provider", "provider_subject"],
            unique=False,
        )


def downgrade() -> None:
    from alembic import context
    from sqlalchemy import inspect

    bind = context.get_bind()
    inspector = inspect(bind)
    if "user_identities" not in inspector.get_table_names():
        return

    indexes = {index["name"] for index in inspector.get_indexes("user_identities")}
    for index_name in (
        "ix_user_identities_provider_subject",
        "ix_user_identities_user_id",
        "ix_user_identities_provider",
        "ix_user_identities_id",
    ):
        if index_name in indexes:
            op.drop_index(index_name, table_name="user_identities")
    op.drop_table("user_identities")
