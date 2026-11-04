"""A checkpoint written by a previous release must resume on the current code.

Releases pause running tasks and resume them on the new version, so what the
last release wrote has to be readable by this one. That is a release invariant,
and this module is the gate for it.

What is tested
--------------
``tests/fixtures/checkpoints/<release>/<storage>/<name>.json`` holds checkpoints
as that release persisted them (``v1`` legacy trace storage, ``v2``
execution-event storage), one per resume point worth guarding: the ReAct labels
``before_llm``, ``after_llm``, ``before_tool``, ``after_tool``,
``before_tool_batch``, ``after_tool_batch`` and ``waiting_for_user``; a
sequential DAG; a DAG with two steps in flight at once; and Auto routing into a
ReAct or a DAG child.

Every fixture of every retained release is restored through the real path,
``AgentExecutionAdapter.resume`` -> ``AgentRunner`` -> checkpoint reader ->
``ExecutionContext.from_dict`` -> the pattern's ``load_state``, with a fake model
and fake tools. The run continues until it reaches its next LLM call or tool step
and is paused there (a real pause: every call in flight is cancelled). The test
asserts that:

* the run got that far and ended as a pause, not as a failure or a finish;
* the checkpoint the paused run stores again is well formed: every tool result
  answers one earlier tool call, in the root conversation and in each DAG step's;
* if it reached an LLM call, the request it would send is well formed: every
  tool call also has its result, which a provider requires;
* if it reached a tool step, that call is one the checkpoint had planned.

It deliberately does not assert whether a tool call that was in flight is
replayed or replanned: either is a valid way to resume, and only a run that
cannot resume at all is a break.

``tests/web/services/test_checkpoint_backward_compat_storage.py`` covers the other
half: the same fixtures written through the real task-database writers and read
back through the real readers, which also validate the stored envelope.

When this test fails
--------------------
A failure means a task paused on the previous release may not resume on this one.
Fix the code so it still reads the old checkpoint. Only if dropping support is
intended, make the break explicit in the same change: delete that release's
fixture directory and say so in the release notes. Do not edit a fixture to make
it pass; a fixture is the old release's output.

Regenerating
------------
Fixtures are produced by ``tests/shared/checkpoint_compat.py``. At each release,
on the release commit, add the new frozen set::

    uv run python -m tests.shared.checkpoint_compat <release>

Keep as many releases as should stay resumable (at least the previous one). The
generator refuses to overwrite an existing release.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from tests.shared.checkpoint_compat import (
    EXECUTION_ID,
    FIXTURE_ROOT,
    USER_ANSWER,
    CheckpointBackend,
    CompatTool,
    Probe,
    ProbeLLM,
    write_release,
)
from xagent.core.agent.checkpoint import (
    CHECKPOINT_SCHEMA_VERSION,
    READABLE_CHECKPOINT_TYPES,
    TraceCheckpointStore,
)
from xagent.core.agent.execution_adapter import (
    AgentExecutionAdapter,
    AgentExecutionConfig,
)
from xagent.core.agent.runner import UserMessageInjectionOutcome

FIXTURES = sorted(FIXTURE_ROOT.glob("*/*/*.json"))

# ``AgentExecutionConfig.pattern`` that builds each checkpointed pattern.
CONFIG_PATTERN = {
    "ReActPattern": "react",
    "DAGPattern": "dag_plan_execute",
    "AutoPattern": "auto",
}


def _fixture_id(path: Path) -> str:
    return "/".join(path.with_suffix("").parts[-3:])


def _pending_calls(state: Any) -> list[tuple[str, dict[str, Any]]]:
    """Every ``(tool, args)`` planned in a pattern state, nested ones included."""
    if isinstance(state, list):
        return [call for item in state for call in _pending_calls(item)]
    if not isinstance(state, dict):
        return []
    own = [(c["name"], c["args"]) for c in state.get("pending_tool_calls") or []]
    nested = [value for value in state.values() if isinstance(value, (dict, list))]
    return own + _pending_calls(nested)


def _message_lists(node: Any) -> Iterator[list[dict[str, Any]]]:
    """Every conversation in a payload: the root context and each step's."""
    if isinstance(node, dict):
        messages = node.get("messages")
        if isinstance(messages, list) and all(
            isinstance(message, dict) and "role" in message for message in messages
        ):
            yield messages
        for value in node.values():
            yield from _message_lists(value)
    elif isinstance(node, list):
        for value in node:
            yield from _message_lists(value)


