"""A checkpoint written by a previous release must resume on the current code.

Releases pause running tasks and resume them on the new version, so what the
last release wrote has to be readable by this one. That is a release invariant,
and this module is the gate for it.

What is tested
--------------
``tests/fixtures/checkpoints/<release>/<storage>/<name>.json`` holds checkpoints
as that release's own code persisted them (``v1`` legacy trace storage, ``v2``
execution-event storage), one per resume point worth guarding: the ReAct labels
``before_llm``, ``after_llm``, ``before_tool``, ``after_tool``,
``before_tool_batch``, ``after_tool_batch`` and two ``waiting_for_user`` forms (a
model's question and a tool's); a sequential DAG; a DAG with two steps in flight
at once; and Auto routing into a ReAct or a DAG child.

``test_previous_release_checkpoint_resumes`` restores every fixture of every
retained release through the real path, ``AgentExecutionAdapter.resume`` ->
``AgentRunner`` -> checkpoint reader -> ``ExecutionContext.from_dict`` -> the
pattern's ``load_state``, with a fake model and fake tools. It runs once with the
keys in the order the release wrote them and once in the order PostgreSQL JSONB
returns them (``jsonb_order``). The run continues until its next LLM call or tool
step and is paused there (a real pause: every call in flight is cancelled). It
asserts that:

* the run got that far and ended as a pause, not as a failure or a finish;
* the checkpoint the paused run stores again is well formed (every tool result
  answers one earlier tool call) and keeps what the checkpoint recorded as done:
  completed tool calls with their results, the DAG plan and its completed steps,
  the Auto routing decision, every earlier message, and no lower iteration;
* if it reached an LLM call, the request is well formed (every tool call has its
  result), is not a planning or routing request the checkpoint had already
  answered, and carries every user message, the user's answer and every
  completed tool result of the conversation;
* if it reached a tool step, that call is one the checkpoint had planned.

None of this depends on whether a tool call that was in flight is replayed or
replanned: either is a valid way to resume, and only a run that cannot resume,
or one that silently forgets progress, is a break.

``test_load_state_keeps_every_checkpointed_key`` catches the quiet kind of loss.
Most ``load_state`` reads are ``state.get(key, default)``, so a renamed key does
not fail a resume; the run just forgets part of its state. The test loads each
state and reads it back: every key the old release wrote must survive with its
value, unless it is listed in ``MIGRATED_STATE_PATHS``.

``test_call_order_survives_jsonb`` is an expected failure that documents a known
bug (see its reason). It turns into a failure when the bug is fixed, as a prompt
to delete the marker.

``tests/web/services/test_checkpoint_backward_compat_storage.py`` covers the
other half: the same fixtures written through the real task-database writers and
read back through the real readers (SQLite, and PostgreSQL when
``XAGENT_TEST_POSTGRES_URL`` is set), which also validate the stored envelope and
prove the ``jsonb_order`` model.

When this test fails
--------------------
A failure means a task paused on the previous release may not resume on this one,
or would resume having forgotten work. Fix the code so it still reads the old
checkpoint. Only if dropping support is intended, make the break explicit in the
same change: delete that release's fixture directory and say so in the release
notes. Do not edit a fixture to make it pass; a fixture is the old release's
output.

Adding a release's fixtures
---------------------------
Fixtures are produced by ``tests/shared/checkpoint_compat.py`` from the code of
the release they stand for, never from ``main``. At each release, follow the
release procedure in that module's docstring (``git worktree add`` the tag, run
the generator there, copy the directory into a PR) and see it for when old
releases are pruned.
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
    ApprovalTool,
    CheckpointBackend,
    CompatTool,
    Probe,
    ProbeLLM,
    jsonb_order,
    write_release,
)
from xagent.core.agent import AutoPattern, DAGPattern, ReActPattern
from xagent.core.agent.checkpoint import (
    CHECKPOINT_SCHEMA_VERSION,
    READABLE_CHECKPOINT_TYPES,
    TraceCheckpointStore,
)
from xagent.core.agent.execution_adapter import (
    AgentExecutionAdapter,
    AgentExecutionConfig,
)
from xagent.core.agent.pattern.auto.auto import DECISION_TOOL_NAME
from xagent.core.agent.pattern.dag.plan_generator import LLMPlanGenerator
from xagent.core.agent.runner import UserMessageInjectionOutcome

FIXTURES = sorted(FIXTURE_ROOT.glob("*/*/*.json"))

# ``AgentExecutionConfig.pattern`` that builds each checkpointed pattern.
CONFIG_PATTERN = {
    "ReActPattern": "react",
    "DAGPattern": "dag_plan_execute",
    "AutoPattern": "auto",
}

# State keys a release deliberately moved or dropped, as dotted paths into a
# pattern state (``tool_ledger.<id>.status``, ``plan.steps[0].task``): ``load_state``
# followed by ``get_state`` may lose them. Empty by design: every entry is a
# checkpoint format break and belongs in the release notes.
MIGRATED_STATE_PATHS: frozenset[str] = frozenset()

# Key orders a resumed run can be handed: the writer's own, and PostgreSQL's
# (see ``jsonb_order``).
ORDERS = ("written", "jsonb")


def _fixture_id(path: Path) -> str:
    return "/".join(path.with_suffix("").parts[-3:])


def _load(path: Path, order: str) -> dict[str, Any]:
    envelope: dict[str, Any] = json.loads(path.read_text())
    if order == "jsonb":
        envelope = jsonb_order(envelope, path.parent.name)
    return envelope


def _pattern_states(snapshot: dict[str, Any]) -> dict[str, tuple[str, dict[str, Any]]]:
    """Every pattern state in a snapshot, nested ones included, by path."""
    found: dict[str, tuple[str, dict[str, Any]]] = {}

    def walk(kind: str, state: Any, path: str) -> None:
        if not isinstance(state, dict):
            return
        found[path] = (kind, state)
        if kind == "AutoPattern":
            walk("ReActPattern", state.get("react_state"), f"{path}/react")
            walk("DAGPattern", state.get("dag_state"), f"{path}/dag")
        elif kind == "DAGPattern":
            for step_id, step_state in (
                state.get("active_step_pattern_states") or {}
            ).items():
                walk("ReActPattern", step_state, f"{path}/step:{step_id}")

    walk(snapshot["pattern"], snapshot.get("pattern_state"), "")
    return found


def _conversations(snapshot: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """The root conversation and each in-flight DAG step's, by path."""
    found = {"": snapshot["context"]["messages"]}
    for path, (kind, state) in _pattern_states(snapshot).items():
        if kind != "DAGPattern":
            continue
        for step_id, context in (state.get("active_step_contexts") or {}).items():
            found[f"{path}/step:{step_id}"] = context["messages"]
    return found


