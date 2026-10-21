"""The host release gate seam: final fence, claim modes, idle release, permits.

A recording in-memory gate stands in for the embedding host's release policy;
the engine never sees the host's tables. These tests pin where the engine calls
the gate and what it does with a refusal. The host's real policy, on
PostgreSQL, is exercised by the embedding application's own suite.
"""

# Pytest fixture imports are intentionally shadowed by test parameters.
# ruff: noqa: F811

from __future__ import annotations

import asyncio
import contextvars
import logging
from typing import Any

import pytest
from sqlalchemy import select

from tests.shared.async_waits import eventually
from tests.web.services.test_dispatch_cap import (  # noqa: F401
    CapHost,
    cap_host,
    dispatch,
    set_limit,
)
from tests.web.services.test_dispatch_cap_idle_release import (
    add_command,
    lease_runner,
    record_idle_outcomes,
)
from xagent.web.models.task import Task, TaskStatus
from xagent.web.services import task_command_transport as transport
from xagent.web.services import task_coordinator_runtime as runtime
from xagent.web.services import task_release_gate as gate_module
from xagent.web.services.task_admission_execution import (
    AdmissionWaiting,
    admission_execution,
    allow_injected_guidance,
)
from xagent.web.services.task_command_transport import TaskCommandKind
from xagent.web.services.task_coordinator_service import (
    begin_task_execution_no_commit,
)
from xagent.web.services.task_execution_controller import task_control_snapshot
from xagent.web.services.task_lease_service import acquire_task_lease_no_commit
from xagent.web.services.task_release_gate import (
    ClaimMode,
    ExecutionAcquisition,
    ExecutionClosed,
    admit_execution,
    current_claim_mode,
    execution_permit,
    reserve_execution_permit_isolated,
)


class RecordingGate:
    """An in-memory host policy that records every call the engine makes."""

    def __init__(self) -> None:
        self.mode = ClaimMode.OPEN
        self.refuse = False
        self.fail_release = False
        self.admitted: list[ExecutionAcquisition] = []
        self.reserved: list[tuple[int, str | None]] = []
        self.released: list[tuple[int, str | None]] = []

    def claim_mode(self) -> ClaimMode:
        return self.mode

    def admit(self, db: Any, acquisition: ExecutionAcquisition) -> None:
        self.admitted.append(acquisition)
        if self.refuse:
            raise ExecutionClosed("new executions are closed")

    def reserve(self, db: Any, task_id: int, run_id: str | None) -> object:
        if self.refuse:
            raise ExecutionClosed("new executions are closed")
        self.reserved.append((task_id, run_id))
        return len(self.reserved)

    def ownership_released(self, db: Any, task_id: int) -> None:
        if self.fail_release:
            raise RuntimeError("host settlement failed")
        # Seen inside the releasing transaction: the owner is already cleared.
        runner = db.execute(select(Task.runner_id).where(Task.id == task_id)).scalar()
        self.released.append((task_id, runner))


@pytest.fixture
def gate():
    installed = RecordingGate()
    gate_module.install_release_gate(installed)
    try:
        yield installed
    finally:
        gate_module.uninstall_release_gate(installed)


def task_row(host: CapHost, task_id: int) -> tuple[Any, ...]:
    with host.factory() as db:
        task = db.get(Task, task_id)
        return task.status, task.runner_id, task.run_id


# ------------------------------------------------------------------ no gate


async def test_without_a_gate_nothing_is_called_and_claims_are_open(
    cap_host: CapHost,
) -> None:
    assert gate_module.installed_release_gate() is None
    assert current_claim_mode() is ClaimMode.OPEN
    assert (
        cap_host.manager.dispatch_cap_snapshot(
            runtime.build_dispatch_cap_snapshot().policy
        ).claims
        is ClaimMode.OPEN
    )
    assert reserve_execution_permit_isolated(1, "run") is None
    task_id = cap_host.new_task(status=TaskStatus.PENDING)
    with cap_host.factory() as db, db.begin():
        assert acquire_task_lease_no_commit(db, task_id, runner_id="w") is not None
    assert task_row(cap_host, task_id)[:2] == (TaskStatus.RUNNING, "w")


def test_only_one_gate_per_process(gate: RecordingGate) -> None:
    with pytest.raises(RuntimeError):
        gate_module.install_release_gate(RecordingGate())
    gate_module.install_release_gate(gate)  # the same gate again is harmless


# --------------------------------------------------------------- final fence