def _assert_tool_results_pair_up(
    messages: list[dict[str, Any]], *, all_calls_answered: bool = False
) -> None:
    """Every tool result answers one earlier tool call, once.

    With ``all_calls_answered`` every tool call also has its result, as a
    provider requires of a request. A stored conversation may legitimately end
    with calls still planned, so that half is only asserted for requests.
    """
    called: set[str] = set()
    answered: set[str] = set()
    for message in messages:
        for call in message.get("tool_calls") or []:
            called.add(call["id"])
        if message["role"] == "tool":
            call_id = message["tool_call_id"]
            assert call_id in called, f"tool result {call_id} answers no earlier call"
            assert call_id not in answered, f"tool call {call_id} answered twice"
            answered.add(call_id)
    if all_calls_answered:
        assert called == answered, f"unanswered tool calls: {sorted(called - answered)}"


def test_a_release_with_fixtures_is_retained() -> None:
    """The parametrized restore test is vacuous without fixtures."""
    assert FIXTURES, f"no checkpoint fixtures under {FIXTURE_ROOT}"


@pytest.mark.parametrize("path", FIXTURES, ids=_fixture_id)
async def test_previous_release_checkpoint_resumes(path: Path) -> None:
    envelope = json.loads(path.read_text())
    snapshot = envelope["snapshot"]
    storage = path.parent.name

    # The envelope the old release stored is still one this release reads.
    assert envelope["checkpoint_type"] in READABLE_CHECKPOINT_TYPES
    assert envelope["snapshot_schema_version"] <= CHECKPOINT_SCHEMA_VERSION

    probe = Probe()
    backend = CheckpointBackend(execution_events=storage == "v2", seed=envelope)
    adapter = AgentExecutionAdapter(
        AgentExecutionConfig(
            name="compat",
            pattern=CONFIG_PATTERN[snapshot["pattern"]],
            llm=ProbeLLM(probe),
            tools=[
                CompatTool("record", probe=probe),
                CompatTool("lookup", read_only=True, probe=probe),
            ],
            tracer=TraceCheckpointStore(backend),
            workspace_enabled=False,
            skills_enabled=False,
            tool_parallel_enabled=True,
        )
    )
    if snapshot["label"] == "waiting_for_user":
        # A parked run resumes once the user has answered, as the web layer does.
        posted = await adapter.post_user_message(
            EXECUTION_ID, USER_ANSWER, request_interrupt=False
        )
        assert posted is UserMessageInjectionOutcome.POSTED_FRESH

    probe.pause = lambda: adapter.pause(EXECUTION_ID, reason=Probe.PAUSE_REASON)

    result = await adapter.resume(EXECUTION_ID)

    assert result is not None
    # The run got as far as an LLM call or a tool step and was paused there; it
    # did not fail, finish, or park again without reaching either.
    failure = (
        result["status"],
        result["error"],
        result["agent_result"].get("pattern_errors"),
    )
    assert probe.reached is not None, failure
    assert result["status"] == "interrupted", failure
    # What the paused run stored again is as well formed as what it was given.
    for messages in _message_lists(backend.envelopes[-1]["snapshot"]):
        _assert_tool_results_pair_up(messages)
    kind, detail = probe.reached
    if kind == "llm":
        _assert_tool_results_pair_up(detail, all_calls_answered=True)
        if snapshot["label"] == "waiting_for_user":
            assert USER_ANSWER in json.dumps(detail)
    else:
        assert detail in _pending_calls(snapshot["pattern_state"])


def test_generator_is_deterministic(tmp_path: Path) -> None:
    """Regenerating from the same code is byte-identical.

    Without this a regenerated set would differ in noise (ids, timestamps) and a
    reviewer could not tell a real checkpoint format change from churn.
    """
    first = write_release("one", tmp_path)
    second = write_release("two", tmp_path)

    names = sorted(path.relative_to(first) for path in first.rglob("*.json"))
    assert names == sorted(path.relative_to(second) for path in second.rglob("*.json"))
    assert names
    for name in names:
        assert (first / name).read_bytes() == (second / name).read_bytes(), name
