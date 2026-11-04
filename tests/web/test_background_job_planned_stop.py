"""A planned worker stop hands a running job back without costing it an attempt.

Celery's cold shutdown (``REMAP_SIGTERM=SIGQUIT``) interrupts a running job with
``SystemExit`` in the pool child and puts the unacknowledged message back on the
broker itself; these tests cover the other half, the job row. The real worker
and broker are exercised in ``test_celery_planned_stop_e2e.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from xagent.config import CELERY_BROKER_URL, CELERY_ENABLED
from xagent.web.jobs import tasks as tasks_module
from xagent.web.jobs.exceptions import BackgroundJobHandlerError
from xagent.web.models.background_job import BackgroundJob, BackgroundJobStatus
from xagent.web.models.database import get_session_local, init_db
from xagent.web.models.user import User
from xagent.web.services.background_jobs import (
    PLANNED_STOP_REQUEUE_MESSAGE,
    create_background_job,
    requeue_job_after_planned_stop,
)

JOB_TYPE = "test.planned_stop"


@pytest.fixture
def sessions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A fresh SQLite database, with Celery disabled so nothing is published."""
    monkeypatch.setenv(CELERY_ENABLED, "false")
    monkeypatch.delenv(CELERY_BROKER_URL, raising=False)
    init_db(f"sqlite:///{tmp_path / 'planned-stop.db'}")
    return get_session_local()


@pytest.fixture
def job_id(sessions) -> str:
    """A pending ``test.planned_stop`` job with the default three attempts."""
    with sessions() as db:
        user = User(username="planned-stop", password_hash="x")
        db.add(user)
        db.commit()
        job = create_background_job(
            db,
            user_id=int(user.id),
            job_type=JOB_TYPE,
            payload={},
            max_attempts=3,
        )
        return str(job.id)


def _row(sessions, job_id: str) -> BackgroundJob:
    """The job as stored now, detached from any session the code under test used."""
    with sessions() as db:
        job = db.get(BackgroundJob, job_id)
        db.expunge(job)
        return job


def _set_row(sessions, job_id: str, **values) -> None:
    with sessions() as db:
        db.query(BackgroundJob).filter(BackgroundJob.id == job_id).update(values)
        db.commit()


def _register(handler):
    tasks_module.register_background_job_handler(JOB_TYPE, handler, replace=True)


@pytest.fixture(autouse=True)
def _unregister_handler():
    yield
    tasks_module._EXTRA_HANDLERS.pop(JOB_TYPE, None)


def _interrupted_by_worker_stop(_db, _job):
    """What billiard does to a pool child that Celery's cold shutdown cancels."""
    raise SystemExit(-241)


def test_interrupted_run_is_requeued_without_consuming_an_attempt(sessions, job_id):
    _register(_interrupted_by_worker_stop)

    with pytest.raises(SystemExit):
        tasks_module.execute_background_job.apply(args=[job_id], throw=True)

    job = _row(sessions, job_id)
    assert job.status == BackgroundJobStatus.ENQUEUED.value
    assert job.attempts == 0
    assert job.error_message == PLANNED_STOP_REQUEUE_MESSAGE
    assert job.progress["message"] == PLANNED_STOP_REQUEUE_MESSAGE
    assert job.finished_at is None


def test_rerun_after_a_planned_stop_holds_the_same_attempt_number(sessions, job_id):
    """The attempt a handler sees is what it would have been without the stop."""
    seen: list[int] = []

    def handler(_db, job):
        seen.append(int(job.attempts))
        if len(seen) == 1:
            raise SystemExit(-241)
        return {"done": True}

    _register(handler)
    with pytest.raises(SystemExit):
        tasks_module.execute_background_job.apply(args=[job_id], throw=True)

    tasks_module.execute_background_job.apply(args=[job_id], throw=True)

    assert seen == [1, 1]
    job = _row(sessions, job_id)
    assert job.status == BackgroundJobStatus.SUCCEEDED.value
    assert job.attempts == 1
    assert job.error_message is None


