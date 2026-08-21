"""Trusted actor ownership for existing builtin OAuth provider flows."""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.parse import parse_qs, urlparse

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from xagent.core.utils.encryption import encrypt_value
from xagent.web import mcp_apps
from xagent.web.api import auth as auth_api
from xagent.web.api.auth import (
    create_access_token,
    generic_oauth_callback,
    generic_oauth_login,
    start_builtin_oauth_for_resource_owner,
    verify_token,
)
from xagent.web.models.database import Base
from xagent.web.models.mcp import MCPServer, UserMCPServer
from xagent.web.models.public_mcp import PublicMCPApp
from xagent.web.models.user import User
from xagent.web.models.user_oauth import UserOAuth
from xagent.web.services.connector_team_scope import set_connector_team_hooks

ACTOR_ALICE = "toby:slack:41:UALICE"
ACTOR_BOB = "toby:slack:41:UBOB"


class _Response:
    def __init__(self, data: dict, status_code: int = 200):
        self._data = data
        self.status_code = status_code

    def json(self) -> dict:
        return self._data


@pytest.fixture
def oauth_db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'builtin-oauth.db'}")
    Base.metadata.create_all(engine)
    session_local = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = session_local()
    user = User(username="workspace-account", password_hash="hash")
    db.add(user)
    db.commit()
    db.refresh(user)
    try:
        yield db, user
    finally:
        db.close()
        engine.dispose()


def _provider():
    return SimpleNamespace(
        client_id=encrypt_value("client-id"),
        client_secret=encrypt_value("client-secret"),
        auth_url="https://provider.example/authorize",
        token_url="https://provider.example/token",
        userinfo_url="https://provider.example/me",
        redirect_uri="https://xagent.example/api/auth/custom/callback",
        default_scopes=["profile.read"],
        user_id_path="id",
        email_path="email",
    )


def _access_token(user: User) -> str:
    return create_access_token(
        data={"sub": user.username, "type": "access"},
        expires_delta=timedelta(minutes=5),
    )


def _state(response) -> str:
    return parse_qs(urlparse(response.headers["location"]).query)["state"][0]


def _callback_request(state: str, code: str = "code"):
    return SimpleNamespace(query_params={"state": state, "code": code})


def _mock_provider_exchange(
    monkeypatch, *, provider_user_id: str = "provider-account"
) -> None:
    def post(_url, *, data, **_kwargs):
        return _Response(
            {
                "access_token": f"access:{data['code']}",
                "refresh_token": f"refresh:{data['code']}",
                "expires_in": 3600,
                "scope": "profile.read",
            }
        )

    monkeypatch.setattr(auth_api.requests, "post", post)
    monkeypatch.setattr(
        auth_api.requests,
        "get",
        Mock(
            return_value=_Response(
                {"id": provider_user_id, "email": "member@example.com"}
            )
        ),
    )


@pytest.mark.parametrize("app_id", [None, "", "   "])
def test_actor_builtin_oauth_start_requires_nonempty_app_id(oauth_db, app_id) -> None:
    db, user = oauth_db
    with pytest.raises(ValueError, match="app_id"):
        start_builtin_oauth_for_resource_owner(
            provider="custom",
            app_id=app_id,
            user=user,
            resource_owner_key=ACTOR_ALICE,
            db=db,
            db_provider=_provider(),
        )


def _visible_calendar(
    db: Session, user: User, *, active: bool = True
) -> tuple[PublicMCPApp, MCPServer, UserMCPServer]:
    app = db.query(PublicMCPApp).filter(PublicMCPApp.app_id == "calendar").first()
    if app is None:
        app = PublicMCPApp(
            app_id="calendar",
            name="Google Calendar",
            description="Calendar",
            transport="oauth",
            provider_name="custom",
            launch_config={"command": "calendar"},
        )
        db.add(app)
    server = db.query(MCPServer).filter(MCPServer.name == "Google Calendar").first()
    if server is None:
        server = MCPServer(
            name="Google Calendar",
            description="Calendar",
            managed="external",
            transport="oauth",
            auth={"app_id": "calendar", "provider": "custom"},
        )
        db.add(server)
        db.flush()
    association = (
        db.query(UserMCPServer)
        .filter(
            UserMCPServer.user_id == user.id,
            UserMCPServer.mcpserver_id == server.id,
        )
        .first()
    )
    if association is None:
        association = UserMCPServer(
            user_id=int(user.id),
            mcpserver_id=int(server.id),
            is_owner=True,
            is_active=active,
        )
        db.add(association)
    else:
        association.is_active = active
    db.commit()
    return app, server, association


