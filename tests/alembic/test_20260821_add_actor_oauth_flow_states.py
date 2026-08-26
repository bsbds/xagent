"""Migration coverage for the minimal actor OAuth nonce table."""

from pathlib import Path

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from tests.shared.postgres_disposable import load_migration_module

MIGRATION_PATH = (
    Path(__file__).parent.parent.parent
    / "src/xagent/migrations/versions/20260821_add_actor_oauth_flow_states.py"
)
REVISION = "20260821_actor_oauth_flow_states"
DOWN_REVISION = "20260823_add_preferences_to_users"
TABLE = "actor_oauth_flow_states"


def _migration():
    return load_migration_module(MIGRATION_PATH, "actor_oauth_flow_states_migration")


def _run(connection, operation: str) -> None:
    context = MigrationContext.configure(connection)
    with Operations.context(context):
        getattr(_migration(), operation)()


def test_revision_metadata() -> None:
    migration = _migration()
    assert migration.revision == REVISION
    assert migration.down_revision == DOWN_REVISION


def test_sqlite_upgrade_accepts_current_metadata_table(tmp_path) -> None:
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'current.db'}")
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                """
                CREATE TABLE actor_oauth_flow_states (
                    nonce VARCHAR(64) PRIMARY KEY NOT NULL,
                    expires_at DATETIME NOT NULL
                )
                """
            )
        )

        _run(connection, "upgrade")

        assert sa.inspect(connection).has_table(TABLE)
    engine.dispose()


def test_sqlite_upgrade_and_downgrade_have_exact_minimal_shape(tmp_path) -> None:
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'migration.db'}")
    with engine.begin() as connection:
        _run(connection, "upgrade")
        inspector = sa.inspect(connection)
        assert inspector.has_table(TABLE)
        columns = {column["name"]: column for column in inspector.get_columns(TABLE)}
        assert set(columns) == {"nonce", "expires_at"}
        assert columns["nonce"]["primary_key"] == 1
        assert columns["nonce"]["nullable"] is False
        assert columns["expires_at"]["nullable"] is False
        _run(connection, "downgrade")
        assert not sa.inspect(connection).has_table(TABLE)
    engine.dispose()
