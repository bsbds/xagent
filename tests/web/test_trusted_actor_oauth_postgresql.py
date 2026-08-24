"""PostgreSQL release gate for concurrent trusted actor OAuth callbacks."""

from __future__ import annotations

import threading
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from http.cookies import SimpleCookie
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest
from sqlalchemy.orm import sessionmaker

from tests.shared.postgres_disposable import disposable_database_factory
from xagent.core.utils.encryption import encrypt_value
from xagent.web import mcp_apps
from xagent.web.api import auth as auth_api
from xagent.web.models.actor_oauth_flow import ActorOAuthFlowState
from xagent.web.models.database import Base
from xagent.web.models.mcp import MCPServer, UserMCPServer
from xagent.web.models.public_mcp import PublicMCPApp
from xagent.web.models.user import User
from xagent.web.models.user_oauth import UserOAuth

pytestmark = pytest.mark.postgresql

TEST_BUILTIN_APP_ID = "calendar"
TEST_BUILTIN_EXECUTION = {
    "name": "Google Calendar",
    "transport": "oauth",
    "provider_name": "custom",
    "oauth_scopes": [],
    "launch_config": {"command": "calendar"},
}


class _Response:
    status_code = 200

    def __init__(self, data: dict[str, object]) -> None:
        self._data = data

    def json(self) -> dict[str, object]:
        return self._data


def _provider() -> SimpleNamespace:
    return SimpleNamespace(
        client_id=encrypt_value("client-id"),
        client_secret=encrypt_value("client-secret"),
        auth_url="https://provider.example/authorize",
        token_url="https://provider.example/token",
        userinfo_url="",
        redirect_uri="https://xagent.example/api/auth/custom/callback",
        default_scopes=[],
        user_id_path="id",
        email_path="email",
    )


@pytest.fixture
def postgresql_engine(monkeypatch):
    registry_lookup = mcp_apps.get_builtin_execution_fields_and_optional_scopes

    def test_registry(app_id: str):
        if app_id == TEST_BUILTIN_APP_ID:
            return TEST_BUILTIN_EXECUTION, []
        if app_id == "drive":
            return {
                "name": "Drive",
                "transport": "oauth",
                "provider_name": "other",
                "oauth_scopes": [],
                "launch_config": {"command": "drive"},
            }, []
        return registry_lookup(app_id)

    monkeypatch.setattr(
        mcp_apps, "get_builtin_execution_fields_and_optional_scopes", test_registry
    )

    # The shared factory skips only when this explicit release-gate URL is absent.
    with disposable_database_factory("xagent_actor_oauth") as make:
        yield make("callback_claim")


def test_concurrent_callbacks_exchange_and_persist_only_once(
    postgresql_engine, monkeypatch
) -> None:
    tables = [
        User.__table__,
        PublicMCPApp.__table__,
        MCPServer.__table__,
        UserMCPServer.__table__,
        ActorOAuthFlowState.__table__,
        UserOAuth.__table__,
    ]
    Base.metadata.create_all(postgresql_engine, tables=tables)
    factory = sessionmaker(bind=postgresql_engine, autoflush=False, autocommit=False)
    with factory() as db:
        user = User(username="workspace-account", password_hash="hash")
        app = PublicMCPApp(
            app_id="calendar",
            name="Google Calendar",
            transport="oauth",
            provider_name="custom",
            launch_config={"command": "calendar"},
            is_visible_in_connector=True,
        )
        server = MCPServer(
            name="Google Calendar",
            managed="external",
            transport="oauth",
            auth={"app_id": "calendar", "provider": "custom"},
        )
        db.add_all([user, app, server])
        db.flush()
        db.add(
            UserMCPServer(
                user_id=user.id,
                mcpserver_id=server.id,
                is_owner=False,
                is_active=True,
            )
        )
        db.commit()
        start = auth_api.start_builtin_oauth_for_resource_owner(
            provider="custom",
            app_id="calendar",
            user=user,
            resource_owner_key="toby:slack:41:UALICE",
            db=db,
            db_provider=_provider(),
        )
        db.commit()

    state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]
    cookies = SimpleCookie()
    cookies.load(start.headers["set-cookie"])
    cookie_name, morsel = next(iter(cookies.items()))
    request = SimpleNamespace(
        query_params={"state": state, "code": "provider-code"},
        cookies={cookie_name: morsel.value},
    )

    exchange_entered = threading.Event()
    release_exchange = threading.Event()
    exchange_count = 0
    exchange_lock = threading.Lock()

    def post(*_args, **_kwargs):
        nonlocal exchange_count
        with exchange_lock:
            exchange_count += 1
        exchange_entered.set()
        assert release_exchange.wait(timeout=10)
        return _Response({"access_token": "actor-token", "scope": "profile.read"})

    monkeypatch.setattr(auth_api.requests, "post", post)

    def callback() -> int:
        with factory() as callback_db:
            return auth_api.generic_oauth_callback(
                "custom", request, callback_db, _provider()
            ).status_code

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(callback) for _ in range(2)]
        assert exchange_entered.wait(timeout=10)
        completed, _pending = wait(futures, timeout=10, return_when=FIRST_COMPLETED)
        assert len(completed) == 1
        assert next(iter(completed)).result() == 400
        release_exchange.set()
        statuses = sorted(future.result(timeout=10) for future in futures)

    assert statuses == [200, 400]
    assert exchange_count == 1
    with factory() as db:
        assert db.query(ActorOAuthFlowState).count() == 0
        row = db.query(UserOAuth).one()
        assert row.resource_owner_key == "toby:slack:41:UALICE"
        assert row.access_token == "actor-token"