def _trusted_start(
    db: Session,
    user: User,
    owner: str,
    *,
    governing_team_id: int | None = None,
):
    return start_builtin_oauth_for_resource_owner(
        provider="custom",
        app_id="calendar",
        user=user,
        resource_owner_key=owner,
        redirect="https://toby.example/settings",
        db=db,
        db_provider=_provider(),
        governing_team_id=governing_team_id,
    )


def test_trusted_start_rejects_app_without_visible_server_link(oauth_db) -> None:
    db, user = oauth_db
    app, server, association = _visible_calendar(db, user, active=False)

    expected_error = getattr(
        auth_api, "ActorBuiltinOAuthServerNotVisibleError", ValueError
    )
    with pytest.raises(expected_error, match="visible MCP server"):
        _trusted_start(db, user, ACTOR_ALICE)

    assert app.app_id == "calendar"
    assert server.id is not None
    assert association.is_active is False


def test_visibility_helper_creates_one_nonowning_machine_link(oauth_db) -> None:
    """Trusted setup creates visibility metadata, never an actor credential."""
    db, user = oauth_db
    app = PublicMCPApp(
        app_id="calendar",
        name="Google Calendar",
        description="Calendar",
        transport="oauth",
        provider_name="custom",
        launch_config={"command": "calendar"},
        is_visible_in_connector=True,
    )
    db.add(app)
    db.commit()

    helper = getattr(mcp_apps, "ensure_builtin_oauth_server_visibility_for_user", None)
    assert helper is not None
    first = helper(db, user_id=int(user.id), app_id="calendar")
    second = helper(db, user_id=int(user.id), app_id="calendar")

    assert first.id == second.id
    assert first.name == "Google Calendar"
    assert first.managed == "external"
    assert first.transport == "oauth"
    assert first.auth == {"app_id": "calendar", "provider": "custom"}
    links = db.query(UserMCPServer).all()
    assert len(links) == 1
    assert links[0].user_id == user.id
    assert links[0].is_active is True
    assert links[0].is_owner is False
    assert links[0].can_edit is False
    assert links[0].can_delete is False
    assert db.query(UserOAuth).count() == 0


def test_visibility_helper_reactivates_without_downgrading_permissions(
    oauth_db,
) -> None:
    db, user = oauth_db
    _app, server, association = _visible_calendar(db, user, active=False)
    association.can_edit = True
    association.can_delete = True
    db.commit()

    helper = getattr(mcp_apps, "ensure_builtin_oauth_server_visibility_for_user", None)
    assert helper is not None
    result = helper(db, user_id=int(user.id), app_id="calendar")

    assert result.id == server.id
    db.refresh(association)
    assert association.is_active is True
    assert association.can_edit is True
    assert association.can_delete is True


def test_visibility_helper_adopts_only_a_canonical_legacy_definition(oauth_db) -> None:
    db, user = oauth_db
    app = PublicMCPApp(
        app_id="calendar",
        name="Google Calendar",
        description="Current description",
        transport="oauth",
        provider_name="custom",
        launch_config={"command": "calendar"},
        is_visible_in_connector=True,
    )
    legacy = MCPServer(
        name="Google Calendar",
        description="Legacy description",
        managed="external",
        transport="oauth",
        auth={"provider": "custom"},
    )
    db.add_all([app, legacy])
    db.commit()

    helper = getattr(mcp_apps, "ensure_builtin_oauth_server_visibility_for_user", None)
    assert helper is not None
    result = helper(db, user_id=int(user.id), app_id="calendar")

    assert result.id == legacy.id
    assert result.auth == {"app_id": "calendar", "provider": "custom"}
    assert result.description == "Current description"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("command", "/bin/evil"),
        ("args", ["--pwn"]),
        ("url", "https://evil.example/mcp"),
        ("env", {"TOKEN": "secret"}),
        ("headers", {"Authorization": "Bearer secret"}),
        ("runtime_input_schema", {"secrets": {"token": {"type": "string"}}}),
        ("allow_delegated_authorization", True),
        ("docker_image", "evil:latest"),
    ],
)
def test_visibility_helper_rejects_noncanonical_executable_fields(
    oauth_db, field, value
) -> None:
    db, user = oauth_db
    app = PublicMCPApp(
        app_id="calendar",
        name="Google Calendar",
        description="Calendar",
        transport="oauth",
        provider_name="custom",
        launch_config={"command": "calendar"},
        is_visible_in_connector=True,
    )
    server = MCPServer(
        name="Google Calendar",
        description="Calendar",
        managed="external",
        transport="oauth",
        auth={"app_id": "calendar", "provider": "custom"},
    )
    setattr(server, field, value)
    db.add_all([app, server])
    db.commit()

    helper = getattr(mcp_apps, "ensure_builtin_oauth_server_visibility_for_user", None)
    assert helper is not None
    error = getattr(mcp_apps, "BuiltinOAuthServerDefinitionError", ValueError)
    with pytest.raises(error, match="canonical"):
        helper(db, user_id=int(user.id), app_id="calendar")

    assert db.query(UserMCPServer).count() == 0


