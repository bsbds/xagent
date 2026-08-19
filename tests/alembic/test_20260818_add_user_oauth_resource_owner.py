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


def test_sqlite_upgrade_rejects_owner_index_name_collision_before_table_rebuild(
    tmp_path,
) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'oauth-index-collision.db'}")
    migration = _migration_module()

    with engine.begin() as connection:
        _create_old_table(connection)
        connection.execute(
            text(f"CREATE INDEX {ORDINARY_INDEX} ON user_oauth (user_id)")
        )

        with patch.object(migration, "op", _operations(connection)):
            with pytest.raises(RuntimeError, match="already exist"):
                migration.upgrade()

        constraints = {
            constraint["name"]
            for constraint in inspect(connection).get_unique_constraints("user_oauth")
        }
        assert OLD_CONSTRAINT in constraints
        assert "resource_owner_key" not in {
            column["name"] for column in inspect(connection).get_columns("user_oauth")
        }


def test_sqlite_upgrade_rejects_cross_table_index_name_collision_before_rebuild(
    tmp_path,
) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'oauth-global-collision.db'}")
    migration = _migration_module()

    with engine.begin() as connection:
        _create_old_table(connection)
        connection.execute(text("CREATE TABLE unrelated (id INTEGER PRIMARY KEY)"))
        connection.execute(text(f"CREATE INDEX {ACTOR_INDEX} ON unrelated (id)"))

        with patch.object(migration, "op", _operations(connection)):
            with pytest.raises(RuntimeError, match="already exist"):
                migration.upgrade()

        constraints = {
            constraint["name"]
            for constraint in inspect(connection).get_unique_constraints("user_oauth")
        }
        assert OLD_CONSTRAINT in constraints
        assert "resource_owner_key" not in {
            column["name"] for column in inspect(connection).get_columns("user_oauth")
        }


def test_existing_owner_aware_schema_requires_semantic_index_definitions(
    tmp_path,
) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'oauth-current-drift.db'}")
    migration = _migration_module()

    with engine.begin() as connection:
        _operations(connection).create_table(
            "user_oauth",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("provider", sa.String(50), nullable=False),
            sa.Column("access_token", sa.String(), nullable=False),
            sa.Column("provider_user_id", sa.String(), nullable=True),
            sa.Column("resource_owner_key", sa.String(512), nullable=True),
            sa.PrimaryKeyConstraint("id"),
        )
        connection.execute(
            text(f"CREATE INDEX {ORDINARY_INDEX} ON user_oauth (user_id)")
        )
        connection.execute(
            text(
                f"CREATE UNIQUE INDEX {ACTOR_INDEX} ON user_oauth "
                "(user_id, resource_owner_key, provider, provider_user_id) "
                "WHERE resource_owner_key IS NOT NULL"
            )
        )
        connection.execute(
            text(
                f"CREATE INDEX {LOOKUP_INDEX} ON user_oauth "
                "(user_id, resource_owner_key, provider)"
            )
        )

        with patch.object(migration, "op", _operations(connection)):
            with pytest.raises(RuntimeError, match="incorrect indexes"):
                migration.upgrade()


def test_postgresql_upgrade_creates_indexes_transactionally_before_old_constraint_drop() -> (
    None
):
    migration = _migration_module()
    events: list[str] = []
    fake_op = SimpleNamespace(
        get_bind=lambda: SimpleNamespace(dialect=SimpleNamespace(name="postgresql")),
        add_column=lambda *_args, **_kwargs: events.append("add-column"),
        drop_constraint=lambda *_args, **_kwargs: events.append("drop-constraint"),
    )

    with (
        patch.object(migration, "op", fake_op),
        patch.object(migration, "_table_exists", return_value=True),
        patch.object(migration, "_column_names", return_value=set()),
        patch.object(migration, "_constraint_names", return_value={OLD_CONSTRAINT}),
        patch.object(
            migration,
            "_create_owner_indexes",
            side_effect=lambda: events.append("create-indexes"),
        ),
    ):
        migration.upgrade()

    assert events == ["add-column", "create-indexes", "drop-constraint"]


def test_postgresql_owner_index_creation_does_not_accept_existing_names() -> None:
    migration = _migration_module()
    created: list[str] = []
    fake_op = SimpleNamespace(
        get_bind=lambda: SimpleNamespace(dialect=SimpleNamespace(name="postgresql")),
        create_index=lambda name, *_args, **_kwargs: created.append(name),
    )

    with (
        patch.object(migration, "op", fake_op),
        patch.object(
            migration,
            "_index_names",
            return_value={ORDINARY_INDEX, ACTOR_INDEX, LOOKUP_INDEX},
        ),
    ):
        migration._create_owner_indexes()

    assert created == [ORDINARY_INDEX, ACTOR_INDEX, LOOKUP_INDEX]


def test_postgresql_index_creation_failure_keeps_old_constraint() -> None:
    migration = _migration_module()
    events: list[str] = []
    fake_op = SimpleNamespace(
        get_bind=lambda: SimpleNamespace(dialect=SimpleNamespace(name="postgresql")),
        add_column=lambda *_args, **_kwargs: events.append("add-column"),
        drop_constraint=lambda *_args, **_kwargs: events.append("drop-constraint"),
    )

    with (
        patch.object(migration, "op", fake_op),
        patch.object(migration, "_table_exists", return_value=True),
        patch.object(migration, "_column_names", return_value=set()),
        patch.object(migration, "_constraint_names", return_value={OLD_CONSTRAINT}),
        patch.object(
            migration,
            "_create_owner_indexes",
            side_effect=RuntimeError("index creation failed"),
        ),
    ):
        with pytest.raises(RuntimeError, match="index creation failed"):
            migration.upgrade()

    assert events == ["add-column"]


def test_downgrade_rejects_unsupported_dialect_before_inspection() -> None:
    migration = _migration_module()
    fake_op = SimpleNamespace(
        get_bind=lambda: SimpleNamespace(dialect=SimpleNamespace(name="mysql"))
    )

    with patch.object(migration, "op", fake_op):
        with pytest.raises(RuntimeError, match="partial unique indexes"):
            migration.downgrade()
