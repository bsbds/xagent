from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from sqlalchemy.orm import Session

from ..models.background_job import (
    BackgroundJob,
    BackgroundJobStatus,
    BackgroundJobType,
)
from ..models.database import get_session_local, init_db
from ..services.background_jobs import (
    mark_job_failed,
    mark_job_running,
    mark_job_succeeded,
    requeue_job_after_planned_stop,
)
from .celery_app import celery_app
from .exceptions import BackgroundJobHandlerError

logger = logging.getLogger(__name__)

BackgroundJobHandler = Callable[[Session, BackgroundJob], dict[str, Any]]

# Downstream distributions own job types this package must not import. They
# register a handler when the worker imports their task module, so their work
# still flows through execute_background_job and keeps the shared retry and
# stale-requeue behaviour instead of needing a parallel Celery task.
_EXTRA_HANDLERS: dict[str, BackgroundJobHandler] = {}

# Job types this package routes itself in _execute_job_handler, which runs
# before _EXTRA_HANDLERS is consulted.
_BUILTIN_JOB_TYPES = frozenset(job_type.value for job_type in BackgroundJobType)


def register_background_job_handler(
    job_type: str, handler: BackgroundJobHandler, *, replace: bool = False
) -> None:
    """Register a handler for a job type defined outside this package.

    Args:
        job_type: Durable ``BackgroundJob.job_type`` value the handler owns.
        handler: Callable invoked by ``_execute_job_handler`` for that job type.
        replace: Allow replacing an existing registration for ``job_type``.

    Raises:
        ValueError: If ``job_type`` is built into this package, or is already
            registered and ``replace`` is not set.
    """
    key = str(job_type)
    if key in _BUILTIN_JOB_TYPES:
        # _execute_job_handler would never reach such a handler, so accepting
        # the registration would silently do nothing.
        raise ValueError(f"Background job type is built-in: {key}")
    if not replace and key in _EXTRA_HANDLERS:
        raise ValueError(f"Background job handler already registered: {key}")
    _EXTRA_HANDLERS[key] = handler


def is_background_job_handler_registered(job_type: str) -> bool:
    """Return whether an externally registered handler owns ``job_type``.

    Built-in job types are routed directly by ``_execute_job_handler`` and are
    never present in the external registry, so this reports ``False`` for them.
    """
    return str(job_type) in _EXTRA_HANDLERS


def _open_worker_session() -> Session:
    try:
        SessionLocal = get_session_local()
    except RuntimeError:
        init_db()
        SessionLocal = get_session_local()
    return SessionLocal()


def _execute_job_handler(db: Session, job: BackgroundJob) -> dict[str, Any]:
    if job.job_type == BackgroundJobType.KB_INGEST_DOCUMENT.value:
        from .kb_tasks import handle_kb_ingest_document

        return handle_kb_ingest_document(db, job)
    if job.job_type == BackgroundJobType.KB_INGEST_WEB.value:
        from .kb_tasks import handle_kb_ingest_web

        return handle_kb_ingest_web(db, job)
    if job.job_type == BackgroundJobType.TRIGGER_EVENT.value:
        from .trigger_tasks import handle_trigger_event

        return handle_trigger_event(db, job)
    if job.job_type == BackgroundJobType.TRIGGER_SCAN.value:
        from .trigger_tasks import handle_trigger_scan

        return handle_trigger_scan(db, job)

    handler = _EXTRA_HANDLERS.get(str(job.job_type))
    if handler is not None:
        return handler(db, job)

    # A worker that cannot route this job type will never grow a handler by
    # waiting, so this is permanent: fail fast instead of burning retries.
    raise BackgroundJobHandlerError(
        f"Unsupported background job type: {job.job_type}",
        retryable=False,
    )


