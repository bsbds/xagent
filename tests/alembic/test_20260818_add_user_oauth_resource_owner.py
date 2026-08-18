"""Tests for actor-aware builtin OAuth storage identity."""

import importlib.util
from pathlib import Path
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


def test_upgrade_without_user_oauth_table_is_a_noop(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'empty.db'}")
    migration = _migration_module()

    with engine.begin() as connection:
        with patch.object(migration, "op", _operations(connection)):
            migration.upgrade()
            migration.downgrade()

        assert "user_oauth" not in inspect(connection).get_table_names()
