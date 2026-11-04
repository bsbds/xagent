"""A planned worker stop requeues a running job at once, without using an attempt.

Runs the production Celery worker (prefork pool, Redis broker, ``acks_late``) as
a subprocess against a real Redis, stops it the way a release does -- one
SIGTERM, with ``REMAP_SIGTERM=SIGQUIT`` as the Compose ``worker`` service sets --
and checks what the next worker finds. Needs ``XAGENT_TEST_REDIS_URL`` or a
``redis-server`` binary; skipped otherwise.
"""

from __future__ import annotations

import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import TypeVar

import pytest
import redis
from celery import Celery

from xagent.web.models.background_job import BackgroundJob, BackgroundJobStatus
from xagent.web.models.database import get_session_local, init_db
from xagent.web.models.user import User
from xagent.web.services.background_jobs import (
    PLANNED_STOP_REQUEUE_MESSAGE,
    create_background_job,
)

from .celery_planned_stop_app import JOB_TYPE

REPO_ROOT = Path(__file__).resolve().parents[2]
# A database of its own: CI's shared Redis service also serves other suites.
BROKER_DB = 11
# More jobs than pool children: some run, one is still waiting in the queue.
POOL_CHILDREN = 2
JOBS = 3
T = TypeVar("T")


@pytest.fixture(scope="module")
def redis_url(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    configured = os.getenv("XAGENT_TEST_REDIS_URL")
    process = None
    if configured:
        base = configured.rsplit("/", 1)[0]
    else:
        executable = shutil.which("redis-server")
        if executable is None:
            pytest.skip("needs XAGENT_TEST_REDIS_URL or a redis-server binary")
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        base = f"redis://127.0.0.1:{port}"
        log = tmp_path_factory.mktemp("planned-stop-redis") / "redis.log"
        with log.open("w") as out:
            process = subprocess.Popen(
                [executable, "--bind", "127.0.0.1", "--port", str(port)]
                + ["--save", "", "--appendonly", "no"],
                stdout=out,
                stderr=subprocess.STDOUT,
            )
    url = f"{base}/{BROKER_DB}"
    client = redis.Redis.from_url(url)
    try:
        wait_for(lambda: client.ping() or None, "redis to accept connections", 10)
        yield url
    finally:
        client.close()
        if process is not None:
            process.terminate()
            process.wait(timeout=10)


@pytest.fixture
def broker(redis_url: str) -> Iterator[redis.Redis]:
    client = redis.Redis.from_url(redis_url)
    client.flushdb()
    yield client
    client.flushdb()
    client.close()


def wait_for(probe: Callable[[], T | None], what: str, timeout: float) -> T:
    """Poll ``probe`` until it returns something other than ``None``."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            found = probe()
        except redis.ConnectionError:
            found = None
        if found is not None:
            return found
        if time.monotonic() >= deadline:
            raise AssertionError(f"timed out after {timeout}s waiting for {what}")
        time.sleep(0.1)


class Worker:
    """One ``celery worker`` process, in its own session so it cannot leak."""

    def __init__(self, env: dict[str, str], log: Path) -> None:
        self._log = log.open("w")
        self.process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "celery",
                "-A",
                "tests.web.celery_planned_stop_app:celery_app",
                "worker",
                "--queues=default",
                f"--concurrency={POOL_CHILDREN}",
                "--loglevel=INFO",
                f"--hostname=worker-{log.stem}@%h",
            ],
            cwd=REPO_ROOT,
            env=env,
            stdout=self._log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )

    def wait_until_ready(self) -> None:
        wait_for(
            lambda: "ready." in self.output() or self.fail_if_exited(),
            "the worker to start",
            120,
        )

    def fail_if_exited(self) -> None:
        if self.process.poll() is not None:
            raise AssertionError(f"worker exited early:\n{self.output()}")

    def output(self) -> str:
        self._log.flush()
        return Path(self._log.name).read_text()

    def stop(self) -> float:
        """What a release does: one SIGTERM. Returns the seconds until it exited."""
        begun = time.monotonic()
        self.process.send_signal(signal.SIGTERM)
        try:
            self.process.wait(timeout=60)
        except subprocess.TimeoutExpired:
            raise AssertionError(
                f"worker still running after SIGTERM:\n{self.output()}"
            )
        return time.monotonic() - begun

    def kill(self) -> None:
        if self.process.poll() is None:
            os.killpg(self.process.pid, signal.SIGKILL)
            self.process.wait()
        self._log.close()


@pytest.fixture
def start_worker(tmp_path: Path, redis_url: str, broker: redis.Redis):
    database = f"sqlite:///{tmp_path / 'e2e.db'}"
    env = {
        **os.environ,
        "REMAP_SIGTERM": "SIGQUIT",
        "DATABASE_URL": database,
        "XAGENT_CELERY_ENABLED": "true",
        "XAGENT_CELERY_BROKER_URL": redis_url,
        "XAGENT_STORAGE_ROOT": str(tmp_path / "storage"),
        "XAGENT_ENVIRONMENT": "development",
        "PYTHONPATH": os.pathsep.join(
            [str(REPO_ROOT), os.environ.get("PYTHONPATH", "")]
        ),
    }
    env.pop("XAGENT_REDIS_URL", None)
    workers: list[Worker] = []

    def start() -> Worker:
        worker = Worker(env, tmp_path / f"w{len(workers) + 1}.log")
        workers.append(worker)
        worker.wait_until_ready()
        return worker

    init_db(database)
    yield start
    for worker in workers:
        worker.kill()


def test_planned_stop_requeues_running_jobs_for_the_next_worker(
    tmp_path: Path, redis_url: str, broker: redis.Redis, start_worker
) -> None:
    sessions = get_session_local()
    markers = tmp_path / "markers"
    markers.mkdir()
    with sessions() as db:
        user = User(username="planned-stop-e2e", password_hash="x")
        db.add(user)
        db.commit()
        job_ids = [
            str(
                create_background_job(
                    db,
                    user_id=int(user.id),
                    job_type=JOB_TYPE,
                    payload={"marker_dir": str(markers)},
                    queue="default",
                    max_attempts=3,
                ).id
            )
            for _ in range(JOBS)
        ]
        db.query(BackgroundJob).update({"status": BackgroundJobStatus.ENQUEUED.value})
        db.commit()

    def rows() -> list[BackgroundJob]:
        with sessions() as db:
            found = db.query(BackgroundJob).order_by(BackgroundJob.created_at).all()
            db.expunge_all()
            return found

    def started() -> list[str]:
        return sorted(path.name for path in markers.glob("started-*"))

    # A client of its own: this process's ``celery_app`` was configured for
    # whatever broker the rest of the suite uses, not this one.
    client = Celery("planned-stop-client", broker=redis_url)
    for job_id in job_ids:
        client.send_task(
            "xagent.web.jobs.tasks.execute_background_job",
            args=[job_id],
            queue="default",
        )

    first = start_worker()
    wait_for(
        lambda: len(started()) == POOL_CHILDREN or None,
        "the first worker to start a job on every pool child",
        60,
    )
    running = [row for row in rows() if row.status == BackgroundJobStatus.RUNNING.value]
    assert len(running) == POOL_CHILDREN
    assert {row.attempts for row in running} == {1}

    seconds = first.stop()

    # Every job is back on the queue before the worker finished exiting -- the
    # running ones restored, the waiting one never taken -- and the attempts
    # the interrupted runs used were given back.
    assert seconds < 30, first.output()
    assert broker.llen("default") == JOBS, first.output()
    assert broker.hlen("unacked") == 0, first.output()
    interrupted = rows()
    assert {row.status for row in interrupted} == {BackgroundJobStatus.ENQUEUED.value}
    assert {row.attempts for row in interrupted} == {0}
    requeued = [row for row in interrupted if row.error_message is not None]
    assert len(requeued) == POOL_CHILDREN
    for row in requeued:
        assert row.error_message == PLANNED_STOP_REQUEUE_MESSAGE
        assert row.progress["message"] == PLANNED_STOP_REQUEUE_MESSAGE

    # The next worker runs them all as their very first attempt.
    second = start_worker()
    wait_for(
        lambda: len(started()) == POOL_CHILDREN + POOL_CHILDREN or None,
        "the next worker to run the requeued jobs",
        60,
    )
    assert all(name.endswith("-1") for name in started()), started()
    assert {row.attempts for row in rows()} <= {0, 1}

    (markers / "release").touch()
    wait_for(
        lambda: (
            {row.status for row in rows()} == {BackgroundJobStatus.SUCCEEDED.value}
            or None
        ),
        "every job to finish",
        60,
    )
    finished = rows()
    assert {row.attempts for row in finished} == {1}
    assert {row.error_message for row in finished} == {None}
    wait_for(
        lambda: broker.llen("default") == 0 and broker.hlen("unacked") == 0 or None,
        "the broker to be empty",
        10,
    )
    second.stop()
