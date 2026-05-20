from __future__ import annotations

from typing import Any

import pytest

from xagent.core.agent.service import AgentService


class NamedTool:
    def __init__(self, name: str) -> None:
        self.name = name


class RefreshingToolConfig:
    _workspace_config = None

    def __init__(self) -> None:
        self.refresh_count = 0

    def refresh_user_tool_overrides(self) -> None:
        self.refresh_count += 1

    def get_user_tool_overrides(self) -> dict[str, dict[str, bool]]:
        return {"disabled": {"enabled": self.refresh_count < 2}}

    def get_allowed_tools(self) -> None:
        return None


@pytest.mark.asyncio
async def test_agent_service_refreshes_initialized_tools_when_policy_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tool_config = RefreshingToolConfig()
    tool_sets: list[list[Any]] = [
        [NamedTool("allowed"), NamedTool("disabled")],
        [NamedTool("allowed")],
    ]

    async def create_all_tools(config: Any) -> list[Any]:
        assert config is tool_config
        return tool_sets.pop(0)

    monkeypatch.setattr(
        "xagent.core.tools.adapters.vibe.factory.ToolFactory.create_all_tools",
        create_all_tools,
    )

    service = AgentService(
        name="tool-refresh-test",
        id="tool-refresh-test",
        tool_config=tool_config,
        enable_workspace=False,
    )

    await service._ensure_tools_initialized()
    assert {tool.name for tool in service.tools} == {"allowed", "disabled"}

    await service._ensure_tools_initialized()
    assert {tool.name for tool in service.tools} == {"allowed"}
    assert tool_config.refresh_count == 2