def _pending_calls(snapshot: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """Every ``(tool, args)`` planned anywhere in a snapshot's pattern states."""
    return [
        (call["name"], call["args"])
        for _, state in _pattern_states(snapshot).values()
        for call in state.get("pending_tool_calls") or []
    ]


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


def _strings(node: Any) -> Iterator[str]:
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for value in node.values():
            yield from _strings(value)
    elif isinstance(node, list):
        for value in node:
            yield from _strings(value)


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


def _assert_progress_kept(old: dict[str, Any], new: dict[str, Any]) -> None:
    """The run kept what the checkpoint recorded as done.

    Holds whether an in-flight tool call is replayed or replanned: a resumed run
    may add to the record, never lose from it. Pattern states are compared where
    both snapshots still have one; a step that finished or was cancelled has none.
    """
    new_states = _pattern_states(new)
    for path, (kind, old_state) in _pattern_states(old).items():
        if path not in new_states:
            continue
        new_state = new_states[path][1]
        where = f"{kind} state at {path or 'the root'}"
        if kind == "ReActPattern":
            for key, record in old_state["tool_ledger"].items():
                if record["status"] == "completed":
                    kept = new_state["tool_ledger"].get(key, {})
                    assert kept.get("status") == "completed", (
                        f"{where} lost the completed call {record['tool_call_id']}"
                    )
                    assert kept.get("result") == record["result"], (
                        f"{where} changed the result of {record['tool_call_id']}"
                    )
            assert new_state["current_iteration"] >= old_state["current_iteration"], (
                f"{where} went back to iteration {new_state['current_iteration']}"
            )
        elif kind == "DAGPattern":
            old_plan, new_plan = old_state.get("plan"), new_state.get("plan")
            if old_plan:
                assert new_plan, f"{where} lost its plan"
                assert [s["id"] for s in new_plan["steps"]] == [
                    s["id"] for s in old_plan["steps"]
                ], f"{where} changed its plan"
                new_status = {s["id"]: s["status"] for s in new_plan["steps"]}
                for step in old_plan["steps"]:
                    if step["status"] == "completed":
                        assert new_status[step["id"]] == "completed", (
                            f"{where} reopened completed step {step['id']}"
                        )
            for step_id, result in old_state["step_results"].items():
                assert new_state["step_results"].get(step_id) == result, (
                    f"{where} lost the result of step {step_id}"
                )
        elif kind == "AutoPattern":
            assert new_state["decision"] == old_state["decision"], (
                f"{where} lost its routing decision"
            )

    new_conversations = _conversations(new)
    for path, old_messages in _conversations(old).items():
        if path not in new_conversations:
            continue

        def digest(messages: list[dict[str, Any]]) -> list[tuple[Any, ...]]:
            # A tool result may be rewritten in place when the tool settles a
            # question it had put to the user; completed results are checked
            # through the ledger above.
            return [
                (
                    m["role"],
                    None if m["role"] == "tool" else m["content"],
                    m["tool_call_id"],
                )
                for m in messages
            ]

        assert digest(new_conversations[path])[: len(old_messages)] == digest(
            old_messages
        ), f"the conversation at {path or 'the root'} lost messages"


def _lacks_paths(old: Any, new: Any, path: str = "") -> Iterator[str]:
    """Paths of ``old`` that ``new`` does not carry with the same value.

    ``new`` may have more keys than ``old``; a key ``old`` has must survive.
    """
    if isinstance(old, dict):
        if not isinstance(new, dict):
            yield path
            return
        for key, value in old.items():
            if key in new:
                yield from _lacks_paths(value, new[key], f"{path}.{key}".lstrip("."))
            else:
                yield f"{path}.{key}".lstrip(".")
    elif isinstance(old, list):
        if not isinstance(new, list) or len(old) != len(new):
            yield path
            return
        for index, (a, b) in enumerate(zip(old, new)):
            yield from _lacks_paths(a, b, f"{path}[{index}]")
    elif old != new:
        yield path


def _new_pattern(kind: str) -> Any:
    if kind == "ReActPattern":
        return ReActPattern()
    if kind == "DAGPattern":
        return DAGPattern(plan_generator=lambda **_: None)
    return AutoPattern()


def test_a_release_with_fixtures_is_retained() -> None:
    """The parametrized restore test is vacuous without fixtures."""
    assert FIXTURES, f"no checkpoint fixtures under {FIXTURE_ROOT}"


@pytest.mark.parametrize("order", ORDERS)
@pytest.mark.parametrize("path", FIXTURES, ids=_fixture_id)
async def test_previous_release_checkpoint_resumes(path: Path, order: str) -> None:
    written = json.loads(path.read_text())
    envelope = _load(path, order)
    snapshot = written["snapshot"]
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
                ApprovalTool(probe=probe),
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

    # What the paused run stores again is as well formed as what it was given,
    # and keeps everything the checkpoint had recorded as done.
    stored = backend.envelopes[-1]["snapshot"]
    for messages in _message_lists(stored):
        _assert_tool_results_pair_up(messages)
    _assert_progress_kept(snapshot, stored)

    kind, detail = probe.reached
    if kind == "tool":
        assert detail in _pending_calls(snapshot)
        return

    messages, tool_names = detail
    _assert_tool_results_pair_up(messages, all_calls_answered=True)
    # The run did not go back to a decision the checkpoint already holds.
    states = list(_pattern_states(snapshot).values())
    if any(k == "DAGPattern" and s.get("plan") for k, s in states):
        assert LLMPlanGenerator.PLAN_TOOL_NAME not in tool_names, "re-planned"
    if any(k == "AutoPattern" and s.get("decision") for k, s in states):
        assert DECISION_TOOL_NAME not in tool_names, "re-routed"
    # Everything the user said, and every tool answer in the same conversation,
    # is in the request the model gets.
    seen = "\n".join(_strings(messages))
    answer = USER_ANSWER if snapshot["label"] == "waiting_for_user" else None
    if answer is not None:
        assert answer in seen, "the user's answer is not in the request"
    completed = {
        record["tool_call_id"]
        for kind, state in states
        if kind == "ReActPattern"
        for record in state["tool_ledger"].values()
        if record["status"] == "completed"
    }
    for message in snapshot["context"]["messages"]:
        if message["role"] == "user" or (
            message["role"] == "tool"
            and snapshot["pattern"] != "DAGPattern"
            and message["tool_call_id"] in completed
        ):
            assert message["content"] in seen, f"lost {message['role']} message"


@pytest.mark.parametrize("order", ORDERS)
@pytest.mark.parametrize("path", FIXTURES, ids=_fixture_id)
def test_load_state_keeps_every_checkpointed_key(path: Path, order: str) -> None:
    """``load_state`` then ``get_state`` returns everything the checkpoint held.

    Most ``load_state`` reads are ``state.get(key, default)``, so a key that is
    renamed or dropped does not fail a resume: the run just silently forgets that
    part of its state. Only a round trip shows it. New keys are fine; a key the
    old release wrote either survives with its value or is listed in
    ``MIGRATED_STATE_PATHS``.
    """
    snapshot = _load(path, order)["snapshot"]
    for state_path, (kind, state) in _pattern_states(snapshot).items():
        pattern = _new_pattern(kind)
        pattern.load_state(json.loads(json.dumps(state)))
        lost = [
            f"{state_path or 'root'}:{lost_path}"
            for lost_path in _lacks_paths(state, pattern.get_state())
            if lost_path not in MIGRATED_STATE_PATHS
        ]
        assert not lost, f"{kind} forgot checkpointed state: {lost}"


@pytest.mark.xfail(
    strict=True,
    reason=(
        "PostgreSQL JSONB re-sorts keys, so tool_call_id_index loads sorted by "
        "provider id instead of in the order the calls were made, and "
        "ReActPattern.load_state keeps that order. The repeated-tool counters "
        "(react.py, I3) then read the wrong calls as the latest. v2 storage loses "
        "the ledger's order too, so the fix needs an ordered list in get_state()."
    ),
)
@pytest.mark.parametrize("path", FIXTURES, ids=_fixture_id)
def test_call_order_survives_jsonb(path: Path) -> None:
    """I3 (react.py): the tool call index keeps the order calls were made in."""
    written = _pattern_states(json.loads(path.read_text())["snapshot"])
    stored = _pattern_states(_load(path, "jsonb")["snapshot"])
    checked = False
    for state_path, (kind, state) in stored.items():
        order = list(written[state_path][1].get("tool_call_id_index") or [])
        if kind != "ReActPattern" or len(order) < 2:
            continue
        checked = True
        pattern = ReActPattern()
        pattern.load_state(state)
        assert list(pattern.tool_call_id_index) == order, "call order was lost"
    if not checked:
        pytest.skip("fewer than two tool calls recorded")


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
