"""PostgreSQL race coverage for builtin OAuth visibility savepoints.

Requires ``XAGENT_TEST_POSTGRES_URL``. Each run uses a disposable schema and
never touches tables in the server's default search path.
"""

from __future__ import annotations

import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.orm import Session, sessionmaker

from xagent.web.mcp_apps import ensure_builtin_oauth_server_visibility_for_user
from xagent.web.models.database import Base
from xagent.web.models.mcp import MCPServer, UserMCPServer
from xagent.web.models.public_mcp import PublicMCPApp
from xagent.web.models.user import User

pytestmark = pytest.mark.postgresql


@pytest.fixture()
def engine():
    url = os.getenv("XAGENT_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("XAGENT_TEST_POSTGRES_URL is not set")
    schema = "builtin_oauth_visibility_" + uuid.uuid4().hex[:8]
    admin_engine = sa.create_engine(url)
    with admin_engine.begin() as connection:
        connection.execute(sa.text(f'CREATE SCHEMA "{schema}"'))
    admin_engine.dispose()

    isolated_engine = sa.create_engine(
        url,
        connect_args={"options": f"-csearch_path={schema}"},
    )
    try:
        Base.metadata.create_all(bind=isolated_engine)
        yield isolated_engine
    finally:
        isolated_engine.dispose()
        admin_engine = sa.create_engine(url)
        with admin_engine.begin() as connection:
            connection.execute(sa.text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin_engine.dispose()


def test_concurrent_visibility_insert_preserves_loser_transaction(engine) -> None:
    """The expected unique loser keeps unrelated caller work committable."""
    session_factory = sessionmaker(
        bind=engine,
        autoflush=False,
        autocommit=False,
    )
    with session_factory() as setup:
        machine = User(username="machine", password_hash="hash")
        app = PublicMCPApp(
            app_id="calendar",
            name="Calendar",
            transport="oauth",
            provider_name="custom",
            launch_config={"command": "calendar"},
            is_visible_in_connector=True,
        )
        server = MCPServer(
            name="Calendar",
            managed="external",
            transport="oauth",
            auth={"app_id": "calendar", "provider": "custom"},
        )
        setup.add_all([machine, app, server])
        setup.commit()
        machine_id = int(machine.id)
        server_id = int(server.id)

    barrier = threading.Barrier(2)

    def synchronize_visibility_flush(
        session: Session, _flush_context: object, _instances: object
    ) -> None:
        if any(isinstance(row, UserMCPServer) for row in session.new):
            barrier.wait(timeout=20)

    event.listen(session_factory.class_, "before_flush", synchronize_visibility_flush)

    def connect(worker: int) -> int:
        with session_factory() as db:
            # This row is caller-owned work. A session-wide rollback in the
            # unique-conflict path would silently remove it.
            db.add(User(username=f"sentinel-{worker}", password_hash="hash"))
            resolved = ensure_builtin_oauth_server_visibility_for_user(
                db,
                user_id=machine_id,
                app_id="calendar",
            )
            db.commit()
            return int(resolved.id)

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            resolved_ids = list(executor.map(connect, (1, 2)))
    finally:
        event.remove(
            session_factory.class_, "before_flush", synchronize_visibility_flush
        )

    assert resolved_ids == [server_id, server_id]
    with session_factory() as verify:
        assert (
            verify.query(UserMCPServer)
            .filter(
                UserMCPServer.user_id == machine_id,
                UserMCPServer.mcpserver_id == server_id,
            )
            .count()
            == 1
        )
        assert (
            verify.query(User)
            .filter(User.username.in_(["sentinel-1", "sentinel-2"]))
            .count()
            == 2
        )