def test_visibility_helper_rejects_duplicate_legacy_catalog_names(oauth_db) -> None:
    db, user = oauth_db
    db.add_all(
        [
            PublicMCPApp(
                app_id="calendar",
                name="Google Calendar",
                transport="oauth",
                provider_name="custom",
                launch_config={"command": "calendar"},
                is_visible_in_connector=True,
            ),
            PublicMCPApp(
                app_id="calendar-secondary",
                name="Google Calendar",
                transport="oauth",
                provider_name="custom",
                launch_config={"command": "calendar-secondary"},
                is_visible_in_connector=True,
            ),
            MCPServer(
                name="Google Calendar",
                managed="external",
                transport="oauth",
            ),
        ]
    )
    db.commit()

    helper = getattr(mcp_apps, "ensure_builtin_oauth_server_visibility_for_user", None)
    assert helper is not None
    error = getattr(mcp_apps, "BuiltinOAuthServerDefinitionError", ValueError)
    with pytest.raises(error, match="ambiguous"):
        helper(db, user_id=int(user.id), app_id="calendar")


def test_visibility_helper_rejects_multiple_server_definitions(oauth_db) -> None:
    db, user = oauth_db
    db.add(
        PublicMCPApp(
            app_id="calendar",
            name="Google Calendar",
            transport="oauth",
            provider_name="custom",
            launch_config={"command": "calendar"},
            is_visible_in_connector=True,
        )
    )
    db.add_all(
        [
            MCPServer(
                name="calendar",
                managed="external",
                transport="oauth",
                auth={"app_id": "calendar", "provider": "custom"},
            ),
            MCPServer(
                name="Google Calendar",
                managed="external",
                transport="oauth",
            ),
        ]
    )
    db.commit()

    helper = getattr(mcp_apps, "ensure_builtin_oauth_server_visibility_for_user", None)
    assert helper is not None
    error = getattr(mcp_apps, "BuiltinOAuthServerDefinitionError", ValueError)
    with pytest.raises(error, match="multiple"):
        helper(db, user_id=int(user.id), app_id="calendar")


def test_visibility_helper_leaves_commit_and_rollback_to_caller(oauth_db) -> None:
    db, user = oauth_db
    db.add(
        PublicMCPApp(
            app_id="calendar",
            name="Google Calendar",
            transport="oauth",
            provider_name="custom",
            launch_config={"command": "calendar"},
            is_visible_in_connector=True,
        )
    )
    db.commit()

    helper = getattr(mcp_apps, "ensure_builtin_oauth_server_visibility_for_user", None)
    assert helper is not None
    helper(db, user_id=int(user.id), app_id="calendar")
    db.rollback()

    assert db.query(MCPServer).count() == 0
    assert db.query(UserMCPServer).count() == 0


def test_trusted_start_rejects_same_named_server_linked_to_another_app(
    oauth_db,
) -> None:
    db, user = oauth_db
    _app, server, _association = _visible_calendar(db, user)
    server.auth = {"app_id": "another-calendar", "provider": "custom"}
    db.commit()

    with pytest.raises(ValueError, match="visible MCP server"):
        _trusted_start(db, user, ACTOR_ALICE)


def test_trusted_start_accepts_governing_team_visible_server(oauth_db) -> None:
    db, user = oauth_db
    _app, server, _association = _visible_calendar(db, user, active=False)
    set_connector_team_hooks(
        team_visibility=lambda _db, *, team_id: {
            "mcp": {int(server.id)} if team_id == 41 else set(),
            "custom_api": set(),
        }
    )
    try:
        response = _trusted_start(db, user, ACTOR_ALICE, governing_team_id=41)
    finally:
        set_connector_team_hooks()

    assert verify_token(_state(response))["resource_owner_key"] == ACTOR_ALICE


