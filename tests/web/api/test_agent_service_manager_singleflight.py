"""Concurrency regression tests for task-scoped AgentService construction."""

from __future__ import annotations

import asyncio
from collections import deque
from unittest.mock import AsyncMock, MagicMock

import pytest

from xagent.web.api.chat import AgentServiceManager


@pytest.mark.asyncio
async def test_get_agent_for_task_serializes_builds_for_same_task() -> None:
    manager = AgentServiceManager()
    active_builds = 0
    max_active_builds = 0

    async def _build(*args, **kwargs):
        nonlocal active_builds, max_active_builds
        active_builds += 1
        max_active_builds = max(max_active_builds, active_builds)
        await asyncio.sleep(0)
        active_builds -= 1
        return object()

    manager._get_agent_for_task_unlocked = AsyncMock(side_effect=_build)

    await asyncio.gather(
        manager.get_agent_for_task(42),
        manager.get_agent_for_task(42),
    )

    assert max_active_builds == 1
    assert manager._get_agent_for_task_unlocked.await_count == 2


@pytest.mark.asyncio
async def test_get_agent_for_task_allows_different_tasks_to_build_concurrently() -> (
    None
):
    manager = AgentServiceManager()
    both_started = asyncio.Event()
    started: set[int] = set()

    async def _build(task_id: int, **kwargs):
        started.add(task_id)
        if len(started) == 2:
            both_started.set()
        await asyncio.wait_for(both_started.wait(), timeout=1)
        return object()

    manager._get_agent_for_task_unlocked = AsyncMock(side_effect=_build)

    await asyncio.gather(
        manager.get_agent_for_task(42),
        manager.get_agent_for_task(43),
    )

    assert started == {42, 43}


def test_remove_agent_releases_per_task_build_lock() -> None:
    manager = AgentServiceManager()
    manager._agent_build_locks[42] = asyncio.Lock()
    manager._cleanup_workspace_directory = MagicMock()

    manager.remove_agent(42)

    assert 42 not in manager._agent_build_locks


def test_remove_agent_detaches_runtime_state_when_workspace_cleanup_fails() -> None:
    """Failed deletion must detach runtime state and permit a direct retry."""
    manager = AgentServiceManager()
    agent = MagicMock()
    agent.workspace.workspace_dir = "/tmp/web_task_42"
    agent.cleanup_workspace.side_effect = RuntimeError("filesystem unavailable")
    manager._agents[42] = agent
    manager._agent_owner_ids[42] = 7
    manager._agent_sandbox_keys[42] = "user:7"
    manager._agent_sandbox_providers[42] = object()
    manager._agent_scope_fingerprints[42] = None
    manager._agent_evicted_scope_fingerprints[42] = deque([None])
    manager._mcp_policy_required_task_ids.add(42)
    manager._agent_build_locks[42] = asyncio.Lock()
    manager._cleanup_workspace_directory = MagicMock()

    with pytest.raises(RuntimeError, match="filesystem unavailable"):
        manager.remove_agent(42)

    assert 42 not in manager._agents
    assert 42 not in manager._agent_owner_ids
    assert 42 not in manager._agent_sandbox_keys
    assert 42 not in manager._agent_sandbox_providers
    assert 42 not in manager._agent_scope_fingerprints
    assert 42 not in manager._agent_evicted_scope_fingerprints
    assert 42 not in manager._mcp_policy_required_task_ids
    assert 42 not in manager._agent_build_locks
    assert manager._agent_cleanup_owner_ids[42] == 7

    manager.remove_agent(42)
    manager._cleanup_workspace_directory.assert_called_once_with(42, 7)
    assert 42 not in manager._agent_cleanup_owner_ids


@pytest.mark.asyncio
async def test_remove_agent_keeps_inflight_build_lock() -> None:
    manager = AgentServiceManager()
    lock = asyncio.Lock()
    await lock.acquire()
    manager._agent_build_locks[42] = lock
    manager._cleanup_workspace_directory = MagicMock()

    manager.remove_agent(42)

    assert manager._agent_build_locks[42] is lock
    lock.release()
