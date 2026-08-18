"""add actor ownership to builtin OAuth credentials

Existing rows remain ordinary credentials because the new owner key is
nullable and has no server default. The previous owner-blind unique constraint
is replaced by two partial unique indexes:

* null owner: ``(user_id, provider, provider_user_id)``
* actor owner: ``(user_id, resource_owner_key, provider, provider_user_id)``

This preserves SQL's existing null behavior for ``provider_user_id``. The
migration performs no backfill and rewrites no credential values.

Deployment ordering is strict. Apply this migration only after old xagent
instances have stopped, and do not create actor-owned rows until every instance
runs owner-aware code. The nullable column itself is backward compatible, but
an old owner-blind reader could otherwise select an actor credential.

Downgrade is permitted only before actor-owned rows exist. Collapsing two actor
namespaces into the old identity can violate uniqueness and would destroy the
security boundary, so downgrade refuses rather than deleting or merging rows.

Revision ID: 20260818_user_oauth_resource_owner
Revises: 20260813_trace_json_columns_to_jsonb
Create Date: 2026-08-18
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260818_user_oauth_resource_owner"
down_revision: Union[str, None] = "20260813_trace_json_columns_to_jsonb"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = "user_oauth"
OWNER_COLUMN = "resource_owner_key"
OWNER_LENGTH = 512
OLD_CONSTRAINT = "uq_user_provider_account"
ORDINARY_INDEX = "uq_user_oauth_ordinary_account"
ACTOR_INDEX = "uq_user_oauth_actor_account"
LOOKUP_INDEX = "ix_user_oauth_owner_provider"
ORDINARY_WHERE = sa.text(f"{OWNER_COLUMN} IS NULL")
ACTOR_WHERE = sa.text(f"{OWNER_COLUMN} IS NOT NULL")


def _table_exists() -> bool:
    return sa.inspect(op.get_bind()).has_table(TABLE)


def _column_names() -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(TABLE)}


def _constraint_names() -> set[str]:
    return {
        constraint["name"]
        for constraint in sa.inspect(op.get_bind()).get_unique_constraints(TABLE)
        if constraint.get("name")
    }


def _index_names() -> set[str]:
    return {
        index["name"]
        for index in sa.inspect(op.get_bind()).get_indexes(TABLE)
        if index.get("name")
    }


def _create_owner_indexes() -> None:
    existing = _index_names()
    if ORDINARY_INDEX not in existing:
        op.create_index(
            ORDINARY_INDEX,
            TABLE,
            ["user_id", "provider", "provider_user_id"],
            unique=True,
            sqlite_where=ORDINARY_WHERE,
            postgresql_where=ORDINARY_WHERE,
        )
    if ACTOR_INDEX not in existing:
        op.create_index(
            ACTOR_INDEX,
            TABLE,
            ["user_id", OWNER_COLUMN, "provider", "provider_user_id"],
            unique=True,
            sqlite_where=ACTOR_WHERE,
            postgresql_where=ACTOR_WHERE,
        )
    if LOOKUP_INDEX not in existing:
        op.create_index(
            LOOKUP_INDEX,
            TABLE,
            ["user_id", OWNER_COLUMN, "provider"],
            unique=False,
        )


def upgrade() -> None:
    if not _table_exists():
        return

    columns = _column_names()
    constraints = _constraint_names()
    needs_column = OWNER_COLUMN not in columns
    has_old_constraint = OLD_CONSTRAINT in constraints
    dialect = op.get_bind().dialect.name

    if dialect == "sqlite" and (needs_column or has_old_constraint):
        # SQLite cannot drop a named UNIQUE constraint directly. Batch mode
        # rebuilds the table once while preserving every credential row.
        with op.batch_alter_table(TABLE) as batch_op:
            if needs_column:
                batch_op.add_column(sa.Column(OWNER_COLUMN, sa.String(OWNER_LENGTH)))
            if has_old_constraint:
                batch_op.drop_constraint(OLD_CONSTRAINT, type_="unique")
    else:
        if needs_column:
            op.add_column(TABLE, sa.Column(OWNER_COLUMN, sa.String(OWNER_LENGTH)))
        if has_old_constraint:
            op.drop_constraint(OLD_CONSTRAINT, TABLE, type_="unique")

    _create_owner_indexes()


def downgrade() -> None:
    if not _table_exists():
        return

    columns = _column_names()
    if OWNER_COLUMN in columns:
        actor_row = (
            op.get_bind()
            .execute(
                sa.text(
                    f"SELECT 1 FROM {TABLE} WHERE {OWNER_COLUMN} IS NOT NULL LIMIT 1"
                )
            )
            .first()
        )
        if actor_row is not None:
            raise RuntimeError(
                "cannot downgrade while actor-owned UserOAuth rows exist"
            )

    existing_indexes = _index_names()
    for index_name in (LOOKUP_INDEX, ACTOR_INDEX, ORDINARY_INDEX):
        if index_name in existing_indexes:
            op.drop_index(index_name, table_name=TABLE)

    has_old_constraint = OLD_CONSTRAINT in _constraint_names()
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite" and (OWNER_COLUMN in columns or not has_old_constraint):
        with op.batch_alter_table(TABLE) as batch_op:
            if not has_old_constraint:
                batch_op.create_unique_constraint(
                    OLD_CONSTRAINT,
                    ["user_id", "provider", "provider_user_id"],
                )
            if OWNER_COLUMN in columns:
                batch_op.drop_column(OWNER_COLUMN)
    else:
        if not has_old_constraint:
            op.create_unique_constraint(
                OLD_CONSTRAINT,
                TABLE,
                ["user_id", "provider", "provider_user_id"],
            )
        if OWNER_COLUMN in columns:
            op.drop_column(TABLE, OWNER_COLUMN)
