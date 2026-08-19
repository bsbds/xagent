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


def _trusted_start(db: Session, user: User, owner: str):
    return start_builtin_oauth_for_resource_owner(
        provider="custom",
        app_id="calendar",
        user=user,
        resource_owner_key=owner,
        redirect="https://toby.example/settings",
        db=db,
        db_provider=_provider(),
    )


def test_trusted_start_places_actor_only_in_signed_state(oauth_db) -> None:
    db, user = oauth_db

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

    with pytest.raises(ValueError, match="resource_owner_key"):
        _trusted_start(db, user, "   ")
    with pytest.raises(ValueError, match="resource_owner_key"):
        _trusted_start(db, user, "x" * 513)


def test_callback_persists_separate_actor_rows_for_one_xagent_user(
    oauth_db, monkeypatch
) -> None:
    db, user = oauth_db
    db.add(
        PublicMCPApp(
            app_id="calendar",
            name="Google Calendar",
            description="Calendar",
            transport="oauth",
            provider_name="custom",
            launch_config={"command": "calendar"},
        )
    )
    db.commit()
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
    server = db.query(MCPServer).filter(MCPServer.name == "Google Calendar").one()
    assert (
        db.query(UserMCPServer)
        .filter(
            UserMCPServer.user_id == user.id,
            UserMCPServer.mcpserver_id == server.id,
        )
        .one_or_none()
        is None
    )


def test_callback_replaces_only_the_same_actor_namespace(oauth_db, monkeypatch) -> None:
    db, user = oauth_db
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
