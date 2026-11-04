"""Previous-release checkpoints must still be readable from the task database.

The core compatibility test (``tests/core/agent/test_checkpoint_backward_compat.py``)
restores each fixture from an in-memory store. This one stores the same fixtures,
byte for byte, through the real persistence writers and reads them back through
the real readers, for both storages:

* ``v1``: legacy trace events (``DatabaseTraceHandler``);
* ``v2``: execution events (``ExecutionEventTraceAdapter``), whose reader also
  validates the recovery envelope and the attempt identity of every pending tool
  call before it hands a checkpoint to the runner.

A checkpoint a reader refuses is a task that cannot resume after a release.
See the core test's docstring for when to regenerate or drop fixtures.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy.orm import sessionmaker

from tests.shared.checkpoint_compat import FIXTURE_ROOT
from tests.web.services.task_database_shared import engine as engine_fixture
from tests.web.services.task_database_shared import task_id as task_id_fixture
from xagent.core.agent.checkpoint import CHECKPOINT_EVENT_TYPE, TraceCheckpointStore
from xagent.core.agent.trace import Tracer
from xagent.web.models.task import Task
from xagent.web.services.trace_handlers import DatabaseTraceHandler
from xagent.web.tracing import ExecutionEventTraceAdapter

engine = engine_fixture
task_id = task_id_fixture

FIXTURES = sorted(FIXTURE_ROOT.glob("*/*/*.json"))


@pytest.mark.parametrize("storage", ["v1", "v2"])
async def test_previous_release_checkpoints_read_back(
    storage: str, engine, task_id, monkeypatch
) -> None:
    # One database per storage: building the schema dominates the cost, and a
    # checkpoint read returns the latest write, so each fixture is written and
    # read back in turn.
    paths = [path for path in FIXTURES if path.parent.name == storage]
    assert paths, f"no {storage} fixtures under {FIXTURE_ROOT}"

    monkeypatch.setenv("XAGENT_ASYNC_TRACE_DB_ENABLED", "false")
    factory = sessionmaker(engine)
    with factory() as db:
        db.get(Task, task_id).conversation_storage_version = 2 if storage == "v2" else 1
        db.commit()
    monkeypatch.setattr("xagent.web.models.database._SessionLocal", factory)
    monkeypatch.setattr(
        "xagent.web.services.trace_handlers.get_db", lambda: iter([factory()])
    )

    handler = (
        ExecutionEventTraceAdapter(task_id)
        if storage == "v2"
        else DatabaseTraceHandler(task_id)
    )
    tracer = Tracer()
    tracer.handlers = [handler]
    if storage == "v2":
        tracer.event_writer = handler.commit_event

    for path in paths:
        envelope = json.loads(path.read_text())
        execution_id = envelope["root_execution_id"]
        # Persist the envelope exactly as the old release produced it.
        await tracer.trace_event(
            CHECKPOINT_EVENT_TYPE,
            task_id=execution_id,
            data=envelope,
            require_persisted=True,
        )
        restored = await TraceCheckpointStore(tracer).load_latest_checkpoint(
            execution_id
        )
        assert restored == envelope["snapshot"], path
