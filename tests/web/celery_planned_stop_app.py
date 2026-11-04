"""Celery app for the planned-stop end-to-end test: the production app, plus one job.

``test_celery_planned_stop_e2e.py`` runs ``celery -A`` against this module so the
worker is the real ``execute_background_job`` worker with a real broker. The
only addition is a ``test.planned_stop`` handler, which stands in for a long KB
ingest: it announces itself, then runs until told to finish.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

from xagent.web.jobs.celery_app import celery_app
from xagent.web.jobs.tasks import register_background_job_handler
from xagent.web.models.background_job import BackgroundJob

__all__ = ["JOB_TYPE", "celery_app"]

JOB_TYPE = "test.planned_stop_e2e"


def _long_running_job(_db: Any, job: BackgroundJob) -> dict[str, Any]:
    """Write ``started-<pid>-<attempts>``, then run until ``release`` appears."""
    directory = Path(dict(job.payload or {})["marker_dir"])
    (directory / f"started-{os.getpid()}-{job.attempts}").touch()
    while not (directory / "release").exists():
        time.sleep(0.05)
    return {"finished_by": os.getpid()}


register_background_job_handler(JOB_TYPE, _long_running_job)