def test_repeated_planned_stops_never_exhaust_the_attempts(sessions, job_id):
    """Releases cannot push a job to its final attempt, where failure rolls back."""
    stops_left = 5  # more than max_attempts

    def handler(_db, job):
        nonlocal stops_left
        # The ingest handlers roll back and release their target generation on
        # the final attempt; a stop must never get a job there.
        assert int(job.attempts) < int(job.max_attempts)
        if stops_left:
            stops_left -= 1
            raise SystemExit(-241)
        return {"done": True}

    _register(handler)
    for _ in range(5):
        with pytest.raises(SystemExit):
            tasks_module.execute_background_job.apply(args=[job_id], throw=True)
        assert _row(sessions, job_id).attempts == 0

    tasks_module.execute_background_job.apply(args=[job_id], throw=True)

    job = _row(sessions, job_id)
    assert job.status == BackgroundJobStatus.SUCCEEDED.value
    assert job.attempts == 1


def test_attempts_from_earlier_failures_survive_a_planned_stop(sessions, job_id):
    """Only the interrupted run's own attempt is given back."""
    _set_row(
        sessions,
        job_id,
        status=BackgroundJobStatus.ENQUEUED.value,
        attempts=1,
        error_message="earlier transient failure",
    )
    _register(_interrupted_by_worker_stop)

    with pytest.raises(SystemExit):
        tasks_module.execute_background_job.apply(args=[job_id], throw=True)

    job = _row(sessions, job_id)
    assert job.status == BackgroundJobStatus.ENQUEUED.value
    assert job.attempts == 1
    assert job.error_message == PLANNED_STOP_REQUEUE_MESSAGE


def test_failed_run_still_consumes_its_attempt(sessions, job_id):
    """Only a stop is refunded; an ordinary failure keeps counting."""

    def failing(_db, _job):
        raise BackgroundJobHandlerError("permanent failure", retryable=False)

    _register(failing)
    outcome = tasks_module.execute_background_job.apply(args=[job_id], throw=False)

    assert isinstance(outcome.result, BackgroundJobHandlerError)
    job = _row(sessions, job_id)
    assert job.status == BackgroundJobStatus.FAILED.value
    assert job.attempts == 1


def test_stop_during_a_handler_transaction_still_requeues(sessions, job_id):
    """Whatever the interrupted code left uncommitted must not block the requeue."""

    def handler(db, job):
        job.result = {"half": "written"}
        db.flush()  # holds the row, uncommitted, when the stop arrives
        raise SystemExit(-241)

    _register(handler)
    with pytest.raises(SystemExit):
        tasks_module.execute_background_job.apply(args=[job_id], throw=True)

    job = _row(sessions, job_id)
    assert job.status == BackgroundJobStatus.ENQUEUED.value
    assert job.attempts == 0
    assert job.result is None


def test_requeue_failure_does_not_stop_the_stop(sessions, job_id, monkeypatch):
    """A database that is gone must not keep the worker from exiting."""

    def broken(*_args, **_kwargs):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(tasks_module, "requeue_job_after_planned_stop", broken)
    _register(_interrupted_by_worker_stop)

    with pytest.raises(SystemExit):
        tasks_module.execute_background_job.apply(args=[job_id], throw=True)

    # Left as a crash leaves it: Celery redelivers the message and the stale
    # sweep covers a lost one.
    job = _row(sessions, job_id)
    assert job.status == BackgroundJobStatus.RUNNING.value
    assert job.attempts == 1


@pytest.mark.parametrize(
    "newer_state",
    [
        # The stale sweep took the job over and another worker claimed it.
        {"status": BackgroundJobStatus.RUNNING.value, "attempts": 2},
        # The stale sweep took the job over and it has not been claimed yet.
        {"status": BackgroundJobStatus.ENQUEUED.value, "attempts": 1},
        # The job finished or was cancelled while the stop was in flight.
        {"status": BackgroundJobStatus.SUCCEEDED.value, "attempts": 1},
        {"status": BackgroundJobStatus.CANCELLED.value, "attempts": 1},
    ],
)
def test_requeue_leaves_a_job_that_moved_on_alone(sessions, job_id, newer_state):
    _set_row(sessions, job_id, **newer_state)

    with sessions() as db:
        requeued = requeue_job_after_planned_stop(db, job_id, claimed_attempt=1)

    assert requeued is False
    job = _row(sessions, job_id)
    assert job.status == newer_state["status"]
    assert job.attempts == newer_state["attempts"]
    assert job.error_message is None


def test_requeue_ignores_unknown_job(sessions):
    with sessions() as db:
        assert (
            requeue_job_after_planned_stop(db, "no-such-job", claimed_attempt=1)
            is False
        )