async def test_fence_runs_without_command_context_and_rolls_back(
    cap_host: CapHost, gate: RecordingGate
) -> None:
    task_id = cap_host.new_task(status=TaskStatus.PENDING)
    gate.refuse = True
    with pytest.raises(ExecutionClosed) as refused:
        with cap_host.factory() as db, db.begin():
            acquire_task_lease_no_commit(db, task_id, runner_id="w", new_run=True)
    assert isinstance(refused.value, AdmissionWaiting)
    [acquisition] = gate.admitted
    assert (acquisition.task_id, acquisition.continuation) == (task_id, False)
    assert acquisition.coordinated is False and acquisition.permit is None
    # The RUNNING write was rolled back with the refusal.
    assert task_row(cap_host, task_id) == (TaskStatus.PENDING, None, None)


async def test_fence_precedes_the_guidance_exemption(
    cap_host: CapHost, gate: RecordingGate
) -> None:
    """Confirmed guidance skips admission tickets, never the release fence."""
    task_id = cap_host.new_task(status=TaskStatus.RUNNING, run_id="run-1")
    with cap_host.factory() as db, db.begin():
        db.get(Task, task_id).runner_id = "w"
    gate.refuse = True
    with admission_execution(41, task_id, governed=True):
        allow_injected_guidance(task_id, "run-1")
        with pytest.raises(ExecutionClosed):
            with cap_host.factory() as db, db.begin():
                acquire_task_lease_no_commit(
                    db, task_id, runner_id="w", expected_run_id="run-1"
                )
        assert gate.admitted[-1].continuation is True
        gate.refuse = False
        with cap_host.factory() as db, db.begin():
            lease = acquire_task_lease_no_commit(
                db, task_id, runner_id="w", expected_run_id="run-1"
            )
        assert lease is not None and lease.run_id == "run-1"
    # Guidance for another run is not a continuation of this one.
    with admission_execution(42, task_id, governed=False):
        with cap_host.factory() as db, db.begin():
            acquire_task_lease_no_commit(
                db, task_id, runner_id="w", expected_run_id="run-1"
            )
    assert gate.admitted[-1].continuation is False


async def test_guidance_is_a_continuation_without_team_admission(
    cap_host: CapHost, gate: RecordingGate
) -> None:
    """Joining a live run is flagged whether or not admission governs the command."""
    task_id = cap_host.new_task(status=TaskStatus.RUNNING, run_id="run-1")
    with cap_host.factory() as db, db.begin():
        db.get(Task, task_id).runner_id = "w"

    def join_and_acquire(run_id: str) -> list[bool]:
        with admission_execution(43, task_id, governed=False):
            allow_injected_guidance(task_id, run_id)
            for _ in range(2):
                with cap_host.factory() as db, db.begin():
                    acquire_task_lease_no_commit(
                        db, task_id, runner_id="w", expected_run_id="run-1"
                    )
        return [a.continuation for a in gate.admitted[-2:]]

    # Single use: only the acquisition that continues the join is flagged.
    assert contextvars.copy_context().run(join_and_acquire, "run-1") == [True, False]
    # Guidance confirmed for another run never flags this one.
    assert contextvars.copy_context().run(join_and_acquire, "run-0") == [False, False]


async def test_entering_a_kept_run_is_always_new(
    cap_host: CapHost, gate: RecordingGate
) -> None:
    """A resume keeps its run id; the row was resting, so it is a new execution."""
    task_id = cap_host.new_task(
        status=TaskStatus.PAUSED, run_id="run-1", control_state="paused"
    )
    coordinator = await cap_host.registry.ensure(task_id)
    assert coordinator is not None and coordinator.lease is not None
    gate.refuse = True
    with pytest.raises(ExecutionClosed):
        with cap_host.factory() as db, db.begin():
            begin_task_execution_no_commit(
                db,
                coordinator.lease,
                expected=task_control_snapshot(db.get(Task, task_id)),
                new_run=False,
            )
    [acquisition] = gate.admitted
    assert (acquisition.run_id, acquisition.continuation) == ("run-1", False)
    assert acquisition.coordinated is True
    assert task_row(cap_host, task_id)[0] == TaskStatus.PAUSED


# ------------------------------------------------------------------- permits


