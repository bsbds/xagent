"""A run a host shutdown cancels is paused at once and resumed elsewhere.

With ``XAGENT_SHUTDOWN_HANDOFF_ENABLED`` on, the cancelled run of an eligible
task settles as a ``shutdown`` interruption (``_schedule_bg`` ->
``settle_task_lease_isolated``): PAUSED, its lease released, an automatic
resume scheduled with no backoff. Nothing is left for lease-TTL recovery to
find. The orchestrator-level routing (which cancellations ask for the
interruption) is pinned in ``test_task_orchestrator.py``; this file runs the
real settlement. Runs on SQLite and, when ``XAGENT_TEST_POSTGRES_URL`` is set,
on PostgreSQL through the shared ``canonical`` fixture.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests.web.services.test_persistence_failure_pause import (
    _clear_bg_manager,
    _late_bound_sessions,
    _prepare_run,
    _projected_task,
    _projection,
    _resume_raising,
    _state,
    canonical,
    engine,
    task_id,
)
from xagent.core.agent.interruption import InterruptionReason
from xagent.web.models.task import Task, TaskStatus
from xagent.web.models.trigger import TriggerRunStatus
from xagent.web.services import task_coordinator_runtime
from xagent.web.services import task_orchestrator as orchestrator
from xagent.web.services.release_hold import release_hold
from xagent.web.services.task_auto_resume import run_auto_resume_tick
from xagent.web.services.task_coordinator_service import TaskLease as OwnerLease
from xagent.web.services.task_coordinator_service import (
    release_task_lease_no_commit as release_owner_lease_no_commit,
)
from xagent.web.services.task_execution import _acquire_resume_task_lease
from xagent.web.services.task_lease_service import TaskLease
from xagent.web.services.task_orchestrator import (
    TaskTurnPayload,
    settle_task_lease_isolated,
)
from xagent.web.services.task_runtime import (
    MCP_RUNTIME_AUTHORIZATION_POLICY_REQUIRED_KEY,
)

# Fixtures the suite reuses; imported names pytest collects.
_FIXTURES = (
    _clear_bg_manager,
    _late_bound_sessions,
    canonical,
    engine,
    task_id,
)

HANDOFF_SWITCH = "XAGENT_SHUTDOWN_HANDOFF_ENABLED"
PAUSE_SWITCH = "XAGENT_TASK_INFRA_FAILURE_PAUSE_ENABLED"
AUTO_RESUME_SWITCH = "XAGENT_TASK_AUTO_RESUME_ENABLED"
# The jitter cap of the ``shutdown`` policy.
SHUTDOWN_JITTER = timedelta(seconds=5)


@pytest.fixture(autouse=True)
def _handoff_on(monkeypatch):
    """The host is shutting down, and every switch the handoff needs is on."""
    monkeypatch.setenv(HANDOFF_SWITCH, "true")
    monkeypatch.setenv(AUTO_RESUME_SWITCH, "true")
    # Unset, the pause switch follows automatic resume: the default under test.
    monkeypatch.delenv(PAUSE_SWITCH, raising=False)
    monkeypatch.setattr(orchestrator, "_host_shutdown_in_progress", lambda: True)


def _utc(value: datetime) -> datetime:
    """SQLite returns naive datetimes; the policy writes UTC."""
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


async def _shutdown_cancel_turn(
    ids: dict[str, Any], lease: TaskLease, *, source: str = "trigger"
) -> list[dict[str, Any]]:
    """Run one scheduled turn that the host shutdown cancels mid-execution.

    Returns every published frame.
    """
    frames: list[dict[str, Any]] = []

    async def publish(event, _task_id):
        frames.append(event)

    with (
        patch.object(orchestrator, "run_task_lease_heartbeat", new=AsyncMock()),
        patch.object(
            orchestrator, "load_task_setup_snapshot_sync", return_value=MagicMock()
        ),
        patch.object(
            orchestrator, "resolve_execution_scope", return_value=None, create=True
        ),
        patch(
            "xagent.web.services.task_execution.execute_task_background",
            AsyncMock(side_effect=asyncio.CancelledError()),
        ),
        patch("xagent.web.services.task_event_display.publish_task_result", publish),
        patch("xagent.web.services.task_events.publish_task_event", publish),
        patch.object(orchestrator, "_get_agent_manager", return_value=MagicMock()),
    ):
        bg_task = orchestrator._schedule_bg(
            task_id=ids["task"],
            task_owner_user_id=ids["user"],
            task_source=source,
            task_lease=lease,
            payload=TaskTurnPayload("2+2", turn_id=ids["turn_id"]),
            force_fresh=False,
            context=None,
        )
        with pytest.raises(asyncio.CancelledError):
            await bg_task
    return frames


@pytest.mark.asyncio
async def test_shutdown_cancel_pauses_releases_and_schedules_an_immediate_resume(
    canonical,
):
    factory, _tid = canonical
    ids = _projected_task(factory)
    lease = _prepare_run(factory, ids)
    unsettled_before = release_hold.unsettled_total

    frames = await _shutdown_cancel_turn(ids, lease)

    task, row, events = _state(factory, ids["task"])
    # Paused, with the lease released in the same settlement: another process
    # can take the task over without waiting for a TTL.
    assert task.status == TaskStatus.PAUSED
    assert task.control_state == "paused"
    assert task.run_id == lease.run_id
    assert (task.runner_id, task.lease_attempt_id, task.lease_expires_at) == (
        None,
        None,
        None,
    )
    assert task.error_message is None
    # Nothing was retained for TTL recovery, so a pooled release drain has no
    # unsettled lease to wait on.
    assert release_hold.unsettled_total == unsettled_before
    # Scheduled with no backoff: due within the jitter, not a backoff later.
    assert (row.reason, row.state) == ("shutdown", "scheduled")
    due_in = _utc(row.next_attempt_at) - _utc(row.interrupted_at)
    assert timedelta(0) <= due_in <= SHUTDOWN_JITTER
    assert (row.no_progress_resumes, row.total_resumes) == (0, 0)
    assert [(e.event, e.reason) for e in events] == [
        ("interrupted", "shutdown"),
        ("scheduled", "shutdown"),
    ]
    # The scheduled resume settles the TriggerRun later; it stays RUNNING.
    projection = _projection(factory, ids, lease)
    assert projection["trigger_run"] == (TriggerRunStatus.RUNNING.value, None, False)
    assert projection["workforce_run"] == ("paused", True)
    # The run may have consumed the message: "do not resend", never failed.
    assert projection["delivery"] == ["dispatched"]
    assert [frame["type"] for frame in frames] == [
        "task_paused",
        "task_recovery_update",
    ]
    assert frames[0]["interruption_reason"] == "shutdown"


@pytest.mark.asyncio
async def test_the_sweeper_resumes_a_shutdown_pause_once_it_is_due(
    canonical, engine, monkeypatch
):
    """End to end on the paused row: once it is due the sweeper dispatches it."""
    # Selects the sweeper's PostgreSQL row-lock path or its SQLite CAS path.
    monkeypatch.setattr("xagent.web.models.database._engine", engine)
    factory, _tid = canonical
    ids = _projected_task(factory)
    lease = _prepare_run(factory, ids)
    await _shutdown_cancel_turn(ids, lease)
    _task, row, _events = _state(factory, ids["task"])

    report = run_auto_resume_tick(now=_utc(row.next_attempt_at))

    assert report.dispatched == 1
    _task, row, _events = _state(factory, ids["task"])
    assert row.state == "dispatched"
    assert row.total_resumes == 1


@pytest.mark.asyncio
async def test_shutdown_cancel_without_a_usable_checkpoint_still_fails(canonical):
    """The settlement decides from the checkpoint, so a run it could not resume
    fails now rather than resting PAUSED with nothing to resume from."""
    factory, _tid = canonical
    ids = _projected_task(factory)
    lease = _prepare_run(factory, ids, checkpoint=False)

    frames = await _shutdown_cancel_turn(ids, lease)

    task, row, _events = _state(factory, ids["task"])
    assert task.status == TaskStatus.FAILED
    assert task.error_message == "task execution cancelled"
    assert task.runner_id is None
    assert (row.reason, row.state) == ("not_recoverable", "manual")
    assert "task_paused" not in [frame["type"] for frame in frames]


@pytest.mark.asyncio
async def test_shutdown_cancel_of_an_sdk_task_is_handed_off_like_any_other(canonical):
    """SDK callers read only the status, but a PAUSED task with a scheduled
    resume reads as non-terminal (``status_only_terminal_view`` reports
    ``failed`` only once automatic resume stops), and with no backoff the
    resume is seconds away: SDK tasks are paused and resumed like the rest."""
    factory, _tid = canonical
    ids = _projected_task(factory, source="sdk")
    lease = _prepare_run(factory, ids)

    await _shutdown_cancel_turn(ids, lease, source="sdk")

    task, row, _events = _state(factory, ids["task"])
    assert task.status == TaskStatus.PAUSED
    assert task.runner_id is None
    assert (row.reason, row.state) == ("shutdown", "scheduled")


@pytest.mark.asyncio
async def test_shutdown_cancel_of_an_actor_marked_task_fails_despite_a_checkpoint(
    canonical,
):
    """An actor-marked task cannot be resumed (``get_agent_for_task`` refuses to
    rebuild it), so a handoff would leave it PAUSED behind resumes that cannot
    start; it fails now, as before."""
    factory, _tid = canonical
    ids = _projected_task(factory)
    lease = _prepare_run(factory, ids)
    with factory() as db:
        task = db.get(Task, ids["task"])
        task.agent_config = {
            **task.agent_config,
            MCP_RUNTIME_AUTHORIZATION_POLICY_REQUIRED_KEY: True,
        }
        db.commit()

    await _shutdown_cancel_turn(ids, lease)

    task, row, _events = _state(factory, ids["task"])
    assert task.status == TaskStatus.FAILED
    assert task.runner_id is None
    assert (row.reason, row.state, row.state_detail) == (
        "shutdown",
        "ineligible",
        "ineligible:actor_marked",
    )


@pytest.mark.asyncio
async def test_shutdown_cancel_fails_a_task_automatic_recovery_excludes(canonical):
    """Eligibility is the shared decision's: an a2a task fails now, with the
    row saying why, though it has a checkpoint."""
    factory, _tid = canonical
    ids = _projected_task(factory, source="a2a")
    lease = _prepare_run(factory, ids)

    await _shutdown_cancel_turn(ids, lease, source="a2a")

    task, row, _events = _state(factory, ids["task"])
    assert task.status == TaskStatus.FAILED
    assert (row.reason, row.state, row.state_detail) == (
        "shutdown",
        "ineligible",
        "ineligible:source_a2a",
    )


@pytest.mark.asyncio
async def test_shutdown_cancel_fails_when_automatic_resume_is_off(
    canonical, monkeypatch
):
    """Nothing would resume the paused run, so it fails as before."""
    monkeypatch.setenv(AUTO_RESUME_SWITCH, "false")
    factory, _tid = canonical
    ids = _projected_task(factory)
    lease = _prepare_run(factory, ids)

    await _shutdown_cancel_turn(ids, lease)

    task, row, _events = _state(factory, ids["task"])
    assert task.status == TaskStatus.FAILED
    assert row.state != "scheduled"


@pytest.mark.asyncio
@pytest.mark.parametrize("pause_switch", ["false", "true"])
async def test_shutdown_pause_does_not_follow_the_infra_failure_switch(
    canonical, monkeypatch, pause_switch
):
    """A host shutdown is planned, not an infrastructure failure:
    ``XAGENT_SHUTDOWN_HANDOFF_ENABLED`` is its opt-in, so only automatic resume
    has to be on, whatever ``XAGENT_TASK_INFRA_FAILURE_PAUSE_ENABLED`` says."""
    monkeypatch.setenv(PAUSE_SWITCH, pause_switch)
    factory, _tid = canonical
    ids = _projected_task(factory)
    lease = _prepare_run(factory, ids)

    await _shutdown_cancel_turn(ids, lease)

    task, row, _events = _state(factory, ids["task"])
    assert task.status == TaskStatus.PAUSED
    assert (row.reason, row.state) == ("shutdown", "scheduled")


@pytest.mark.asyncio
async def test_shutdown_cancel_of_a_run_the_user_paused_is_not_resumed(canonical):
    """A pause the user already asked for wins: the run rests for the user
    instead of being resumed behind their back."""
    factory, _tid = canonical
    ids = _projected_task(factory)
    lease = _prepare_run(factory, ids)
    with factory() as db:
        db.get(Task, ids["task"]).control_state = "pause_requested"
        db.commit()

    await _shutdown_cancel_turn(ids, lease)

    task, row, _events = _state(factory, ids["task"])
    assert task.status == TaskStatus.PAUSED
    assert (row.reason, row.state) == ("user_pause", "manual")


@pytest.mark.asyncio
async def test_a_shutdown_pause_lets_the_closing_coordinator_release_the_owner_lease(
    canonical, monkeypatch
):
    """Shared execution: a run settles inside its coordinator, which keeps the
    task's owner lease until it has drained every handle and releases it on
    close. Only a task that left RUNNING can be released, so the run's PAUSED
    settlement is what lets another process acquire the task at once; a run
    left RUNNING for TTL recovery would block that release."""
    factory, _tid = canonical
    ids = _projected_task(factory)
    lease = _prepare_run(factory, ids)
    owner = OwnerLease(ids["task"], lease.runner_id, lease.attempt_id)
    # Settle as the coordinator's execution does: ownership stays with it.
    monkeypatch.setattr(
        task_coordinator_runtime,
        "current_task_coordinator",
        lambda task_id: object() if task_id == ids["task"] else None,
    )

    settled = settle_task_lease_isolated(
        lease,
        error_message="task execution cancelled",
        interruption=InterruptionReason.SHUTDOWN,
    )

    assert settled
    task, _row, _events = _state(factory, ids["task"])
    assert task.status == TaskStatus.PAUSED
    assert task.runner_id == lease.runner_id
    with factory() as db:
        assert release_owner_lease_no_commit(db, owner)
        db.commit()
    task, _row, _events = _state(factory, ids["task"])
    assert (task.runner_id, task.lease_attempt_id, task.lease_expires_at) == (
        None,
        None,
        None,
    )


# --------------------------------------------------------------------------
# A resumed run is handed off like a new one
# --------------------------------------------------------------------------


def _resume_settlement(seen: list[dict[str, Any]]) -> MagicMock:
    """A resume settlement that records its arguments and reports a pause."""

    def settle(lease, **kwargs):
        seen.append(kwargs)
        interruption = kwargs["interruption"]
        if interruption is not None:
            report = kwargs["report"]
            report.paused_for = interruption
            report.control_state.update(status="paused", run_id=lease.run_id)
        return True

    return MagicMock(side_effect=settle)


@pytest.mark.asyncio
async def test_a_resumed_run_cancelled_by_a_host_shutdown_settles_as_shutdown():
    """The resume entry point asks for the same decision as a new turn: it is
    the path of a run moved to this host by an automatic resume, a manual one,
    an answered question or an injected message."""
    seen: list[dict[str, Any]] = []

    publish_pause, _frames = await _resume_raising(
        asyncio.CancelledError(), _resume_settlement(seen)
    )

    (call,) = seen
    assert call["interruption"] is InterruptionReason.SHUTDOWN
    assert call["error_message"] == "resume execution cancelled"
    publish_pause.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["switch_off", "user_cancel", "not_shutting_down"])
async def test_a_resumed_run_cancelled_for_another_reason_settles_as_before(
    monkeypatch, case
):
    if case == "switch_off":
        monkeypatch.setenv(HANDOFF_SWITCH, "false")
    if case == "not_shutting_down":
        monkeypatch.setattr(orchestrator, "_host_shutdown_in_progress", lambda: False)
    seen: list[dict[str, Any]] = []

    publish_pause, _frames = await _resume_raising(
        asyncio.CancelledError(),
        _resume_settlement(seen),
        explicit_cancel=case == "user_cancel",
    )

    (call,) = seen
    assert call["interruption"] is None
    publish_pause.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_run_shut_down_twice_is_resumed_again_and_never_failed(
    canonical, engine, monkeypatch
):
    """A rolling release: host A stops and pauses the run, the sweeper hands it
    to host B, which is stopped later in the same release. The resumed run is
    paused and scheduled again instead of failing; only ``total_resumes``
    carries over."""
    monkeypatch.setattr("xagent.web.models.database._engine", engine)
    factory, _tid = canonical
    ids = _projected_task(factory)
    lease = _prepare_run(factory, ids)

    # Host A stops.
    await _shutdown_cancel_turn(ids, lease)
    _task, row, _events = _state(factory, ids["task"])
    assert (row.reason, row.state, row.total_resumes) == ("shutdown", "scheduled", 0)

    # The sweeper hands the run to host B.
    assert run_auto_resume_tick(now=_utc(row.next_attempt_at)).dispatched == 1
    resume_lease = _acquire_resume_task_lease(
        ids["task"], ids["user"], lease.run_id, refuse_terminal_status=True
    )
    assert resume_lease is not None and resume_lease.run_id == lease.run_id

    # Host B stops while it runs the resumed run.
    await _resume_raising(
        asyncio.CancelledError(),
        None,
        task_id=ids["task"],
        owner_user_id=ids["user"],
        lease=resume_lease,
    )

    task, row, events = _state(factory, ids["task"])
    assert task.status == TaskStatus.PAUSED
    assert (task.runner_id, task.lease_expires_at) == (None, None)
    assert (row.reason, row.state, row.run_id) == (
        "shutdown",
        "scheduled",
        lease.run_id,
    )
    assert row.total_resumes == 1
    assert [e.event for e in events].count("auto_resumed") == 1
    # Due at once, so host C takes it over right away.
    assert run_auto_resume_tick(now=_utc(row.next_attempt_at)).dispatched == 1
    _task, row, _events = _state(factory, ids["task"])
    assert (row.state, row.total_resumes) == ("dispatched", 2)