def test_trusted_start_places_actor_only_in_signed_state(oauth_db) -> None:
    db, user = oauth_db
    _visible_calendar(db, user)

    response = _trusted_start(db, user, ACTOR_ALICE)

    payload = verify_token(_state(response))
    assert payload is not None
    assert payload["resource_owner_key"] == ACTOR_ALICE
    assert ACTOR_ALICE not in response.headers["location"].split("state=")[0]


def test_public_start_uses_the_ordinary_null_owner(oauth_db) -> None:
    db, user = oauth_db

    response = generic_oauth_login(
        "custom",
        token=_access_token(user),
        app_id="calendar",
        db=db,
        db_provider=_provider(),
    )

    payload = verify_token(_state(response))
    assert payload is not None
    assert payload.get("resource_owner_key") is None


def test_trusted_start_rejects_blank_or_oversized_owner(oauth_db) -> None:
    db, user = oauth_db
    _visible_calendar(db, user)

    with pytest.raises(ValueError, match="resource_owner_key"):
        _trusted_start(db, user, "   ")
    with pytest.raises(ValueError, match="resource_owner_key"):
        _trusted_start(db, user, "x" * 513)


def test_callback_persists_separate_actor_rows_for_one_xagent_user(
    oauth_db, monkeypatch
) -> None:
    db, user = oauth_db
    _app, server, association = _visible_calendar(db, user)
    _mock_provider_exchange(monkeypatch)

    for owner, code in ((ACTOR_ALICE, "alice"), (ACTOR_BOB, "bob")):
        response = generic_oauth_callback(
            "custom",
            _callback_request(_state(_trusted_start(db, user, owner)), code),
            db,
            _provider(),
        )
        assert response.status_code == 200

    rows = db.query(UserOAuth).order_by(UserOAuth.resource_owner_key).all()
    assert [(row.resource_owner_key, row.access_token) for row in rows] == [
        (ACTOR_ALICE, "access:alice"),
        (ACTOR_BOB, "access:bob"),
    ]
    assert {row.provider_user_id for row in rows} == {"provider-account"}
    db.refresh(association)
    assert association.mcpserver_id == server.id
    assert association.is_active is True


def test_actor_callback_does_not_reactivate_inactive_ordinary_association(
    oauth_db, monkeypatch
) -> None:
    db, user = oauth_db
    app = PublicMCPApp(
        app_id="calendar",
        name="Google Calendar",
        description="Calendar",
        transport="oauth",
        provider_name="custom",
        launch_config={"command": "calendar"},
    )
    server = MCPServer(
        name="Google Calendar",
        description="Calendar",
        managed="external",
        transport="oauth",
        auth={"app_id": "calendar", "provider": "custom"},
    )
    db.add_all([app, server])
    db.flush()
    association = UserMCPServer(
        user_id=int(user.id),
        mcpserver_id=int(server.id),
        is_owner=True,
        is_active=False,
    )
    db.add(association)
    db.commit()
    _mock_provider_exchange(monkeypatch)
    set_connector_team_hooks(
        team_visibility=lambda _db, *, team_id: {
            "mcp": {int(server.id)} if team_id == 41 else set(),
            "custom_api": set(),
        }
    )
    try:
        start = _trusted_start(db, user, ACTOR_ALICE, governing_team_id=41)
        response = generic_oauth_callback(
            "custom",
            _callback_request(_state(start), "alice"),
            db,
            _provider(),
        )
    finally:
        set_connector_team_hooks()

    assert response.status_code == 200
    db.refresh(association)
    assert association.is_active is False
    assert db.query(UserOAuth).one().resource_owner_key == ACTOR_ALICE


def test_actor_callback_rejects_server_relinked_after_trusted_start(
    oauth_db, monkeypatch
) -> None:
    db, user = oauth_db
    _app, server, _association = _visible_calendar(db, user)
    start = _trusted_start(db, user, ACTOR_ALICE)
    server.auth = {"app_id": "another-calendar", "provider": "custom"}
    db.commit()
    exchange = Mock()
    monkeypatch.setattr(auth_api.requests, "post", exchange)

    response = generic_oauth_callback(
        "custom",
        _callback_request(_state(start), "alice"),
        db,
        _provider(),
    )

    assert response.status_code == 409
    exchange.assert_not_called()
    assert db.query(UserOAuth).count() == 0