def _requeue_after_planned_stop(
    db: Session, job: BackgroundJob, *, claimed_attempt: int
) -> None:
    """Repair the job row of a run that a planned worker stop interrupted.

    Called from ``execute_background_job`` when ``SystemExit`` unwinds a running
    handler. A planned stop (release, ``docker stop``) makes the Celery worker
    cancel its running tasks, which its prefork pool delivers to the pool child
    as a termination signal; billiard turns that into ``SystemExit`` in the
    child's main thread (``billiard.common._shutdown_cleanup``). ``SystemExit``
    is not an ``Exception``, so it unwinds the handler past every ``except
    Exception``: no retry bookkeeping, no failure rollback, and no release of a
    KB ingest target generation (``coordinate_ingest_job`` releases only on a
    normal return or an ``Exception``). That is what makes the run safe to
    simply run again. The one thing left to fix is the row, which still says
    ``RUNNING`` and still counts the attempt.

    The message is deliberately not re-sent. The task is acks-late, so its
    message is still unacknowledged and Celery puts that same message back on
    the broker once the pool child has exited; sending another one here would
    run the job twice.
    """
    try:
        requeued = requeue_job_after_planned_stop(
            db, str(job.id), claimed_attempt=claimed_attempt
        )
    except Exception:  # noqa: BLE001
        # Stopping must not hang on a database that is gone. Celery still
        # redelivers the message, and the job then reruns and consumes an
        # attempt, exactly as it would after a crash.
        logger.exception(
            "Failed to requeue background job %s after a planned worker stop",
            job.id,
        )
        return
    logger.warning(
        "Background job %s interrupted by a planned worker stop (requeued=%s); "
        "attempt %s not consumed",
        job.id,
        requeued,
        claimed_attempt,
    )


@celery_app.task(
    bind=True,
    name="xagent.web.jobs.tasks.execute_background_job",
    retry_backoff=True,
    retry_jitter=True,
)
def execute_background_job(self: Any, job_id: str) -> dict[str, Any]:
    db = _open_worker_session()
    try:
        job = db.query(BackgroundJob).filter(BackgroundJob.id == job_id).first()
        if job is None:
            raise ValueError(f"Background job not found: {job_id}")
        if job.status == BackgroundJobStatus.CANCELLED.value:
            logger.info("Skipping cancelled background job %s", job_id)
            return {"status": "cancelled"}
        if job.status == BackgroundJobStatus.SUCCEEDED.value:
            logger.info("Skipping already completed background job %s", job_id)
            return dict(job.result or {"status": "succeeded"})
        if job.status == BackgroundJobStatus.FAILED.value:
            # Nothing re-enqueues a failed job, so a delivery for one is stale: a
            # message left behind by a worker that died before it acknowledged
            # it, redelivered only once the long visibility timeout expires, by
            # which time the stale sweep has long since run the job to its end.
            logger.info("Skipping already failed background job %s", job_id)
            return {"status": "failed"}

        mark_job_running(db, job)
        claimed_attempt = int(job.attempts)
        try:
            result = _execute_job_handler(db, job)
        except BackgroundJobHandlerError as exc:
            db.refresh(job)
            if exc.retryable and int(job.attempts or 0) < int(job.max_attempts or 1):
                setattr(job, "status", BackgroundJobStatus.ENQUEUED.value)
                setattr(job, "error_message", str(exc))
                setattr(job, "result", exc.result)
                db.add(job)
                db.commit()
                raise self.retry(exc=exc, max_retries=int(job.max_attempts or 1))
            mark_job_failed(db, job, error_message=str(exc), result=exc.result)
            raise
        except Exception as exc:  # noqa: BLE001
            db.refresh(job)
            if int(job.attempts or 0) < int(job.max_attempts or 1):
                setattr(job, "status", BackgroundJobStatus.ENQUEUED.value)
                setattr(job, "error_message", str(exc))
                db.add(job)
                db.commit()
                raise self.retry(exc=exc, max_retries=int(job.max_attempts or 1))
            mark_job_failed(db, job, error_message=str(exc))
            raise
        except SystemExit:
            _requeue_after_planned_stop(db, job, claimed_attempt=claimed_attempt)
            raise

        db.refresh(job)
        mark_job_succeeded(db, job, result=result)
        return result
    finally:
        db.close()