async def test_a_permit_reaches_one_detached_acquisition(
    cap_host: CapHost, gate: RecordingGate
) -> None:
    task_id = cap_host.new_task(status=TaskStatus.PENDING)
    permit = reserve_execution_permit_isolated(task_id, "run-1")
    assert permit is not None and gate.reserved == [(task_id, "run-1")]

    def admit_twice() -> None:
        with cap_host.factory() as db:
            admit_execution(db, task_id, "run-1", continuation=False, coordinated=True)
            admit_execution(db, task_id, "run-1", continuation=False, coordinated=True)

    with execution_permit(permit):
        detached = asyncio.create_task(asyncio.to_thread(admit_twice))
    await detached
    assert [a.permit for a in gate.admitted] == [permit, None]
    assert permit.used
    # Another task never sees it.
    other = reserve_execution_permit_isolated(task_id, "run-1")
    with execution_permit(other):
        with cap_host.factory() as db:
            admit_execution(db, task_id + 1, "x", continuation=False, coordinated=True)
    assert gate.admitted[-1].permit is None


def test_a_refused_reservation_is_an_admission_wait(
    cap_host: CapHost, gate: RecordingGate
) -> None:
    gate.refuse = True
    with pytest.raises(AdmissionWaiting):
        reserve_execution_permit_isolated(1, "run-1")


# -------------------------------------------------------------------- claims