def test_callback_replaces_only_the_same_actor_namespace(oauth_db, monkeypatch) -> None:
    db, user = oauth_db
    _visible_calendar(db, user)
    db.add_all(
        [
            UserOAuth(
                user_id=int(user.id),
                provider="calendar",
                resource_owner_key=None,
                provider_user_id="provider-account",
                access_token="ordinary",
            ),
            UserOAuth(
                user_id=int(user.id),
                provider="calendar",
                resource_owner_key=ACTOR_ALICE,
                provider_user_id="provider-account",
                access_token="old-alice",
            ),
            UserOAuth(
                user_id=int(user.id),
                provider="calendar",
                resource_owner_key=ACTOR_BOB,
                provider_user_id="provider-account",
                access_token="bob",
            ),
        ]
    )
    db.commit()
    _mock_provider_exchange(monkeypatch)

    response = generic_oauth_callback(
        "custom",
        _callback_request(_state(_trusted_start(db, user, ACTOR_ALICE)), "new-alice"),
        db,
        _provider(),
    )

    assert response.status_code == 200
    rows = db.query(UserOAuth).all()
    tokens = {row.resource_owner_key: row.access_token for row in rows}
    assert tokens == {
        None: "ordinary",
        ACTOR_ALICE: "access:new-alice",
        ACTOR_BOB: "bob",
    }


def test_actor_callback_skips_ordinary_post_commit_side_effects(
    oauth_db, monkeypatch
) -> None:
    db, user = oauth_db
    _visible_calendar(db, user)
    _mock_provider_exchange(monkeypatch)
    post_commit = Mock(side_effect=AssertionError("ordinary side effects must not run"))
    monkeypatch.setattr(auth_api, "_run_post_commit_oauth_side_effects", post_commit)

    response = generic_oauth_callback(
        "custom",
        _callback_request(_state(_trusted_start(db, user, ACTOR_ALICE)), "alice"),
        db,
        _provider(),
    )

    assert response.status_code == 200
    post_commit.assert_not_called()


def test_actor_callback_rejects_signed_state_without_app_id(
    oauth_db, monkeypatch
) -> None:
    db, user = oauth_db
    state = create_access_token(
        data={
            "type": "oauth_state",
            "user_id": int(user.id),
            "provider": "custom",
            "app_id": None,
            "resource_owner_key": ACTOR_ALICE,
        },
        expires_delta=timedelta(minutes=10),
    )
    token_exchange = Mock(
        side_effect=AssertionError("invalid actor state must fail before exchange")
    )
    ensure_server = Mock(
        side_effect=AssertionError("invalid actor state must not mutate associations")
    )
    monkeypatch.setattr(auth_api.requests, "post", token_exchange)
    monkeypatch.setattr(auth_api, "_ensure_user_mcp_server", ensure_server)

    response = generic_oauth_callback(
        "custom",
        _callback_request(state, "alice"),
        db,
        _provider(),
    )

    assert response.status_code == 400
    token_exchange.assert_not_called()
    ensure_server.assert_not_called()
    assert db.query(UserOAuth).count() == 0


def test_public_callback_persists_an_ordinary_row(oauth_db, monkeypatch) -> None:
    db, user = oauth_db
    _mock_provider_exchange(monkeypatch)
    start = generic_oauth_login(
        "custom",
        token=_access_token(user),
        app_id="calendar",
        db=db,
        db_provider=_provider(),
    )

    response = generic_oauth_callback(
        "custom",
        _callback_request(_state(start), "ordinary"),
        db,
        _provider(),
    )

    assert response.status_code == 200
    row = db.query(UserOAuth).one()
    assert row.resource_owner_key is None
    assert row.access_token == "access:ordinary"


def test_callback_rejects_an_invalid_signed_owner_before_exchange(
    oauth_db, monkeypatch
) -> None:
    db, user = oauth_db
    state = create_access_token(
        data={
            "type": "oauth_state",
            "user_id": int(user.id),
            "provider": "custom",
            "app_id": "calendar",
            "resource_owner_key": "   ",
        },
        expires_delta=timedelta(minutes=10),
    )
    post = Mock(side_effect=AssertionError("token exchange must not run"))
    monkeypatch.setattr(auth_api.requests, "post", post)

    response = generic_oauth_callback(
        "custom",
        _callback_request(state),
        db,
        _provider(),
    )

    assert response.status_code == 400
    assert "Invalid or expired state" in response.body.decode()
    post.assert_not_called()
