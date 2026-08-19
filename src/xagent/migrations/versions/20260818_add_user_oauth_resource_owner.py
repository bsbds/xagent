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
Revises: 20260818_seed_jira_mcp_app
Create Date: 2026-08-18
"""

import re
from typing import Any, Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260818_user_oauth_resource_owner"
down_revision: Union[str, None] = "20260818_seed_jira_mcp_app"
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
SUPPORTED_DIALECTS = frozenset({"sqlite", "postgresql"})


def _require_partial_unique_index_support() -> str:
    dialect = op.get_bind().dialect.name
    if dialect not in SUPPORTED_DIALECTS:
        raise RuntimeError(
            "actor-owned builtin OAuth requires partial unique indexes; "
            f"database dialect {dialect!r} cannot preserve this identity"
        )
    return dialect


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


def _normalize_postgresql_predicate(predicate: str | None) -> str | None:
    """Normalize PostgreSQL's formatting without weakening predicate equality.

    ``pg_get_expr`` is authoritative for the parsed predicate, but may add
    redundant outer parentheses, identifier quotes, and whitespace. Only those
    presentation differences are removed; casts, operators, and expression
    structure remain significant and therefore fail the exact-definition check.
    """
    if predicate is None:
        return None
    normalized = re.sub(r'"([a-z_][a-z0-9_]*)"', r"\1", predicate.strip().lower())
    normalized = re.sub(r"\s+", " ", normalized)
    while normalized.startswith("(") and normalized.endswith(")"):
        depth = 0
        encloses_whole_expression = True
        for position, character in enumerate(normalized):
            if character == "(":
                depth += 1
            elif character == ")":
                depth -= 1
                if depth == 0 and position != len(normalized) - 1:
                    encloses_whole_expression = False
                    break
        if not encloses_whole_expression or depth != 0:
            break
        normalized = normalized[1:-1].strip()
    return normalized


def _normalize_postgresql_index_part(part: str) -> str:
    """Normalize one simple index key while retaining modifiers/expressions."""
    return re.sub(r'"([a-z_][a-z0-9_]*)"', r"\1", part.strip().lower())


def _postgresql_index_is_exact(
    row: dict[str, Any] | None,
    *,
    unique: bool,
    columns: tuple[str, ...],
    predicate: str | None,
) -> bool:
    """Return whether one catalog row is the complete expected index shape."""
    if row is None:
        return False
    actual_columns = tuple(
        _normalize_postgresql_index_part(str(column))
        for column in row.get("columns") or ()
    )
    expected_columns = tuple(
        _normalize_postgresql_index_part(column) for column in columns
    )
    return (
        row.get("is_target_table") is True
        and row.get("is_valid") is True
        and row.get("is_unique") is unique
        and row.get("access_method") == "btree"
        and actual_columns == expected_columns
        and row.get("key_count") == len(columns)
        and row.get("attribute_count") == len(columns)
        and row.get("nulls_not_distinct") is False
        and row.get("is_primary") is False
        and row.get("is_exclusion") is False
        and row.get("tablespace_oid") == 0
        and not row.get("options")
        and _normalize_postgresql_predicate(row.get("predicate"))
        == _normalize_postgresql_predicate(predicate)
    )


def _inspect_postgresql_index(index_name: str) -> dict[str, Any] | None:
    """Inspect a same-schema index by parsed PostgreSQL catalog properties."""
    result = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT index_namespace.nspname AS schema_name, "
                "index_relation.relname AS index_name, "
                "index_catalog.indrelid = target_table.oid AS is_target_table, "
                "index_catalog.indisvalid AS is_valid, "
                "index_catalog.indisunique AS is_unique, "
                "index_catalog.indnullsnotdistinct AS nulls_not_distinct, "
                "index_catalog.indisprimary AS is_primary, "
                "index_catalog.indisexclusion AS is_exclusion, "
                "access_method.amname AS access_method, "
                "ARRAY(SELECT pg_get_indexdef(index_catalog.indexrelid, key_position, false) "
                "      FROM generate_series(1, index_catalog.indnkeyatts) key_position "
                "      ORDER BY key_position) AS columns, "
                "pg_get_expr(index_catalog.indpred, index_catalog.indrelid) AS predicate, "
                "index_catalog.indnkeyatts AS key_count, "
                "index_catalog.indnatts AS attribute_count, "
                "index_relation.reltablespace AS tablespace_oid, "
                "index_relation.reloptions AS options "
                "FROM pg_class target_table "
                "JOIN pg_class index_relation "
                "  ON index_relation.relnamespace = target_table.relnamespace "
                " AND index_relation.relname = :index_name "
                "JOIN pg_namespace index_namespace "
                "  ON index_namespace.oid = index_relation.relnamespace "
                "LEFT JOIN pg_index index_catalog "
                "  ON index_catalog.indexrelid = index_relation.oid "
                "LEFT JOIN pg_am access_method "
                "  ON access_method.oid = index_relation.relam "
                "WHERE target_table.oid = to_regclass(:table_name)"
            ),
            {"index_name": index_name, "table_name": TABLE},
        )
        .mappings()
        .first()
    )
    return dict(result) if result is not None else None


def _quote_postgresql_identifier(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _create_postgresql_owner_indexes_concurrently() -> None:
    """Repair all owner indexes concurrently, then validate their exact shape.

    PostgreSQL's ``IF NOT EXISTS`` checks only the object name, so it can accept
    an invalid, non-unique, reordered, expression-based, or wrong-predicate
    index. Catalog inspection happens before and after DDL. The caller retains
    the old unique constraint until this function returns successfully.
    """
    definitions = (
        (
            ORDINARY_INDEX,
            True,
            ("user_id", "provider", "provider_user_id"),
            f"{OWNER_COLUMN} IS NULL",
            f"CREATE UNIQUE INDEX CONCURRENTLY {ORDINARY_INDEX} "
            f"ON {TABLE} (user_id, provider, provider_user_id) "
            f"WHERE {OWNER_COLUMN} IS NULL",
        ),
        (
            ACTOR_INDEX,
            True,
            ("user_id", OWNER_COLUMN, "provider", "provider_user_id"),
            f"{OWNER_COLUMN} IS NOT NULL",
            f"CREATE UNIQUE INDEX CONCURRENTLY {ACTOR_INDEX} "
            f"ON {TABLE} (user_id, {OWNER_COLUMN}, provider, provider_user_id) "
            f"WHERE {OWNER_COLUMN} IS NOT NULL",
        ),
        (
            LOOKUP_INDEX,
            False,
            ("user_id", OWNER_COLUMN, "provider"),
            None,
            f"CREATE INDEX CONCURRENTLY {LOOKUP_INDEX} "
            f"ON {TABLE} (user_id, {OWNER_COLUMN}, provider)",
        ),
    )

    with op.get_context().autocommit_block():
        for index_name, unique, columns, predicate, create_statement in definitions:
            existing = _inspect_postgresql_index(index_name)
            if _postgresql_index_is_exact(
                existing,
                unique=unique,
                columns=columns,
                predicate=predicate,
            ):
                continue
            if existing is not None:
                if existing.get("is_target_table") is not True:
                    raise RuntimeError(
                        f"PostgreSQL relation {index_name!r} blocks owner index "
                        "creation but belongs to another table"
                    )
                qualified_name = (
                    f"{_quote_postgresql_identifier(str(existing['schema_name']))}."
                    f"{_quote_postgresql_identifier(index_name)}"
                )
                op.execute(sa.text(f"DROP INDEX CONCURRENTLY {qualified_name}"))
            op.execute(sa.text(create_statement))

    for index_name, unique, columns, predicate, _statement in definitions:
        inspected = _inspect_postgresql_index(index_name)
        if not _postgresql_index_is_exact(
            inspected,
            unique=unique,
            columns=columns,
            predicate=predicate,
        ):
            raise RuntimeError(
                f"owner-aware PostgreSQL index has wrong definition: {index_name}"
            )


def upgrade() -> None:
    dialect = _require_partial_unique_index_support()
    if not _table_exists():
        return

    columns = _column_names()
    constraints = _constraint_names()
    needs_column = OWNER_COLUMN not in columns
    has_old_constraint = OLD_CONSTRAINT in constraints

    if dialect == "sqlite":
        if needs_column or has_old_constraint:
            # SQLite cannot drop a named UNIQUE constraint directly. Batch mode
            # rebuilds the table once while preserving every credential row.
            with op.batch_alter_table(TABLE) as batch_op:
                if needs_column:
                    batch_op.add_column(
                        sa.Column(OWNER_COLUMN, sa.String(OWNER_LENGTH))
                    )
                if has_old_constraint:
                    batch_op.drop_constraint(OLD_CONSTRAINT, type_="unique")
    elif dialect == "postgresql":
        if needs_column:
            op.add_column(TABLE, sa.Column(OWNER_COLUMN, sa.String(OWNER_LENGTH)))
        # Keep the owner-blind constraint until all replacement indexes exist
        # and PostgreSQL reports them valid. This avoids an unprotected window
        # during concurrent index construction or repair.
        _create_postgresql_owner_indexes_concurrently()
        if has_old_constraint:
            op.drop_constraint(OLD_CONSTRAINT, TABLE, type_="unique")
        return
    else:  # pragma: no cover - rejected before schema inspection above
        raise AssertionError(f"unsupported dialect: {dialect}")

    _create_owner_indexes()


def downgrade() -> None:
    _require_partial_unique_index_support()
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