async def test_execution_closed_claims_controls_and_live_guidance_only(
    cap_host: CapHost, gate: RecordingGate, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = cap_host
    set_limit(monkeypatch, 10)
    gate.mode = ClaimMode.EXECUTION_CLOSED
    idle = host.new_task()
    start = host.enqueue(idle, "start")
    running = host.new_task()
    host.live(running)
    guidance = host.enqueue(running, "message")
    controlled = host.new_task()
    host.enqueue(controlled, "cancel")

    while await dispatch(host):
        pass

    assert sorted(host.claimed) == sorted(
        [(running, TaskCommandKind.MESSAGE), (controlled, TaskCommandKind.CANCEL)]
    )
    assert host.row(start).status == "pending"
    assert host.row(start).attempt_count == 0
    assert host.row(guidance).status == "completed"


async def test_closed_claims_nothing(
    cap_host: CapHost, gate: RecordingGate, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = cap_host
    set_limit(monkeypatch, 10)
    gate.mode = ClaimMode.CLOSED
    running = host.new_task()
    host.live(running)
    ids = [
        host.enqueue(running, "message"),
        host.enqueue(host.new_task(), "cancel"),
        host.enqueue(host.new_task(), "start"),
    ]
    assert await dispatch(host) is False
    assert host.claimed == []
    assert [host.row(i).status for i in ids] == ["pending"] * 3
    assert transport.task_command_dispatch_quiescent()
    # Reopening restores the legacy claims.
    gate.mode = ClaimMode.OPEN
    while await dispatch(host):
        pass
    assert len(host.claimed) == 3


async def test_a_control_still_overtakes_held_new_work(
    cap_host: CapHost, gate: RecordingGate, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The closed-cap overtaking rule applies to a release drain unchanged."""
    host = cap_host
    set_limit(monkeypatch, 10)
    gate.mode = ClaimMode.EXECUTION_CLOSED
    task_id = host.new_task()
    host.enqueue(task_id, "start")
    cancel = host.enqueue(task_id, "cancel")
    cap = runtime.build_dispatch_cap_snapshot()
    assert cap.closed and cap.claims is ClaimMode.EXECUTION_CLOSED
    candidate = await asyncio.to_thread(
        transport._find_command_candidate, None, (), cap
    )
    assert candidate is not None and candidate.id == cancel


async def test_release_drain_is_not_reported_as_time_at_the_cap(
    cap_host: CapHost, gate: RecordingGate, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = cap_host
    set_limit(monkeypatch, 10)
    gate.mode = ClaimMode.EXECUTION_CLOSED
    clock = [0.0]
    host.manager._cap_clock = lambda: clock[0]
    assert await dispatch(host) is False
    clock[0] = 5.0
    assert host.manager.drain_gate_held_seconds(5.0) == 0.0


# ------------------------------------------------- refusal keeps budgets intact


async def test_refused_execution_waits_beyond_the_defer_budget(
    cap_host: CapHost, gate: RecordingGate, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Release parking spends neither failure nor deferral budget."""
    host = cap_host
    set_limit(monkeypatch, 10)
    monkeypatch.setattr(transport, "max_command_defers", lambda: 3)
    task_id = host.new_task()
    command_id = host.enqueue(task_id, "start")

    async def refused(command: Any) -> None:
        host.claimed.append((command.task_id, command.kind))
        raise ExecutionClosed("new executions are closed")

    for attempt in range(1, 8):
        with host.factory() as db, db.begin():
            # Skip the capped backoff; only the accounting is under test.
            db.get(type(host.row(command_id)), command_id).retry_available_at = None
        assert await asyncio.wait_for(transport.dispatch_one_task_command(refused), 30)
        row = host.row(command_id)
        assert (row.status, row.attempt_count) == ("pending", attempt)
        assert (row.defer_count, row.failure_count) == (0, 0)
    assert len(host.claimed) == 7 > transport.max_command_defers()


# --------------------------------------------------------------- idle release

IDLE_CASES = [
    # name, claims, task status, rows (kind, status, attempts), outcome
    (
        "requeued_reply_blocks_a_control",
        ClaimMode.EXECUTION_CLOSED,
        TaskStatus.WAITING_FOR_USER,
        [("resume_input", "pending", 1), ("cancel", "pending", 0)],
        "released",
    ),
    (
        "claimable_control",
        ClaimMode.EXECUTION_CLOSED,
        TaskStatus.WAITING_FOR_USER,
        [("cancel", "pending", 0)],
        "busy",
    ),
    (
        "control_overtakes_never_claimed_start",
        ClaimMode.EXECUTION_CLOSED,
        TaskStatus.PENDING,
        [("start", "pending", 0), ("cancel", "pending", 0)],
        "busy",
    ),
    (
        "held_reply_only",
        ClaimMode.EXECUTION_CLOSED,
        TaskStatus.WAITING_FOR_USER,
        [("resume_input", "pending", 0)],
        "released",
    ),
    (
        "closed_control",
        ClaimMode.CLOSED,
        TaskStatus.PAUSED,
        [("pause", "pending", 0), ("resume", "pending", 0)],
        "released",
    ),
    (
        "closed_processing",
        ClaimMode.CLOSED,
        TaskStatus.PAUSED,
        [("cancel", "processing", 1)],
        "busy",
    ),
    (
        "legacy_open_keeps_the_lease",
        ClaimMode.OPEN,
        TaskStatus.WAITING_FOR_USER,
        [("resume_input", "pending", 1), ("cancel", "pending", 0)],
        "busy",
    ),
]


@pytest.mark.parametrize(
    "claims,status,rows,expected",
    [case[1:] for case in IDLE_CASES],
    ids=[case[0] for case in IDLE_CASES],
)
async def test_idle_release_under_a_drain(
    cap_host: CapHost,
    gate: RecordingGate,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    claims: ClaimMode,
    status: TaskStatus,
    rows: list[tuple[str, str, int]],
    expected: str,
) -> None:
    host = cap_host
    set_limit(monkeypatch, 10)
    gate.mode = claims
    target = host.new_task(status=status)
    ids = [add_command(host, target, k, status=s, attempts=a) for k, s, a in rows]
    outcomes = record_idle_outcomes(monkeypatch)
    caplog.set_level(logging.INFO, logger=runtime.logger.name)

    coordinator = await host.registry.ensure(target)
    assert coordinator is not None
    coordinator._ensure_idle_check()
    await eventually(lambda: outcomes)

    assert outcomes[0] == expected
    if expected == "released":
        assert lease_runner(host, target) is None
        # Settled in the releasing transaction, after the owner was cleared.
        assert gate.released == [(target, None)]
        assert [
            r.getMessage() for r in caplog.records if "release drain" in r.getMessage()
        ]
    else:
        assert lease_runner(host, target) == "worker"
        assert gate.released == []
    # Nothing was failed, dropped or claimed by the release.
    assert [host.row(i).status for i in ids] == [s for _, s, _ in rows]


async def test_failed_host_settlement_keeps_the_owner(
    cap_host: CapHost, gate: RecordingGate, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = cap_host
    gate.fail_release = True
    target = host.new_task(status=TaskStatus.COMPLETED)
    coordinator = await host.registry.ensure(target)
    assert coordinator is not None
    coordinator._ensure_idle_check()
    await eventually(lambda: coordinator.state is runtime.CoordinatorState.CLOSED)
    # The release rolled back with the settlement; recovery owns the row now.
    assert lease_runner(host, target) == "worker"
    assert gate.released == []


async def test_only_an_uninterrupted_close_settles(
    cap_host: CapHost, gate: RecordingGate
) -> None:
    host = cap_host
    idle = host.new_task(status=TaskStatus.COMPLETED)
    add_command(host, idle, "cancel")  # keeps the owner busy until close
    busy = host.new_task(status=TaskStatus.PAUSED)
    owners = [await host.registry.ensure(t) for t in (idle, busy)]
    live = asyncio.create_task(asyncio.Event().wait())
    host.held.append(live)
    owners[1].track_execution(live)

    await owners[0].close()
    await owners[1].close()

    assert lease_runner(host, idle) is None and lease_runner(host, busy) is None
    # The close that cancelled live work released without settling it.
    assert gate.released == [(idle, None)]
