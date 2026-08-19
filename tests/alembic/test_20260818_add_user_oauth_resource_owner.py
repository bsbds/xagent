"""Tests for actor-aware builtin OAuth storage identity."""

import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

MIGRATION_NAME = "20260818_add_user_oauth_resource_owner.py"
ORDINARY_INDEX = "uq_user_oauth_ordinary_account"
ACTOR_INDEX = "uq_user_oauth_actor_account"
LOOKUP_INDEX = "ix_user_oauth_owner_provider"
OLD_CONSTRAINT = "uq_user_provider_account"


def _migration_module():
    migration_file = (
        Path(__file__).parent.parent.parent
        / "src/xagent/migrations/versions"
        / MIGRATION_NAME
    )
    spec = importlib.util.spec_from_file_location(
        "user_oauth_actor_migration", migration_file
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _operations(connection):
    return Operations(MigrationContext.configure(connection))


def _create_old_table(connection) -> None:
    _operations(connection).create_table(
        "user_oauth",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(50), nullable=False),
        sa.Column("access_token", sa.String(), nullable=False),
        sa.Column("provider_user_id", sa.String(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id",
            "provider",
            "provider_user_id",
            name=OLD_CONSTRAINT,
        ),
    )


def _index_map(connection) -> dict[str, dict]:
    return {
        index["name"]: index for index in inspect(connection).get_indexes("user_oauth")
    }


def _where(index: dict) -> str:
    options = index.get("dialect_options") or {}
    clause = options.get("sqlite_where")
    if clause is None:
        clause = options.get("postgresql_where")
    return str(clause if clause is not None else "").lower()


def test_upgrade_preserves_rows_and_installs_owner_aware_identity(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'oauth.db'}")
    migration = _migration_module()

    with engine.begin() as connection:
        _create_old_table(connection)
        connection.execute(
            text(
                "INSERT INTO user_oauth "
                "(id, user_id, provider, access_token, provider_user_id) "
                "VALUES (1, 7, 'gmail', 'ordinary', 'provider-account')"
            )
        )

        with patch.object(migration, "op", _operations(connection)):
            migration.upgrade()
            migration.upgrade()

        inspector = inspect(connection)
        columns = {
            column["name"]: column for column in inspector.get_columns("user_oauth")
        }
        assert columns["resource_owner_key"]["nullable"] is True
        assert columns["resource_owner_key"]["type"].length == 512
        assert (
            connection.execute(
                text("SELECT resource_owner_key FROM user_oauth WHERE id = 1")
            ).scalar_one()
            is None
        )

        constraints = {
            constraint["name"]
            for constraint in inspector.get_unique_constraints("user_oauth")
        }
        assert OLD_CONSTRAINT not in constraints

        indexes = _index_map(connection)
        assert tuple(indexes[ORDINARY_INDEX]["column_names"]) == (
            "user_id",
            "provider",
            "provider_user_id",
        )
        assert indexes[ORDINARY_INDEX]["unique"] == 1
        assert "resource_owner_key is null" in _where(indexes[ORDINARY_INDEX])
        assert tuple(indexes[ACTOR_INDEX]["column_names"]) == (
            "user_id",
            "resource_owner_key",
            "provider",
            "provider_user_id",
        )
        assert indexes[ACTOR_INDEX]["unique"] == 1
        assert "resource_owner_key is not null" in _where(indexes[ACTOR_INDEX])
        assert tuple(indexes[LOOKUP_INDEX]["column_names"]) == (
            "user_id",
            "resource_owner_key",
            "provider",
        )
        assert indexes[LOOKUP_INDEX]["unique"] == 0

        connection.execute(
            text(
                "INSERT INTO user_oauth "
                "(user_id, provider, access_token, provider_user_id, resource_owner_key) "
                "VALUES "
                "(7, 'gmail', 'alice', 'provider-account', 'toby:slack:41:UALICE'), "
                "(7, 'gmail', 'bob', 'provider-account', 'toby:slack:41:UBOB')"
            )
        )
        with pytest.raises(IntegrityError):
            connection.execute(
                text(
                    "INSERT INTO user_oauth "
                    "(user_id, provider, access_token, provider_user_id) "
                    "VALUES (7, 'gmail', 'duplicate', 'provider-account')"
                )
            )
        with pytest.raises(IntegrityError):
            connection.execute(
                text(
                    "INSERT INTO user_oauth "
                    "(user_id, provider, access_token, provider_user_id, resource_owner_key) "
                    "VALUES (7, 'gmail', 'duplicate', 'provider-account', "
                    "'toby:slack:41:UALICE')"
                )
            )


def test_upgrade_preserves_nullable_provider_identity_semantics(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'oauth-null.db'}")
    migration = _migration_module()

    with engine.begin() as connection:
        _create_old_table(connection)
        with patch.object(migration, "op", _operations(connection)):
            migration.upgrade()

        connection.execute(
            text(
                "INSERT INTO user_oauth "
                "(user_id, provider, access_token, provider_user_id, resource_owner_key) "
                "VALUES "
                "(7, 'gmail', 'ordinary-1', NULL, NULL), "
                "(7, 'gmail', 'ordinary-2', NULL, NULL), "
                "(7, 'gmail', 'actor-1', NULL, 'toby:slack:41:UALICE'), "
                "(7, 'gmail', 'actor-2', NULL, 'toby:slack:41:UALICE')"
            )
        )

        assert (
            connection.execute(text("SELECT count(*) FROM user_oauth")).scalar_one()
            == 4
        )


def test_downgrade_restores_ordinary_schema_before_actor_rows_exist(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'oauth-down.db'}")
    migration = _migration_module()

    with engine.begin() as connection:
        _create_old_table(connection)
        connection.execute(
            text(
                "INSERT INTO user_oauth "
                "(id, user_id, provider, access_token, provider_user_id) "
                "VALUES (1, 7, 'gmail', 'ordinary', 'provider-account')"
            )
        )
        with patch.object(migration, "op", _operations(connection)):
            migration.upgrade()
            migration.downgrade()

        inspector = inspect(connection)
        columns = {column["name"] for column in inspector.get_columns("user_oauth")}
        assert "resource_owner_key" not in columns
        constraints = {
            constraint["name"]
            for constraint in inspector.get_unique_constraints("user_oauth")
        }
        assert OLD_CONSTRAINT in constraints
        indexes = _index_map(connection)
        assert ORDINARY_INDEX not in indexes
        assert ACTOR_INDEX not in indexes
        assert LOOKUP_INDEX not in indexes
        assert (
            connection.execute(
                text("SELECT access_token FROM user_oauth WHERE id = 1")
            ).scalar_one()
            == "ordinary"
        )


def test_downgrade_refuses_to_collapse_actor_owned_rows(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'oauth-refuse-down.db'}")
    migration = _migration_module()

    with engine.begin() as connection:
        _create_old_table(connection)
        with patch.object(migration, "op", _operations(connection)):
            migration.upgrade()
            connection.execute(
                text(
                    "INSERT INTO user_oauth "
                    "(user_id, provider, access_token, provider_user_id, resource_owner_key) "
                    "VALUES (7, 'gmail', 'alice', 'provider-account', "
                    "'toby:slack:41:UALICE')"
                )
            )
            with pytest.raises(RuntimeError, match="actor-owned UserOAuth"):
                migration.downgrade()

        assert "resource_owner_key" in {
            column["name"] for column in inspect(connection).get_columns("user_oauth")
        }


def test_upgrade_rejects_dialect_without_partial_unique_indexes_before_inspection() -> (
    None
):
    migration = _migration_module()
    fake_op = SimpleNamespace(
        get_bind=lambda: SimpleNamespace(dialect=SimpleNamespace(name="mysql"))
    )

    with patch.object(migration, "op", fake_op):
        with pytest.raises(RuntimeError, match="partial unique indexes"):
            migration.upgrade()


def test_upgrade_without_user_oauth_table_is_a_noop(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'empty.db'}")
    migration = _migration_module()

    with engine.begin() as connection:
        with patch.object(migration, "op", _operations(connection)):
            migration.upgrade()
            migration.downgrade()

        assert "user_oauth" not in inspect(connection).get_table_names()


def _postgresql_index_row(
    *,
    valid: bool = True,
    unique: bool = True,
    columns: tuple[str, ...] = ("user_id", "provider", "provider_user_id"),
    predicate: str | None = "(resource_owner_key IS NULL)",
    method: str = "btree",
    key_count: int | None = None,
    attribute_count: int | None = None,
    options: list[str] | None = None,
    nulls_not_distinct: bool = False,
    tablespace_oid: int = 0,
) -> dict:
    return {
        "schema_name": "public",
        "index_name": ORDINARY_INDEX,
        "is_target_table": True,
        "is_valid": valid,
        "is_unique": unique,
        "access_method": method,
        "columns": list(columns),
        "predicate": predicate,
        "key_count": len(columns) if key_count is None else key_count,
        "attribute_count": (
            len(columns) if attribute_count is None else attribute_count
        ),
        "nulls_not_distinct": nulls_not_distinct,
        "is_primary": False,
        "is_exclusion": False,
        "tablespace_oid": tablespace_oid,
        "options": options,
    }


@pytest.mark.parametrize(
    ("override", "expected"),
    [
        ({}, True),
        ({"valid": False}, False),
        ({"unique": False}, False),
        ({"columns": ("provider", "user_id", "provider_user_id")}, False),
        ({"columns": ("user_id", "lower(provider)", "provider_user_id")}, False),
        ({"predicate": "resource_owner_key IS NOT NULL"}, False),
        ({"predicate": None}, False),
        ({"method": "hash"}, False),
        ({"key_count": 3, "attribute_count": 4}, False),
        ({"options": ["fillfactor=70"]}, False),
        ({"nulls_not_distinct": True}, False),
        ({"tablespace_oid": 42}, False),
    ],
)
def test_postgresql_exact_index_definition_rejects_every_wrong_shape(
    override: dict, expected: bool
) -> None:
    migration = _migration_module()

    assert (
        migration._postgresql_index_is_exact(
            _postgresql_index_row(**override),
            unique=True,
            columns=("user_id", "provider", "provider_user_id"),
            predicate="resource_owner_key IS NULL",
        )
        is expected
    )


def test_postgresql_predicate_normalization_is_exact_but_format_insensitive() -> None:
    migration = _migration_module()

    assert (
        migration._normalize_postgresql_predicate(
            ' (( "resource_owner_key"   IS   NULL )) '
        )
        == "resource_owner_key is null"
    )
    assert migration._normalize_postgresql_predicate(
        "resource_owner_key IS NOT NULL"
    ) != migration._normalize_postgresql_predicate("resource_owner_key IS NULL")


def test_downgrade_rejects_unsupported_dialect_before_inspection() -> None:
    migration = _migration_module()
    fake_op = SimpleNamespace(
        get_bind=lambda: SimpleNamespace(dialect=SimpleNamespace(name="mysql"))
    )

    with patch.object(migration, "op", fake_op):
        with pytest.raises(RuntimeError, match="partial unique indexes"):
            migration.downgrade()


def test_postgresql_catalog_inspection_requests_the_complete_index_shape() -> None:
    migration = _migration_module()
    executed: dict[str, object] = {}

    class _Result:
        def mappings(self):
            return self

        def first(self):
            return None

    def execute(statement, parameters):
        executed["sql"] = str(statement)
        executed["parameters"] = parameters
        return _Result()

    fake_op = SimpleNamespace(get_bind=lambda: SimpleNamespace(execute=execute))
    with patch.object(migration, "op", fake_op):
        assert migration._inspect_postgresql_index(ORDINARY_INDEX) is None

    sql = str(executed["sql"])
    for required_fragment in (
        "indisvalid",
        "indisunique",
        "indnullsnotdistinct",
        "indisprimary",
        "indisexclusion",
        "pg_get_indexdef",
        "pg_get_expr",
        "indnkeyatts",
        "indnatts",
        "reltablespace",
        "reloptions",
        "to_regclass",
    ):
        assert required_fragment in sql
    assert executed["parameters"] == {
        "index_name": ORDINARY_INDEX,
        "table_name": "user_oauth",
    }


def test_postgresql_repair_drops_wrong_index_then_recreates_before_validation() -> None:
    migration = _migration_module()
    statements: list[str] = []

    class _Autocommit:
        def __enter__(self):
            return None

        def __exit__(self, *_args):
            return None

    fake_op = SimpleNamespace(
        get_context=lambda: SimpleNamespace(autocommit_block=lambda: _Autocommit()),
        execute=lambda statement: statements.append(str(statement)),
    )
    ordinary_exact = _postgresql_index_row()
    actor_exact = _postgresql_index_row(
        columns=("user_id", "resource_owner_key", "provider", "provider_user_id"),
        predicate="resource_owner_key IS NOT NULL",
    )
    lookup_exact = _postgresql_index_row(
        unique=False,
        columns=("user_id", "resource_owner_key", "provider"),
        predicate=None,
    )
    inspections = iter(
        [
            _postgresql_index_row(unique=False),
            None,
            lookup_exact,
            ordinary_exact,
            actor_exact,
            lookup_exact,
        ]
    )

    with (
        patch.object(migration, "op", fake_op),
        patch.object(
            migration,
            "_inspect_postgresql_index",
            side_effect=lambda _name: next(inspections),
        ),
    ):
        migration._create_postgresql_owner_indexes_concurrently()

    assert statements == [
        'DROP INDEX CONCURRENTLY "public"."uq_user_oauth_ordinary_account"',
        "CREATE UNIQUE INDEX CONCURRENTLY uq_user_oauth_ordinary_account "
        "ON user_oauth (user_id, provider, provider_user_id) "
        "WHERE resource_owner_key IS NULL",
        "CREATE UNIQUE INDEX CONCURRENTLY uq_user_oauth_actor_account "
        "ON user_oauth (user_id, resource_owner_key, provider, provider_user_id) "
        "WHERE resource_owner_key IS NOT NULL",
    ]


def test_postgresql_upgrade_never_drops_old_constraint_when_exact_validation_fails() -> (
    None
):
    migration = _migration_module()
    events: list[str] = []
    fake_op = SimpleNamespace(
        get_bind=lambda: SimpleNamespace(dialect=SimpleNamespace(name="postgresql")),
        drop_constraint=lambda *_args, **_kwargs: events.append("drop-constraint"),
    )

    with (
        patch.object(migration, "op", fake_op),
        patch.object(migration, "_table_exists", return_value=True),
        patch.object(migration, "_column_names", return_value={migration.OWNER_COLUMN}),
        patch.object(migration, "_constraint_names", return_value={OLD_CONSTRAINT}),
        patch.object(
            migration,
            "_create_postgresql_owner_indexes_concurrently",
            side_effect=RuntimeError("wrong definition"),
        ),
    ):
        with pytest.raises(RuntimeError, match="wrong definition"):
            migration.upgrade()

    assert events == []
