"""A browser chat socket across a crash and the automatic resume that follows.

The run waits on the model in a worker that is then killed. A replacement
worker (another process: the shared hosts are pooled) resumes the same run
from its checkpoint. These tests read the chat WebSocket the way the frontend
does and check that the client

* notices the ``paused`` -> ``running`` -> ``completed`` transitions (through
  the 5 s ``task_stream_snapshot`` reconciliation and the live frames),
* receives LIVE frames of the resumed run, which another process publishes
  through Redis, and
* gets the same run's frames after reconnecting to a replacement web host.

Backoff and polling are shortened so a resume is due about a second after the
lease (10s) expires.
"""

from __future__ import annotations

import json
import time
from typing import Any

import pytest

from tests.e2e.shared_execution_harness import (
    SETTLE_TIMEOUT,
    create_task,
    kill_host,
    model_calls,
    receive_event,
    task_state,
    wait_for,
    ws_url,
)

pytestmark = pytest.mark.e2e


@pytest.fixture(autouse=True)
def _fast_auto_resume(monkeypatch):
    # Read by the hosts the shared_app fixture spawns after this.
    monkeypatch.setenv("XAGENT_TASK_AUTO_RESUME_SHORT_INITIAL_BACKOFF_SECONDS", "1")
    monkeypatch.setenv("XAGENT_TASK_AUTO_RESUME_POLL_SECONDS", "1")
    monkeypatch.delenv("XAGENT_TASK_AUTO_RESUME_ENABLED", raising=False)
    monkeypatch.delenv("XAGENT_TASK_INFRA_FAILURE_PAUSE_ENABLED", raising=False)


def _read_until(ws, app, done, timeout: float = SETTLE_TIMEOUT) -> list[dict[str, Any]]:
    """Frames, in order, up to and including the first one ``done`` accepts."""
    frames: list[dict[str, Any]] = []
    deadline = time.monotonic() + timeout
    while (remaining := deadline - time.monotonic()) > 0:
        try:
            frame = json.loads(ws.recv(timeout=remaining))
        except TimeoutError:
            break
        frames.append(frame)
        if done(frame):
            return frames
    pytest.fail(
        f"Timed out; frame types: {[_describe(f) for f in frames]}\n{app.diagnostics()}"
    )


def _dump(name: str, frames: list[dict[str, Any]]) -> None:
    import os

    out = os.environ.get("WS_SPIKE_DUMP")
    if out:
        with open(f"{out}/{name}.txt", "w") as handle:
            handle.write("\n".join(_describe(f) for f in frames))


def _describe(frame: dict[str, Any]) -> str:
    kind = frame.get("type")
    if kind == "task_stream_snapshot":
        return f"snapshot:{frame.get('status')}/{frame.get('control_state')}"
    event = frame.get("event_type") or (frame.get("data") or {}).get("event_type")
    extra = f"/{event}" if event else ""
    stream = (
        f"[{str(frame.get('stream_attempt_id'))[:4]}]"
        if frame.get("stream_run_id") is not None
        else ""
    )
    return f"{kind}{extra}{stream}@{frame.get('control_state')}"


def _is_completed_snapshot(frame: dict[str, Any]) -> bool:
    return (
        frame.get("type") == "task_stream_snapshot"
        and frame.get("status") == "completed"
    ) or frame.get("type") == "task_completed"


def _start_gated_run(app, ws, task_id: int) -> str:
    receive_event(ws, "historical_data_complete")
    ws.send(json.dumps({"type": "execute_task"}))
    receive_event(ws, "execution_started")
    wait_for(lambda: (app.root / "model-entered").exists(), app, "model call")
    return task_state(task_id)["run_id"]


def _control_states(frames: list[dict[str, Any]]) -> list[str]:
    """The control states the frames announced, consecutive repeats dropped."""
    states: list[str] = []
    for frame in frames:
        state = frame.get("control_state")
        if state and (not states or states[-1] != state):
            states.append(state)
    return states


def _attempts(frames: list[dict[str, Any]]) -> list[str]:
    """Attempt ids of the frames stamped with one, in order of first sight."""
    seen: list[str] = []
    for frame in frames:
        attempt = frame.get("stream_attempt_id")
        if attempt and attempt not in seen:
            seen.append(attempt)
    return seen


def test_open_socket_sees_the_resumed_run_on_a_replacement_worker(shared_app):
    """Web host survives, the worker is replaced: one socket sees it all."""
    from websockets.sync.client import connect

    app = shared_app
    task_id = create_task(app, "WS resume", "e2e:gate")
    with connect(ws_url(app, task_id), additional_headers=app.headers) as ws:
        run_id = _start_gated_run(app, ws, task_id)
        kill_host(app, app.processes[1])
        (app.root / "model-release").touch()
        app.start("worker")
        frames = _read_until(ws, app, _is_completed_snapshot)

    _dump("open_socket", frames)
    kinds = [_describe(frame) for frame in frames]
    # The client is told about the pause, the resume and the end.
    states = _control_states(frames)
    assert states[-3:] == ["paused", "running", "completed"], kinds
    assert any(f.get("type") == "task_resumed" for f in frames), kinds
    # Live frames of the resumed attempt were published by another process
    # through Redis and reached this socket: same run, a new attempt.
    attempts = _attempts(frames)
    assert len(attempts) == 2, kinds
    resumed = [f for f in frames if f.get("stream_attempt_id") == attempts[1]]
    assert {f["stream_run_id"] for f in resumed} == {run_id}
    assert any(f.get("type") == "final_answer_end" for f in resumed), kinds
    assert task_state(task_id)["run_id"] == run_id
    assert len(model_calls(app)) == 2


def test_reconnect_to_a_replacement_web_host_streams_the_resumed_run(shared_app):
    """Everything restarts; the client reconnects to the new web host."""
    from websockets.sync.client import connect

    app = shared_app
    task_id = create_task(app, "WS reconnect", "e2e:gate")
    with connect(ws_url(app, task_id), additional_headers=app.headers) as ws:
        run_id = _start_gated_run(app, ws, task_id)
        for process in list(app.processes):
            kill_host(app, process)
    (app.root / "model-release").touch()
    app.start_all("web", "worker")

    # What the frontend's reconnect does: a fresh socket to the new host.
    with connect(ws_url(app, task_id), additional_headers=app.headers) as ws:
        frames = _read_until(ws, app, _is_completed_snapshot)

    _dump("reconnect", frames)
    kinds = [_describe(frame) for frame in frames]
    # The persisted history of the killed attempt is replayed first.
    replayed = next(i for i, k in enumerate(kinds) if "historical_data_complete" in k)
    assert any("user_message" in k for k in kinds[:replayed]), kinds
    # Then the live frames of the resumed run, from another process.
    states = _control_states(frames[replayed:])
    assert states[-3:] == ["paused", "running", "completed"], kinds
    resumed = [f for f in frames[replayed:] if f.get("stream_run_id") is not None]
    assert resumed, kinds
    assert {f["stream_run_id"] for f in resumed} == {run_id}
    assert any(f.get("type") == "final_answer_end" for f in resumed), kinds
    assert task_state(task_id)["run_id"] == run_id
    assert len(model_calls(app)) == 2