def test_disconnect_during_actor_exchange_rejects_credential(
    postgresql_engine, monkeypatch
) -> None:
    tables = [
        User.__table__,
        PublicMCPApp.__table__,
        MCPServer.__table__,
        UserMCPServer.__table__,
        ActorOAuthFlowState.__table__,
        UserOAuth.__table__,
    ]
    Base.metadata.create_all(postgresql_engine, tables=tables)
    factory = sessionmaker(bind=postgresql_engine, autoflush=False, autocommit=False)
    with factory() as db:
        user = User(username="workspace-account", password_hash="hash")
        app = PublicMCPApp(
            app_id="calendar",
            name="Google Calendar",
            transport="oauth",
            provider_name="custom",
            launch_config={"command": "calendar"},
            is_visible_in_connector=True,
        )
        server = MCPServer(
            name="Google Calendar",
            managed="external",
            transport="oauth",
            auth={"app_id": "calendar", "provider": "custom"},
        )
        db.add_all([user, app, server])
        db.flush()
        db.add(
            UserMCPServer(
                user_id=user.id,
                mcpserver_id=server.id,
                is_owner=False,
                is_active=True,
            )
        )
        db.commit()
        user_id = int(user.id)
        server_id = int(server.id)
        start = auth_api.start_builtin_oauth_for_resource_owner(
            provider="custom",
            app_id="calendar",
            user=user,
            resource_owner_key="toby:slack:41:UALICE",
            db=db,
            db_provider=_provider(),
        )
        db.commit()

    state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]
    cookies = SimpleCookie()
    cookies.load(start.headers["set-cookie"])
    cookie_name, morsel = next(iter(cookies.items()))
    request = SimpleNamespace(
        query_params={"state": state, "code": "provider-code"},
        cookies={cookie_name: morsel.value},
    )

    exchange_entered = threading.Event()
    release_exchange = threading.Event()

    def post(*_args, **_kwargs):
        exchange_entered.set()
        assert release_exchange.wait(timeout=10)
        return _Response({"access_token": "actor-token", "scope": "profile.read"})

    monkeypatch.setattr(auth_api.requests, "post", post)

    def callback() -> int:
        with factory() as callback_db:
            return auth_api.generic_oauth_callback(
                "custom", request, callback_db, _provider()
            ).status_code

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(callback)
        try:
            assert exchange_entered.wait(timeout=10)
            with factory() as disconnect_db:
                link = (
                    disconnect_db.query(UserMCPServer)
                    .filter(
                        UserMCPServer.user_id == user_id,
                        UserMCPServer.mcpserver_id == server_id,
                    )
                    .one()
                )
                disconnect_db.delete(link)
                disconnect_db.commit()
        finally:
            release_exchange.set()
        assert future.result(timeout=10) == 400

    with factory() as db:
        assert (
            db.query(UserMCPServer)
            .filter(
                UserMCPServer.user_id == user_id,
                UserMCPServer.mcpserver_id == server_id,
            )
            .count()
            == 0
        )
        assert db.query(UserOAuth).count() == 0


def test_independent_actor_flows_replace_one_credential(
    postgresql_engine, monkeypatch
) -> None:
    tables = [
        User.__table__,
        PublicMCPApp.__table__,
        MCPServer.__table__,
        UserMCPServer.__table__,
        ActorOAuthFlowState.__table__,
        UserOAuth.__table__,
    ]
    Base.metadata.create_all(postgresql_engine, tables=tables)
    factory = sessionmaker(bind=postgresql_engine, autoflush=False, autocommit=False)
    with factory() as db:
        user = User(username="workspace-account", password_hash="hash")
        app = PublicMCPApp(
            app_id="calendar",
            name="Google Calendar",
            transport="oauth",
            provider_name="custom",
            launch_config={"command": "calendar"},
            is_visible_in_connector=True,
        )
        server = MCPServer(
            name="Google Calendar",
            managed="external",
            transport="oauth",
            auth={"app_id": "calendar", "provider": "custom"},
        )
        db.add_all([user, app, server])
        db.flush()
        db.add(
            UserMCPServer(
                user_id=user.id,
                mcpserver_id=server.id,
                is_owner=False,
                is_active=True,
            )
        )
        db.commit()
        starts = [
            auth_api.start_builtin_oauth_for_resource_owner(
                provider="custom",
                app_id="calendar",
                user=user,
                resource_owner_key="toby:slack:41:UALICE",
                db=db,
                db_provider=_provider(),
            )
            for _ in range(2)
        ]
        db.commit()

    requests = []
    for index, start in enumerate(starts):
        state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]
        cookies = SimpleCookie()
        cookies.load(start.headers["set-cookie"])
        cookie_name, morsel = next(iter(cookies.items()))
        requests.append(
            SimpleNamespace(
                query_params={"state": state, "code": f"provider-code-{index}"},
                cookies={cookie_name: morsel.value},
            )
        )

    exchange_barrier = threading.Barrier(2)

    def exchange(*_args, **_kwargs) -> _Response:
        exchange_barrier.wait(timeout=10)
        return _Response({"access_token": "actor-token", "scope": "profile.read"})

    monkeypatch.setattr(auth_api.requests, "post", exchange)

    def callback(request) -> int:
        with factory() as callback_db:
            return auth_api.generic_oauth_callback(
                "custom", request, callback_db, _provider()
            ).status_code

    with ThreadPoolExecutor(max_workers=2) as executor:
        statuses = sorted(executor.map(callback, requests))

    assert statuses == [200, 200]
    with factory() as db:
        rows = db.query(UserOAuth).all()
        assert len(rows) == 1
        assert rows[0].resource_owner_key == "toby:slack:41:UALICE"


