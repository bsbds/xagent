"""Actor builtin-OAuth policy enforcement through real delegated-agent builds."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from xagent.core.tools.adapters.vibe.agent_tool import AgentTool, create_agent_tools
from xagent.web.models.agent import Agent, AgentStatus
from xagent.web.models.database import Base
from xagent.web.models.mcp import MCPServer, UserMCPServer
from xagent.web.models.model import Model
from xagent.web.models.public_mcp import PublicMCPApp
from xagent.web.models.user import User
from xagent.web.models.user_oauth import UserOAuth
from xagent.web.services.mcp_runtime import MCPBuiltinOAuthActorPolicy

ALICE = "toby:slack:41:UALICE"
BOB = "toby:slack:41:UBOB"


@pytest.fixture()
def delegated_db(tmp_path: Path) -> Iterator[tuple[Session, Any, User]]:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = session_factory()
    user = User(username="delegated-actor-owner", password_hash="hash")
    db.add(user)
    db.commit()
    db.refresh(user)
    try:
        yield db, session_factory, user
    finally:
        db.close()
        engine.dispose()


def _add_builtin_server(
    db: Session,
    user: User,
    *,
    app_id: str,
    name: str,
) -> MCPServer:
    server = MCPServer(
        name=name,
        description=name,
        managed="external",
        transport="oauth",
        auth={"app_id": app_id, "provider": app_id},
    )
    db.add(server)
    db.flush()
    db.add_all(
        [
            UserMCPServer(
                user_id=int(user.id),
                mcpserver_id=int(server.id),
                is_owner=True,
                is_active=True,
            ),
            PublicMCPApp(
                app_id=app_id,
                name=name,
                description=name,
                transport="oauth",
                provider_name=app_id,
                launch_config={
                    "command": "python",
                    "args": ["-m", f"test.{app_id}"],
                    "env_mapping": {"ACCESS_TOKEN": "access_token"},
                },
            ),
        ]
    )
    db.commit()
    db.refresh(server)
    return server


def _add_accounts(db: Session, user: User, app_id: str) -> None:
    db.add_all(
        [
            UserOAuth(
                user_id=int(user.id),
                provider=app_id,
                resource_owner_key=None,
                provider_user_id="ordinary",
                access_token=f"ordinary:{app_id}",
            ),
            UserOAuth(
                user_id=int(user.id),
                provider=app_id,
                resource_owner_key=ALICE,
                provider_user_id="alice",
                access_token=f"alice:{app_id}",
            ),
            UserOAuth(
                user_id=int(user.id),
                provider=app_id,
                resource_owner_key=BOB,
                provider_user_id="bob",
                access_token=f"bob:{app_id}",
            ),
        ]
    )
    db.commit()


def _add_agent(
    db: Session,
    user: User,
    model: Model,
    *,
    name: str,
    categories: list[str] | None,
) -> Agent:
    agent = Agent(
        user_id=int(user.id),
        name=name,
        description=name,
        instructions="Use the selected connector.",
        status=AgentStatus.PUBLISHED,
        models={"general": int(model.id)},
        tool_categories=categories,
    )
    db.add(agent)
    db.commit()
    db.refresh(agent)
    return agent


def _token_or_reason(config: dict[str, Any]) -> str:
    if config["transport"] == "unavailable":
        return str(config["config"]["reason"])
    return str(config["config"]["env"]["ACCESS_TOKEN"])


async def _run_delegated_path(
    db: Session,
    session_factory: Any,
    user: User,
    *,
    policy: MCPBuiltinOAuthActorPolicy | None,
    nested: bool,
) -> dict[str, list[str]]:
    allowed = _add_builtin_server(
        db, user, app_id="delegated-allowed", name="Delegated Allowed"
    )
    _add_builtin_server(
        db, user, app_id="delegated-excluded", name="Delegated Excluded"
    )
    _add_accounts(db, user, "delegated-allowed")
    _add_accounts(db, user, "delegated-excluded")
    model = Model(
        model_id="delegated-test-model",
        category="llm",
        model_provider="openai",
        model_name="gpt-test",
        api_key="test",
        abilities=["chat"],
    )
    db.add(model)
    db.commit()
    db.refresh(model)
    leaf = _add_agent(
        db,
        user,
        model,
        name="Leaf Delegate",
        categories=["mcp:Delegated Allowed", "mcp:Delegated Excluded"],
    )
    root_categories = (
        None if nested else ["mcp:Delegated Allowed", "mcp:Delegated Excluded"]
    )
    root = _add_agent(
        db,
        user,
        model,
        name="Root Delegate",
        categories=root_categories,
    )
    effective_policy = policy
    if policy is not None:
        effective_policy = MCPBuiltinOAuthActorPolicy(
            builtin_oauth_resource_owner_key=policy.builtin_oauth_resource_owner_key,
            allowed_builtin_oauth_server_ids=frozenset({int(allowed.id)}),
        )

    observations: dict[str, list[str]] = {}

    class _DelegatedService:
        def __init__(self, *, name: str, tool_config: Any, **_kwargs: Any) -> None:
            self.name = name
            self.tool_config = tool_config
            self.workspace = MagicMock()

        async def execute_task(self, *, task: str, **_kwargs: Any) -> dict[str, Any]:
            del task
            configs = await self.tool_config.get_mcp_server_configs()
            observations[self.name] = [_token_or_reason(config) for config in configs]
            if self.name == "Root Delegate" and nested:
                nested_tools = await create_agent_tools(self.tool_config)
                assert len(nested_tools) == 1
                nested_result = await nested_tools[0].run_json_async({"task": "nested"})
                assert nested_result["response"] == "Leaf Delegate completed"
            return {"output": f"{self.name} completed"}

        async def close(self) -> None:
            return None

    tool = AgentTool(
        agent_id=int(root.id),
        agent_name=root.name,
        agent_description=root.description or "",
        session_factory=session_factory,
        user_id=int(user.id),
        task_id="parent-task",
        delegation_allowed_agent_ids=[int(leaf.id)] if nested else [],
        mcp_runtime_authorization_policy=effective_policy,
    )
    tool._create_child_execution_tracer = lambda **_kwargs: None  # type: ignore[method-assign]

    mock_storage = MagicMock()
    mock_storage.get_llm_by_name_with_access.return_value = MagicMock()
    with (
        patch(
            "xagent.web.services.llm_utils.UserAwareModelStorage",
            return_value=mock_storage,
        ),
        patch("xagent.core.agent.service.AgentService", _DelegatedService),
    ):
        result = await tool.run_json_async({"task": "delegate"})

    assert result["response"] == "Root Delegate completed"
    return observations


@pytest.mark.asyncio
async def test_actor_policy_resolves_alice_and_excludes_unallowed_server_through_delegation(
    delegated_db,
) -> None:
    db, session_factory, user = delegated_db
    observations = await _run_delegated_path(
        db,
        session_factory,
        user,
        policy=MCPBuiltinOAuthActorPolicy(
            builtin_oauth_resource_owner_key=ALICE,
            allowed_builtin_oauth_server_ids=frozenset({999}),
        ),
        nested=False,
    )

    assert observations["Root Delegate"] == [
        "alice:delegated-allowed",
        "actor_policy_server_not_allowed",
    ]
    assert all(
        "ordinary" not in value and "bob" not in value
        for value in observations["Root Delegate"]
    )


@pytest.mark.asyncio
async def test_policyless_ordinary_delegation_keeps_ordinary_credentials(
    delegated_db,
) -> None:
    db, session_factory, user = delegated_db
    observations = await _run_delegated_path(
        db, session_factory, user, policy=None, nested=False
    )

    assert observations["Root Delegate"] == [
        "ordinary:delegated-allowed",
        "ordinary:delegated-excluded",
    ]


@pytest.mark.asyncio
async def test_nested_delegation_resolves_the_same_alice_policy_at_both_depths(
    delegated_db,
) -> None:
    db, session_factory, user = delegated_db
    observations = await _run_delegated_path(
        db,
        session_factory,
        user,
        policy=MCPBuiltinOAuthActorPolicy(
            builtin_oauth_resource_owner_key=ALICE,
            allowed_builtin_oauth_server_ids=frozenset({999}),
        ),
        nested=True,
    )

    expected = ["alice:delegated-allowed", "actor_policy_server_not_allowed"]
    assert observations == {
        # The unconfigured root intentionally does not initialize MCP; its
        # nested leaf explicitly selects MCP and must still resolve Alice.
        "Root Delegate": [],
        "Leaf Delegate": expected,
    }
