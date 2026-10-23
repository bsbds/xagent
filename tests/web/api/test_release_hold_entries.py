"""WebSocket message handlers that reach the planned-restart execution hold.

Each test drives a real handler. Direct WS executions are refused in band
before any side effect and, when admitted, are counted until the handler
returns; follow-ups that already have a durable command path keep it. The
trigger HTTP entries are covered beside their own fixtures in
``test_agent_triggers`` / ``test_workforce_triggers``.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from xagent.web.api import websocket as ws_api
from xagent.web.services import release_hold as release_hold_module
from xagent.web.services.release_hold import ReleaseHold


@pytest.fixture
def hold(monkeypatch: pytest.MonkeyPatch) -> ReleaseHold:
    fresh = ReleaseHold()
    monkeypatch.setattr(release_hold_module, "release_hold", fresh)
    monkeypatch.setattr(ws_api, "release_hold", fresh)
    return fresh


# --- WebSocket message handlers --------------------------------------------------


class _Frames:
    """Records every personal frame the handlers send to a fake socket."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.sent: list[dict[str, Any]] = []

        async def record(message: dict[str, Any], _websocket: Any) -> None:
            self.sent.append(message)

        monkeypatch.setattr(ws_api.manager, "send_personal_message", record)
        monkeypatch.setattr(ws_api.manager, "broadcast_to_task", AsyncMock())

    def codes(self) -> list[str | None]:
        return [frame.get("error_code") for frame in self.sent]


def _socket() -> Any:
    return SimpleNamespace(state=SimpleNamespace(preview_task_id=None))


def _user() -> Any:
    return SimpleNamespace(id=7, is_admin=False)


async def test_execute_task_frame_is_refused_before_side_effects(
    hold, monkeypatch
) -> None:
    frames = _Frames(monkeypatch)
    started = AsyncMock()
    monkeypatch.setattr(ws_api.task_start_service, "execute_existing_task", started)
    hold.close("release r1")
    await ws_api.handle_execute_task(_socket(), 5, {"user": _user()})
    started.assert_not_called()
    # No legacy "execution_started" acknowledgement was sent first.
    assert [frame["type"] for frame in frames.sent] == ["error"]
    assert frames.codes() == ["execution_paused"]


async def test_admitted_execute_task_is_counted_until_registered(
    hold, monkeypatch
) -> None:
    _Frames(monkeypatch)
    seen: list[list[str]] = []

    async def start(**_kwargs: Any) -> None:
        seen.append([r["kind"] for r in hold.snapshot()["responsibilities"]])

    monkeypatch.setattr(ws_api.task_start_service, "execute_existing_task", start)
    monkeypatch.setattr(
        ws_api,
        "_load_legacy_execute_task_request_sync",
        lambda *_a, **_k: SimpleNamespace(
            task_id=5,
            task_owner_user_id=7,
            task_source=None,
            task_description="d",
            task_context={},
            task_info={},
            created_at=None,
        ),
    )
    await ws_api.handle_execute_task(_socket(), 5, {"user": _user()})
    assert seen == [["ws_execute_task"]]
    assert hold.snapshot()["responsibilities"] == []


async def test_new_preview_frame_is_refused_without_creating_a_task(
    hold, monkeypatch
) -> None:
    frames = _Frames(monkeypatch)
    create = AsyncMock()
    monkeypatch.setattr("xagent.web.api.chat.create_task", create)
    hold.close("release r1")
    socket = _socket()
    await ws_api.handle_build_preview_execution(
        socket, {"type": "preview", "message": "hi"}, _user()
    )
    create.assert_not_called()
    assert socket.state.preview_task_id is None
    assert frames.codes() == ["execution_paused"]


async def test_preview_follow_up_uses_the_durable_chat_command(
    hold, monkeypatch
) -> None:
    _Frames(monkeypatch)
    chat = AsyncMock()
    monkeypatch.setattr(ws_api, "handle_chat_message", chat)
    hold.close("release r1")
    socket = _socket()
    socket.state.preview_task_id = 12
    await ws_api.handle_build_preview_execution(
        socket, {"type": "preview", "message": "again"}, _user()
    )
    # Not refused here: the durable command stays pending under the held cap
    # unless task 12 is active, which is the dispatcher's decision.
    assert chat.await_args.args[1] == 12


async def test_missing_task_chat_executes_directly_so_it_is_admitted(
    hold, monkeypatch
) -> None:
    frames = _Frames(monkeypatch)
    monkeypatch.setattr(
        ws_api, "_enqueue_websocket_task_command", AsyncMock(return_value=None)
    )
    replacement = AsyncMock()
    monkeypatch.setattr(
        ws_api.command_execution_service, "handle_missing_task_message", replacement
    )
    hold.close("release r1")
    await ws_api.handle_chat_message(
        _socket(),
        9,
        {"type": "chat", "message": "hi", "client_message_id": "m-1", "user": _user()},
    )
    replacement.assert_not_called()
    deliveries = [f for f in frames.sent if f.get("type") == "message_rejected"]
    assert deliveries, frames.sent
    assert deliveries[0].get("rejection_outcome") == "not_accepted"
    assert "execution_paused" in frames.codes()
