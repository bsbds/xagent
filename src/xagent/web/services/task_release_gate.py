"""Host release gate: whether this process may acquire a new execution.

An embedding host that drains a worker before stopping it installs one
:class:`ExecutionReleaseGate` per process. Without one, nothing here changes
behavior. The engine never decides release policy itself; it only calls the
gate at the points where that decision must be atomic:

* **Final fence.** Every write that moves a task into RUNNING calls
  :func:`admit_execution` in the same transaction, after its UPDATE matched and
  before any admission early return (no command context, injected guidance).
  A refusal raises :class:`ExecutionClosed`, an :class:`AdmissionWaiting`, so a
  durable command is returned to its queue without spending failure or defer
  budget, and the caller's transaction rolls back.
* **Detached acquisitions.** Some handlers write durable state (a control
  transition, a delivery claim) and enter RUNNING later in a background task.
  They reserve an :class:`ExecutionPermit` before their first such write, so a
  refusal happens while nothing has been written; the background acquisition
  then presents that permit to the final fence.
* **Claims.** The dispatcher reads :func:`current_claim_mode`, a cheap cached
  view, to avoid claiming commands it would only return. It is a hint: the
  final fence is authoritative.
* **Ownership.** A task owner's release calls :func:`release_ownership` in the
  transaction that clears the owner, so the host can settle what this process
  owed for the task together with the ownership itself.

A continuation is admitted only for confirmed injected guidance into the exact
run being acquired, and the host still has to prove that this process holds
that run's execution; a task id alone never grants anything.
"""

from __future__ import annotations

import enum
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy.orm import Session

from .task_admission_execution import AdmissionWaiting


class ClaimMode(str, enum.Enum):
    """Which commands this process should still claim from the durable queue."""

    #: Everything, subject to the existing dispatch cap and admission.
    OPEN = "open"
    #: No new execution: controls, and commands of tasks this process already
    #: runs (guidance for a live run), as at a closed dispatch cap.
    EXECUTION_CLOSED = "execution_closed"
    #: Nothing at all.
    CLOSED = "closed"


class ExecutionClosed(AdmissionWaiting):
    """The host refused a new execution; return the command to its queue."""


@dataclass
class ExecutionPermit:
    """A host reservation for one detached acquisition of one task.

    ``token`` is host-defined. A permit is presented once: after the final
    fence accepted it, a later acquisition in the same context must stand on
    its own.
    """

    task_id: int
    token: object
    used: bool = False


@dataclass(frozen=True)
class ExecutionAcquisition:
    """What the final fence knows about the execution being entered.

    ``continuation`` is true only for confirmed injected guidance continuing
    exactly ``run_id``. ``coordinated`` says whether this process's task owner
    holds the acquisition. ``permit`` is the unused permit bound for this task,
    if any.
    """

    task_id: int
    run_id: str
    continuation: bool
    coordinated: bool
    permit: ExecutionPermit | None


class ExecutionReleaseGate(Protocol):
    def claim_mode(self) -> ClaimMode:
        """The cached claim view; must not block or touch the database."""
        ...

    def admit(self, db: Session, acquisition: ExecutionAcquisition) -> None:
        """Admit or refuse (``ExecutionClosed``) in the RUNNING transaction."""
        ...

    def reserve(self, db: Session, task_id: int, run_id: str | None) -> object:
        """Reserve a detached acquisition; the caller commits ``db``."""
        ...

    def ownership_released(self, db: Session, task_id: int) -> None:
        """Called in the transaction that releases this process's task owner."""
        ...


@dataclass
class _JoinedRun:
    task_id: int
    run_id: str
    used: bool = False


_gate: ExecutionReleaseGate | None = None
_permit: ContextVar[ExecutionPermit | None] = ContextVar(
    "xagent_execution_permit", default=None
)
_joined: ContextVar[_JoinedRun | None] = ContextVar("xagent_joined_run", default=None)


def install_release_gate(gate: ExecutionReleaseGate) -> None:
    """Install the process's gate. One gate per process; never replaced."""
    global _gate
    if _gate is not None and _gate is not gate:
        raise RuntimeError("An execution release gate is already installed")
    _gate = gate


def uninstall_release_gate(gate: ExecutionReleaseGate) -> None:
    global _gate
    if _gate is gate:
        _gate = None


def installed_release_gate() -> ExecutionReleaseGate | None:
    return _gate


def current_claim_mode() -> ClaimMode:
    gate = _gate
    return ClaimMode.OPEN if gate is None else gate.claim_mode()


def mark_joined_run(task_id: int, run_id: str) -> None:
    """Record that guidance was confirmed injected into this live run.

    Set where the injection is confirmed (``allow_injected_guidance``) for the
    rest of the command's context, which the detached continuation copies,
    whether or not team admission governs the command.
    """
    _joined.set(_JoinedRun(task_id, run_id))


def claim_joined_run(task_id: int, run_id: str) -> bool:
    """Whether this acquisition continues the confirmed join; single use."""
    joined = _joined.get()
    if (
        joined is None
        or joined.used
        or (joined.task_id, joined.run_id)
        != (
            task_id,
            run_id,
        )
    ):
        return False
    joined.used = True
    return True


def admit_execution(
    db: Session,
    task_id: int,
    run_id: str,
    *,
    continuation: bool,
    coordinated: bool,
) -> None:
    """The final fence; a no-op without an installed gate."""
    gate = _gate
    if gate is None:
        return
    permit = _permit.get()
    if permit is not None and (permit.task_id != task_id or permit.used):
        permit = None
    gate.admit(
        db,
        ExecutionAcquisition(
            task_id=task_id,
            run_id=run_id,
            continuation=continuation,
            coordinated=coordinated,
            permit=permit,
        ),
    )
    if permit is not None:
        permit.used = True


def reserve_execution_permit_isolated(
    task_id: int, run_id: str | None
) -> ExecutionPermit | None:
    """Reserve a permit in its own committed transaction; None without a gate.

    Raises :class:`ExecutionClosed` when the host refuses, before the caller
    has written anything for the acquisition.
    """
    gate = _gate
    if gate is None:
        return None
    from ..models.database import get_session_local

    with get_session_local()() as db, db.begin():
        token = gate.reserve(db, task_id, run_id)
    return ExecutionPermit(task_id=task_id, token=token)


@contextmanager
def execution_permit(permit: ExecutionPermit | None) -> Iterator[None]:
    """Bind ``permit`` for work created inside the block (tasks copy it)."""
    token = _permit.set(permit)
    try:
        yield
    finally:
        _permit.reset(token)


def release_ownership(db: Session, task_id: int) -> None:
    gate = _gate
    if gate is not None:
        gate.ownership_released(db, task_id)