def _start_actor_flow(db, user, *, app_id: str, provider: str):
    response = auth_api.start_builtin_oauth_for_resource_owner(
        provider=provider,
        app_id=app_id,
        user=user,
        resource_owner_key="toby:slack:41:UALICE",
        db=db,
        db_provider=_provider(),
    )
    db.commit()
    state = parse_qs(urlparse(response.headers["location"]).query)["state"][0]
    cookies = SimpleCookie()
    cookies.load(response.headers["set-cookie"])
    cookie_name, morsel = next(iter(cookies.items()))
    return SimpleNamespace(
        query_params={"state": state, "code": f"{app_id}-code"},
        cookies={cookie_name: morsel.value},
    )


def _seed_actor_app(db, user, *, app_id: str, provider: str) -> None:
    execution, _optional_scopes = (
        mcp_apps.get_builtin_execution_fields_and_optional_scopes(app_id)
    )
    assert execution is not None
    assert execution["provider_name"] == provider
    app = PublicMCPApp(
        app_id=app_id,
        name=execution["name"],
        transport=execution["transport"],
        provider_name=provider,
        oauth_scopes=execution["oauth_scopes"],
        launch_config=execution["launch_config"],
        is_visible_in_connector=True,
    )
    server = MCPServer(
        name=execution["name"],
        managed="external",
        transport=execution["transport"],
        auth={"app_id": app_id, "provider": provider},
    )
    db.add_all([app, server])
    db.flush()
    db.add(
        UserMCPServer(
            user_id=user.id,
            mcpserver_id=server.id,
            is_owner=False,
            is_active=True,
        )
    )


def test_concurrent_actor_flows_for_distinct_apps_preserve_both_credentials(
    postgresql_engine, monkeypatch
) -> None:
    tables = [
        User.__table__,
        PublicMCPApp.__table__,
        MCPServer.__table__,
        UserMCPServer.__table__,
        ActorOAuthFlowState.__table__,
        UserOAuth.__table__,
    ]
    Base.metadata.create_all(postgresql_engine, tables=tables)
    factory = sessionmaker(bind=postgresql_engine, autoflush=False, autocommit=False)
    with factory() as db:
        user = User(username="workspace-account", password_hash="hash")
        db.add(user)
        db.flush()
        _seed_actor_app(db, user, app_id="calendar", provider="custom")
        _seed_actor_app(db, user, app_id="drive", provider="other")
        db.commit()
        flows = [
            (
                "custom",
                _start_actor_flow(db, user, app_id="calendar", provider="custom"),
            ),
            ("other", _start_actor_flow(db, user, app_id="drive", provider="other")),
        ]

    monkeypatch.setattr(
        auth_api.requests,
        "post",
        lambda *_args, **_kwargs: _Response(
            {"access_token": "actor-token", "scope": "profile.read"}
        ),
    )
    delete_accounts = auth_api.delete_scoped_user_oauth_accounts
    delete_barrier = threading.Barrier(2)

    def synchronize_delete(*args, **kwargs):
        deleted = delete_accounts(*args, **kwargs)
        try:
            delete_barrier.wait(timeout=1)
        except threading.BrokenBarrierError:
            pass
        return deleted

    monkeypatch.setattr(
        auth_api, "delete_scoped_user_oauth_accounts", synchronize_delete
    )

    def callback(provider_name: str, request) -> int:
        with factory() as callback_db:
            return auth_api.generic_oauth_callback(
                provider_name, request, callback_db, _provider()
            ).status_code

    with ThreadPoolExecutor(max_workers=2) as executor:
        statuses = sorted(
            future.result(timeout=10)
            for future in [
                executor.submit(callback, provider_name, request)
                for provider_name, request in flows
            ]
        )

    assert statuses == [200, 200]
    with factory() as db:
        assert {
            (row.provider, row.resource_owner_key) for row in db.query(UserOAuth).all()
        } == {
            ("calendar", "toby:slack:41:UALICE"),
            ("drive", "toby:slack:41:UALICE"),
        }
