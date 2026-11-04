"""Checkpoint fixtures for the cross-release compatibility test, and their generator.

``tests/core/agent/test_checkpoint_backward_compat.py`` restores the checkpoints
stored under ``tests/fixtures/checkpoints/<release>/<storage>/<name>.json``.
Each file is a checkpoint exactly as the release that produced it persisted it
(the ``agent_execution_checkpoint`` event envelope around a ``snapshot``), so it
stays a faithful sample of that release's wire format no matter how the code
evolves afterwards. This module owns everything needed to *produce* such a set:

* the fake model, fake tool and in-memory checkpoint backend shared by the
  generator and the test, so the tools a checkpoint names exist on restore;
* the scenarios, which drive the real ``AgentRunner`` and patterns (ReAct, DAG,
  Auto) with a scripted model and keep the checkpoints worth freezing;
* a canonicalizer that replaces uuids and timestamps with stable stand-ins, so
  regenerating from the same code is byte-identical and a regenerated set only
  differs where the checkpoint format really changed;
* ``main()``, the generator CLI.

Regenerating (release process)
------------------------------
On the commit that becomes release ``<release>`` (e.g. the tag ``v0.9.0``)::

    uv run python -m tests.shared.checkpoint_compat <release>

This writes ``tests/fixtures/checkpoints/<release>/`` and refuses to touch a
release that already has fixtures: a frozen set is the whole point, so the only
way to change one is to delete it first (``rm -r`` the directory). Commit the
new directory next to the release. Dropping support for an old release is the
reverse: delete its directory in the same change that makes the break.

Fixture layout and storage
--------------------------
``v1`` fixtures come from legacy trace-event storage. ``v2`` fixtures come from
execution-event storage, where the tracer ``records_execution_events`` and every
planned tool call also carries a ``tool_attempt_id`` and ``assistant_message_id``
(see ``ReActPattern``). Only checkpoints that hold pending tool calls differ
between the two, so only a representative subset of those is captured twice (a
DAG checkpoint is large; the concurrent one covers the multi-step reader).

Fixture names are ``<structure>__<label>``: ``react``, ``dag`` (sequential
plan), ``dag_concurrent`` (two steps in flight at once), ``auto_react`` and
``auto_dag`` (Auto routing into a child), then the checkpoint label with the
nesting prefix (``auto_child_``, ``dag_``) stripped.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import shutil
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from xagent.core.agent import (
    Agent,
    AgentRunner,
    AutoPattern,
    DAGPattern,
    ReActPattern,
)
from xagent.core.agent.checkpoint import CHECKPOINT_TYPE, TraceCheckpointStore
from xagent.core.agent.pattern.auto.auto import DECISION_TOOL_NAME
from xagent.core.agent.pattern.dag.dag import DAG_COMPLETION_TOOL_NAME

FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "checkpoints"
STORAGES = ("v1", "v2")
EXECUTION_ID = "compat-exec"
# The answer posted to a ``waiting_for_user`` checkpoint before it is resumed.
USER_ANSWER = "Lisbon"

Envelope = dict[str, Any]


# ---------------------------------------------------------------------------
# Test doubles shared by the generator and the compatibility test
# ---------------------------------------------------------------------------


class _ToolArgs(BaseModel):
    value: str


class CompatTool:
    """A tool the scenarios call: ``record`` is serial, ``lookup`` is read-only.

    ``lookup`` is marked concurrency-safe, so two consecutive calls become one
    concurrent batch (``before_tool_batch``) when the pattern allows parallelism.
    With a ``probe`` the tool does not run: it reports the call and stops the run.
    """

    def __init__(
        self, name: str, *, read_only: bool = False, probe: Probe | None = None
    ) -> None:
        self.metadata = type(
            "Metadata",
            (),
            {
                "name": name,
                "description": f"Compat tool {name}.",
                "read_only": read_only,
                "concurrency_safe": read_only,
                "category": "basic",
                "decision_group": None,
            },
        )()
        self._probe = probe

    def args_type(self) -> type[BaseModel]:
        return _ToolArgs

    async def run_json_async(self, args: dict[str, Any]) -> dict[str, Any]:
        if self._probe is not None:
            await self._probe.stop("tool", (self.metadata.name, dict(args)))
        return {"success": True, "tool": self.metadata.name, "args": dict(args)}


class Probe:
    """Stops a restored run at the first LLM call or tool step it reaches.

    The stop is a real pause (``AgentExecutionAdapter.pause``): the runtime
    cancels every call in flight, siblings of a concurrent DAG or tool batch
    included, and the run unwinds as it does when a release pauses it.
    """

    PAUSE_REASON = "compat probe"

    def __init__(self) -> None:
        self.pause: Callable[[], object] = lambda: None
        # ``("llm", messages sent)`` or ``("tool", (name, args))``.
        self.reached: tuple[str, Any] | None = None

    async def stop(self, kind: str, detail: Any) -> None:
        if self.reached is None:
            self.reached = (kind, detail)
        self.pause()
        # The pause cancels this call within a loop turn; getting past the sleep
        # means it did not.
        await asyncio.sleep(10)
        raise AssertionError("the pause did not cancel the probed call")


class ProbeLLM:
    """The model of a restored run: reports its first request and stops there."""

    def __init__(self, probe: Probe) -> None:
        self._probe = probe

    async def chat(self, **kwargs: Any) -> Any:
        await self._probe.stop("llm", list(kwargs["messages"]))


class CheckpointBackend:
    """In-memory trace backend: the writer and reader behind ``TraceCheckpointStore``.

    ``envelopes`` keeps what ``TraceCheckpointStore`` persists, event by event.
    Like the production readers it answers a read with the latest *snapshot*,
    which ``TraceCheckpointStore`` hands to the runner. ``execution_events``
    selects v2 behaviour (``records_execution_events``).
    """

    def __init__(
        self, *, execution_events: bool = False, seed: Envelope | None = None
    ) -> None:
        self.records_execution_events = execution_events
        self.envelopes: list[Envelope] = [seed] if seed is not None else []

    async def trace_event(
        self,
        event_type: Any,
        *,
        data: dict[str, Any] | None = None,
        require_persisted: bool = False,
        **_: Any,
    ) -> str:
        del event_type, require_persisted
        if isinstance(data, dict) and data.get("checkpoint_type") == CHECKPOINT_TYPE:
            self.envelopes.append(data)
        return f"event-{len(self.envelopes)}"

    async def load_latest_checkpoint(self, execution_id: str) -> dict[str, Any] | None:
        for envelope in reversed(self.envelopes):
            if envelope["root_execution_id"] == execution_id:
                return dict(envelope["snapshot"])
        return None

    async def load_committed_tool_outcome(
        self, tool_call: dict[str, Any]
    ) -> dict[str, Any] | None:
        """No tool outcome was committed after the checkpoint (v2 only)."""
        del tool_call
        return None


class ScriptedLLM:
    """Deterministic model that answers from the request, never from call order.

    Concurrent DAG steps interleave their calls, so a response queue would hand
    each step whatever happens to be next. Instead:

    * Auto's routing tool gets ``route``;
    * the DAG completion tool gets "completed";
    * any other call is a ReAct turn. Its index is the number of assistant tool
      call messages already in the request, and ``turns[index]`` lists the tool
      calls to make as ``(tool, suffix_or_args)``; past the last turn the model
      answers in plain text.

    A string suffix becomes ``{"value": f"{key}-{suffix}"}`` where ``key`` is the
    first of ``keys`` found in the request (a DAG step's task names its key), so
    concurrent steps call distinguishable tools.
    """

    def __init__(
        self,
        turns: list[list[tuple[str, str | dict[str, Any]]]],
        *,
        route: str = "react",
        keys: tuple[str, ...] = ("task",),
    ) -> None:
        self._turns = turns
        self._route = route
        self._keys = keys

    async def chat(self, **kwargs: Any) -> dict[str, Any]:
        messages = kwargs["messages"]
        tool_names = {tool["function"]["name"] for tool in kwargs.get("tools") or []}
        if DECISION_TOOL_NAME in tool_names:
            return _tool_response(
                DECISION_TOOL_NAME,
                {
                    "action": self._route,
                    "reason": "compat",
                    "requires_current_or_external_facts": False,
                    "existing_context_sufficient": True,
                    "evidence_basis": "current conversation",
                    "missing_verification": "",
                },
            )
        if DAG_COMPLETION_TOOL_NAME in tool_names:
            return _tool_response(
                DAG_COMPLETION_TOOL_NAME,
                {
                    "status": "completed",
                    "reason": "compat",
                    "answer": "done",
                    "missing_work": "",
                    "replan_instruction": "",
                },
            )
        text = json.dumps(messages)
        key = next((key for key in self._keys if key in text), self._keys[0])
        index = sum(
            1
            for message in messages
            if message["role"] == "assistant" and message.get("tool_calls")
        )
        if index >= len(self._turns):
            return {"content": "done", "done": True}
        calls = [
            {
                "id": f"call_{key}_{index}_{position}",
                "function": {
                    "name": name,
                    "arguments": json.dumps(
                        {"value": f"{key}-{argument}"}
                        if isinstance(argument, str)
                        else argument
                    ),
                },
            }
            for position, (name, argument) in enumerate(self._turns[index])
        ]
        return {"content": "", "tool_calls": calls, "done": False}


def _tool_response(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return {
        "tool_calls": [
            {
                "id": f"call_{name}",
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments)},
            }
        ]
    }


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------

_SERIAL = [("record", "1"), ("record", "2")]
_BATCH = [("lookup", "1"), ("lookup", "2")]
_ASK = [("ask_user_question", {"message": "Which city?", "interactions": []})]


def _steps(*tasks: tuple[str, list[str]]) -> Callable[..., Awaitable[dict[str, Any]]]:
    """A plan generator for ``DAGPattern`` from ``(task, dependencies)`` pairs."""

    async def generate(request: Any, llm: Any) -> dict[str, Any]:
        del request, llm
        return {
            "steps": [
                {
                    "id": task.split()[-1],
                    "task": task,
                    "dependencies": dependencies,
                    "tool_names": ["record"],
                }
                for task, dependencies in tasks
            ]
        }

    return generate


async def _run(
    pattern: Any, llm: ScriptedLLM, backend: CheckpointBackend, *, task: str
) -> None:
    """Run ``pattern`` through the real runner, persisting into ``backend``."""
    agent = Agent(
        name="compat",
        patterns=[pattern],
        tools=[CompatTool("record"), CompatTool("lookup", read_only=True)],
        llm=llm,
    )
    runner = AgentRunner(
        agent, tracer=TraceCheckpointStore(backend), workspace_enabled=False
    )
    await runner.run(task=task, execution_id=EXECUTION_ID)


def _react() -> ReActPattern:
    return ReActPattern(max_iterations=6, tool_parallel_enabled=True)


async def _react_tools(backend: CheckpointBackend) -> None:
    await _run(_react(), ScriptedLLM([_SERIAL, _BATCH]), backend, task="Do the work.")


async def _react_waiting(backend: CheckpointBackend) -> None:
    await _run(_react(), ScriptedLLM([_ASK]), backend, task="Plan a trip.")


async def _dag(backend: CheckpointBackend) -> None:
    plan = _steps(("Collect alpha", []), ("Collect beta", ["alpha"]))
    await _run(
        DAGPattern(plan, max_concurrency=2),
        ScriptedLLM([_SERIAL], keys=("alpha", "beta")),
        backend,
        task="Do the work.",
    )


async def _dag_concurrent(backend: CheckpointBackend) -> None:
    plan = _steps(("Collect alpha", []), ("Collect beta", []))
    await _run(
        DAGPattern(plan, max_concurrency=2),
        ScriptedLLM([_SERIAL], keys=("alpha", "beta")),
        backend,
        task="Do the work.",
    )


async def _auto_react(backend: CheckpointBackend) -> None:
    await _run(
        AutoPattern(react_pattern=_react()),
        ScriptedLLM([_SERIAL]),
        backend,
        task="Do the work.",
    )


async def _auto_dag(backend: CheckpointBackend) -> None:
    plan = _steps(("Collect alpha", []))
    await _run(
        AutoPattern(dag_pattern=DAGPattern(plan, max_concurrency=2)),
        ScriptedLLM([_SERIAL], route="plan_execute", keys=("alpha",)),
        backend,
        task="Do the work.",
    )


SCENARIOS: dict[str, Callable[[CheckpointBackend], Awaitable[None]]] = {
    "react_tools": _react_tools,
    "react_waiting": _react_waiting,
    "dag": _dag,
    "dag_concurrent": _dag_concurrent,
    "auto_react": _auto_react,
    "auto_dag": _auto_dag,
}


def _all_steps_pending(snapshot: dict[str, Any]) -> bool:
    """Two DAG steps are in flight and both still hold planned tool calls."""
    states = snapshot["pattern_state"]["active_step_pattern_states"]
    return len(states) == 2 and all(s["pending_tool_calls"] for s in states.values())


@dataclass(frozen=True)
class Capture:
    """One fixture: the first checkpoint of ``scenario`` with this ``label``.

    ``where`` narrows it further (a concurrent DAG writes the same label for each
    step). ``v2`` also captures it under execution-event storage; set it only
    where pending tool calls make the two storages differ, and sparingly.
    """

    name: str
    scenario: str
    label: str
    where: Callable[[dict[str, Any]], bool] = lambda snapshot: True
    v2: bool = False


CAPTURES: tuple[Capture, ...] = (
    Capture("react__before_llm", "react_tools", "before_llm"),
    Capture("react__after_llm", "react_tools", "after_llm", v2=True),
    Capture("react__before_tool", "react_tools", "before_tool", v2=True),
    Capture("react__after_tool", "react_tools", "after_tool"),
    Capture("react__before_tool_batch", "react_tools", "before_tool_batch", v2=True),
    Capture("react__after_tool_batch", "react_tools", "after_tool_batch"),
    Capture("react__waiting_for_user", "react_waiting", "waiting_for_user"),
    Capture("dag__after_llm", "dag", "dag_after_llm"),
    Capture("dag__before_tool", "dag", "dag_before_tool"),
    Capture("dag__after_tool", "dag", "dag_after_tool"),
    Capture("dag__after_step", "dag", "dag_after_step"),
    Capture(
        "dag_concurrent__before_ready_batch", "dag_concurrent", "dag_before_ready_batch"
    ),
    Capture(
        "dag_concurrent__before_tool",
        "dag_concurrent",
        "dag_before_tool",
        _all_steps_pending,
        v2=True,
    ),
    Capture(
        "dag_concurrent__after_tool",
        "dag_concurrent",
        "dag_after_tool",
        _all_steps_pending,
    ),
    Capture("auto_react__after_decision", "auto_react", "auto_after_decision"),
    Capture("auto_react__after_llm", "auto_react", "auto_child_after_llm"),
    Capture("auto_react__before_tool", "auto_react", "auto_child_before_tool", v2=True),
    Capture("auto_react__after_tool", "auto_react", "auto_child_after_tool"),
    Capture("auto_dag__before_tool", "auto_dag", "auto_child_dag_before_tool"),
)


# ---------------------------------------------------------------------------
# Canonicalization and generation
# ---------------------------------------------------------------------------

# Ids minted per run: uuids (turn ids, tool attempt ids, assistant message ids),
# and the 8 hex digits of a DAG child ``execution_id`` / a ReAct ``step_id``.
_RANDOM_ID = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
    r"|(?<=_child_)[0-9a-f]{8}|(?<=react_)[0-9a-f]{8}"
)
_EPOCH = datetime(2026, 1, 1, tzinfo=timezone.utc)


def canonicalize(envelope: Envelope) -> Envelope:
    """Replace everything that varies between runs with a stable stand-in.

    * minted ids (see ``_RANDOM_ID``) and tool ``invocation_id`` values become
      counters of the same width, numbered by first appearance, in values and in
      dict keys alike (the tool ledger is keyed by invocation id), so an id is
      replaced by the same stand-in everywhere it is referenced;
    * every ``*_at`` and ``timestamp`` value becomes a ``2026-01-01`` time (or
      the matching epoch float), likewise numbered by first appearance.

    Dict order is never touched: the tool ledger's order is meaningful. Content
    digests, text and counters are left alone because the restore path may
    validate them against the content they describe.
    """
    invocation_ids: set[str] = set()

    def collect(value: Any) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if key == "invocation_id" and isinstance(item, str):
                    invocation_ids.add(item)
                collect(item)
        elif isinstance(value, list):
            for item in value:
                collect(item)

    collect(envelope)
    ids: dict[str, int] = {}
    times: dict[Any, int] = {}

    def stand_in(raw: str) -> str:
        n = ids.setdefault(raw, len(ids) + 1)
        if len(raw) == 36:
            return f"00000000-0000-4000-8000-{n:012x}"
        return f"{n:0{len(raw)}x}"

    def text(value: str) -> str:
        if value in invocation_ids:
            return stand_in(value)
        return _RANDOM_ID.sub(lambda match: stand_in(match.group(0)), value)

    def is_time_key(key: str) -> bool:
        return key == "timestamp" or key.endswith("_at")

    def walk(value: Any, key: str = "") -> Any:
        if isinstance(value, dict):
            return {text(k): walk(v, k) for k, v in value.items()}
        if isinstance(value, list):
            return [walk(item, key) for item in value]
        if isinstance(value, str) and is_time_key(key):
            number = times.setdefault(value, len(times) + 1)
            return (_EPOCH + timedelta(seconds=number)).isoformat()
        if isinstance(value, float) and is_time_key(key):
            return _EPOCH.timestamp() + times.setdefault(value, len(times) + 1)
        if isinstance(value, str):
            return text(value)
        return value

    result: Envelope = walk(envelope)
    return result


def _first_match(envelopes: list[Envelope], capture: Capture) -> Envelope:
    for envelope in envelopes:
        if envelope["label"] == capture.label and capture.where(envelope["snapshot"]):
            return envelope
    labels = [envelope["label"] for envelope in envelopes]
    raise LookupError(
        f"scenario {capture.scenario!r} never wrote {capture.name!r} "
        f"(label {capture.label!r}); it wrote {labels}"
    )


async def generate_envelopes() -> dict[str, dict[str, Envelope]]:
    """Run every scenario and return ``{storage: {fixture name: envelope}}``."""
    fixtures: dict[str, dict[str, Envelope]] = {storage: {} for storage in STORAGES}
    for storage in STORAGES:
        wanted = [c for c in CAPTURES if storage == "v1" or c.v2]
        for scenario in dict.fromkeys(capture.scenario for capture in wanted):
            backend = CheckpointBackend(execution_events=storage == "v2")
            await SCENARIOS[scenario](backend)
            for capture in wanted:
                if capture.scenario == scenario:
                    fixtures[storage][capture.name] = canonicalize(
                        _first_match(backend.envelopes, capture)
                    )
    return fixtures


def write_release(release: str, root: Path = FIXTURE_ROOT) -> Path:
    """Generate the fixtures of ``release`` under ``root``; never overwrite."""
    target = root / release
    if target.exists():
        raise FileExistsError(
            f"{target} already exists. A release's fixtures are frozen; delete the "
            "directory first if the checkpoint format break is intentional."
        )
    fixtures = asyncio.run(generate_envelopes())
    try:
        for storage, named in fixtures.items():
            (target / storage).mkdir(parents=True)
            for name, envelope in named.items():
                # Key order is the writer's own and is kept (see canonicalize).
                (target / storage / f"{name}.json").write_text(
                    json.dumps(envelope, indent=1) + "\n"
                )
    except BaseException:
        shutil.rmtree(target, ignore_errors=True)
        raise
    return target


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate checkpoint compatibility fixtures from the current code."
    )
    parser.add_argument("release", help="release label, e.g. v0.9.0")
    args = parser.parse_args()
    target = write_release(args.release)
    print(f"wrote {sum(1 for _ in target.rglob('*.json'))} fixtures to {target}")


if __name__ == "__main__":
    main()
