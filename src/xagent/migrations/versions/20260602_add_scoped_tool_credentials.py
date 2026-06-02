"""add scoped tool credentials

Revision ID: 20260602_add_scoped_tool_credentials
Revises: 20260529_merge_email_reset_and_agent_origin_heads
Create Date: 2026-06-02 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy import and_, inspect

revision: str = "20260602_add_scoped_tool_credentials"
down_revision: str | None = "20260529_merge_email_reset_and_agent_origin_heads"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    tables = inspector.get_table_names()
    if "scoped_tool_credentials" not in tables:
        op.create_table(
            "scoped_tool_credentials",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("scope_type", sa.String(length=20), nullable=False),
            sa.Column("scope_id", sa.Integer(), nullable=True),
            sa.Column("tool_name", sa.String(length=100), nullable=False),
            sa.Column("field_name", sa.String(length=100), nullable=False),
            sa.Column("encrypted_value", sa.Text(), nullable=False),
            sa.Column("masked_value", sa.String(length=500), nullable=False),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
            ),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
            sa.PrimaryKeyConstraint("id"),
        )

    indexes = {idx["name"] for idx in inspector.get_indexes("scoped_tool_credentials")}
    for index_name, column_name in (
        ("ix_scoped_tool_credentials_id", "id"),
        ("ix_scoped_tool_credentials_scope_type", "scope_type"),
        ("ix_scoped_tool_credentials_scope_id", "scope_id"),
        ("ix_scoped_tool_credentials_tool_name", "tool_name"),
        ("ix_scoped_tool_credentials_field_name", "field_name"),
    ):
        if index_name not in indexes:
            op.create_index(index_name, "scoped_tool_credentials", [column_name])

    if "uq_scoped_tool_credential_scoped" not in indexes:
        op.create_index(
            "uq_scoped_tool_credential_scoped",
            "scoped_tool_credentials",
            ["scope_type", "scope_id", "tool_name", "field_name"],
            unique=True,
            sqlite_where=sa.column("scope_id").is_not(None),
            postgresql_where=sa.column("scope_id").is_not(None),
        )
    if "uq_scoped_tool_credential_instance" not in indexes:
        op.create_index(
            "uq_scoped_tool_credential_instance",
            "scoped_tool_credentials",
            ["tool_name", "field_name"],
            unique=True,
            sqlite_where=and_(
                sa.column("scope_type") == "instance",
                sa.column("scope_id").is_(None),
            ),
            postgresql_where=and_(
                sa.column("scope_type") == "instance",
                sa.column("scope_id").is_(None),
            ),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if "scoped_tool_credentials" not in inspector.get_table_names():
        return

    indexes = {idx["name"] for idx in inspector.get_indexes("scoped_tool_credentials")}
    for index_name in (
        "uq_scoped_tool_credential_instance",
        "uq_scoped_tool_credential_scoped",
        "ix_scoped_tool_credentials_field_name",
        "ix_scoped_tool_credentials_tool_name",
        "ix_scoped_tool_credentials_scope_id",
        "ix_scoped_tool_credentials_scope_type",
        "ix_scoped_tool_credentials_id",
    ):
        if index_name in indexes:
            op.drop_index(index_name, table_name="scoped_tool_credentials")
    op.drop_table("scoped_tool_credentials")
