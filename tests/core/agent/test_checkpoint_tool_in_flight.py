"""``checkpoint_tool_call_in_flight``: may a tool call have started after a
checkpoint was written, so that resuming from it would run the call again?

``mark_possibly_started_tool_calls`` flags exactly the ReAct states the predicate
counts, so a resume closes those calls instead of replaying them.
"""

from __future__ import annotations

import asyncio
import copy
from typing import Any

import pytest

from tests.core.agent.concurrency_harness import (
    FakeTool,
    RecordingContext,
    make_react,
    make_tool_call,
)
from xagent.core.agent import PatternRuntime
from xagent.core.agent.checkpoint import (
    BASE_LABEL_METADATA_KEY,
    TOOL_CALLS_MAY_HAVE_STARTED_KEY,
    checkpoint_tool_call_in_flight,
    mark_possibly_started_tool_calls,
    rewrite_checkpoint,
)

_PENDING = {"pending_tool_calls": [{"id": "call-1", "name": "send", "args": {}}]}
_IDLE = {"pending_tool_calls": []}


def _pattern(**kwargs: Any):
    pattern = make_react(
        repeated_tool_decision_after_consecutive_tool_calls=None,
        repeated_tool_decision_after_consecutive_work_tool_calls=None,
        **kwargs,
    )
    pattern.task_text = "t"
    return pattern


async def _wait_for_call(tool: FakeTool) -> None:
    while not tool.calls:
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_serial_tool_is_in_flight_from_before_tool_until_after_tool() -> None:
    first_gate, second_gate = asyncio.Event(), asyncio.Event()
    tools = [FakeTool("u1", gate=first_gate), FakeTool("u2", gate=second_gate)]
    pattern = _pattern()
    pattern.pending_tool_calls = [make_tool_call("u1"), make_tool_call("u2")]
    runtime = PatternRuntime(execution_id="serial-in-flight")

    run = asyncio.create_task(
        pattern._execute_pending_tool_calls(
            context=RecordingContext(), tools=tools, llm=None, runtime=runtime
        )
    )
    await _wait_for_call(tools[0])
    assert runtime.last_checkpoint["label"] == "before_tool"
    assert checkpoint_tool_call_in_flight(runtime.last_checkpoint) is True

    first_gate.set()
    await _wait_for_call(tools[1])
    # u1's after_tool was followed by u2's own before_tool.
    assert runtime.last_checkpoint["label"] == "before_tool"
    assert checkpoint_tool_call_in_flight(runtime.last_checkpoint) is True

    second_gate.set()
    await run
    assert runtime.last_checkpoint["label"] == "after_tool"
    assert checkpoint_tool_call_in_flight(runtime.last_checkpoint) is False


@pytest.mark.asyncio
async def test_concurrent_batch_is_in_flight_until_the_whole_segment_ends() -> None:
    gate = asyncio.Event()
    tools = [
        FakeTool("s1", read_only=True),
        FakeTool("s2", read_only=True, gate=gate),
    ]
    pattern = _pattern(parallel=True, max_concurrency=2)
    pattern.pending_tool_calls = [make_tool_call("s1"), make_tool_call("s2")]
    runtime = PatternRuntime(execution_id="batch-in-flight")

    run = asyncio.create_task(
        pattern._execute_pending_tool_calls(
            context=RecordingContext(), tools=tools, llm=None, runtime=runtime
        )
    )
    await _wait_for_call(tools[1])
    # s1 may already be done; the batch stays pending until s2 returns.
    assert runtime.last_checkpoint["label"] == "before_tool_batch"
    assert checkpoint_tool_call_in_flight(runtime.last_checkpoint) is True

    gate.set()
    await run
    assert runtime.last_checkpoint["label"] == "after_tool_batch"
    assert checkpoint_tool_call_in_flight(runtime.last_checkpoint) is False


@pytest.mark.asyncio
async def test_calls_planned_after_llm_have_not_started() -> None:
    pattern = _pattern()
    pattern.pending_tool_calls = [make_tool_call("u1")]
    runtime = PatternRuntime(execution_id="planned")

    checkpoint = await runtime.checkpoint(
        "after_llm", context=RecordingContext(), pattern=pattern
    )

    assert checkpoint["pattern_state"]["pending_tool_calls"]
    assert checkpoint_tool_call_in_flight(checkpoint) is False


@pytest.mark.asyncio
async def test_control_tool_is_in_flight_from_before_tool_until_its_status() -> None:
    # send_message delivers its message inside the call. The latest checkpoint
    # at that moment must already say a tool call started, or a run stopped
    # right after the delivery would resume from ``after_llm`` and send it again.
    at_delivery: list[dict[str, Any]] = []
    runtime = PatternRuntime(execution_id="control-in-flight")
    runtime.outbound_message_handler = lambda _payload: at_delivery.append(
        runtime.last_checkpoint
    )
    pattern = _pattern()
    pattern.pending_tool_calls = [
        make_tool_call(
            "send_message",
            {"message": "hello", "message_type": "info", "expect_response": False},
        )
    ]

    await pattern._execute_pending_tool_calls(
        context=RecordingContext(), tools=[], llm=None, runtime=runtime
    )

    [checkpoint] = at_delivery
    assert checkpoint["label"] == "before_tool"
    assert checkpoint_tool_call_in_flight(checkpoint) is True
    # Delivered and recorded: the call is no longer pending.
    assert runtime.last_checkpoint["label"] == "message_sent"
    assert checkpoint_tool_call_in_flight(runtime.last_checkpoint) is False


@pytest.mark.asyncio
async def test_final_answer_writes_no_before_tool() -> None:
    # Nothing a final_answer does is visible before its own ``final``
    # checkpoint, so it is replayed, not closed as interrupted, if that write
    # fails or the process dies first.
    runtime = PatternRuntime(execution_id="final-no-start")
    pattern = _pattern()
    pattern.pending_tool_calls = [
        make_tool_call("final_answer", {"answer": "Done", "outcome": "completed"})
    ]

    await pattern._execute_pending_tool_calls(
        context=RecordingContext(), tools=[], llm=None, runtime=runtime
    )

    labels = [checkpoint["label"] for checkpoint in runtime.checkpoints]
    assert labels and "before_tool" not in labels


@pytest.mark.asyncio
async def test_ask_user_question_is_in_flight_from_before_tool() -> None:
    at_delivery: list[dict[str, Any]] = []
    runtime = PatternRuntime(execution_id="question-in-flight")
    runtime.outbound_message_handler = lambda _payload: at_delivery.append(
        runtime.last_checkpoint
    )
    pattern = _pattern()
    pattern.pending_tool_calls = [
        make_tool_call(
            "ask_user_question", {"message": "Which file?", "interactions": []}
        )
    ]

    await pattern._execute_pending_tool_calls(
        context=RecordingContext(), tools=[], llm=None, runtime=runtime
    )

    [checkpoint] = at_delivery
    assert checkpoint["label"] == "before_tool"
    assert checkpoint_tool_call_in_flight(checkpoint) is True


@pytest.mark.asyncio
async def test_control_segment_between_work_calls_writes_its_own_before_tool() -> None:
    tools = [FakeTool("u1"), FakeTool("u2")]
    pattern = _pattern()
    pattern.pending_tool_calls = [
        make_tool_call("u1"),
        make_tool_call(
            "send_message",
            {"message": "hello", "message_type": "info", "expect_response": False},
        ),
        make_tool_call("u2"),
    ]
    runtime = PatternRuntime(execution_id="control-between")

    await pattern._execute_pending_tool_calls(
        context=RecordingContext(), tools=tools, llm=None, runtime=runtime
    )

    assert [checkpoint["label"] for checkpoint in runtime.checkpoints] == [
        "before_tool",
        "after_tool",
        "before_tool",  # the control segment
        "message_sent",
        "before_tool",
        "after_tool",
    ]
    # The pending plan still lists the send_message call at its before_tool.
    control_start = runtime.checkpoints[2]
    assert [
        call["name"] for call in control_start["pattern_state"]["pending_tool_calls"]
    ] == ["send_message", "u2"]


@pytest.mark.parametrize(
    "label, expected",
    [
        ("auto_child_before_tool", True),
        ("auto_child_before_tool_batch", True),
        ("auto_child_after_llm", False),
        ("auto_child_after_tool", False),
    ],
)
def test_auto_child_uses_its_own_label(label: str, expected: bool) -> None:
    checkpoint = {
        "label": label,
        "metadata": {"child_label": label.removeprefix("auto_child_")},
        "pattern_state": {"react_state": _PENDING, "dag_state": None},
    }
    assert checkpoint_tool_call_in_flight(checkpoint) is expected


def _dag_checkpoint(
    label: str, writer: str, step_states: dict[str, Any]
) -> dict[str, Any]:
    return {
        "label": label,
        "metadata": {"active_step_id": writer, "child_label": label[len("dag_") :]},
        "pattern_state": {
            "plan": {"steps": [{"id": step} for step in step_states]},
            "active_step_pattern_states": step_states,
        },
    }


@pytest.mark.parametrize(
    "label, expected",
    [
        ("dag_before_tool", True),
        ("dag_before_tool_batch", True),
        ("dag_after_llm", False),
        ("dag_after_tool", False),
    ],
)
def test_dag_writer_step_uses_the_label(label: str, expected: bool) -> None:
    checkpoint = _dag_checkpoint(label, "a", {"a": _PENDING, "b": _IDLE})
    assert checkpoint_tool_call_in_flight(checkpoint) is expected


def test_dag_sibling_with_pending_calls_counts_as_in_flight() -> None:
    # Steps run concurrently: "b" may be inside a tool call even though the
    # latest checkpoint was written by "a" after its LLM call.
    checkpoint = _dag_checkpoint("dag_after_llm", "a", {"a": _IDLE, "b": _PENDING})
    assert checkpoint_tool_call_in_flight(checkpoint) is True


def test_dag_legacy_singular_step_state_is_read() -> None:
    checkpoint = {
        "label": "dag_before_tool",
        "metadata": {"active_step_id": "a"},
        "pattern_state": {
            "active_step_id": "a",
            "active_step_pattern_state": _PENDING,
        },
    }
    assert checkpoint_tool_call_in_flight(checkpoint) is True


def test_auto_dag_step_label_carries_both_prefixes() -> None:
    checkpoint = {
        "label": "auto_child_dag_before_tool",
        "metadata": {"active_step_id": "a", "child_label": "before_tool"},
        "pattern_state": {
            "react_state": None,
            "dag_state": {"active_step_pattern_states": {"a": _PENDING}},
        },
    }
    assert checkpoint_tool_call_in_flight(checkpoint) is True


@pytest.mark.parametrize(
    "checkpoint",
    [
        None,
        {},
        {"label": "before_tool", "pattern_state": None},
        {"label": "before_tool", "pattern_state": _IDLE},
        {"label": None, "pattern_state": _PENDING},
        {"label": "before_tool", "pattern_state": {"pending_tool_calls": "bad"}},
        {
            "label": "dag_before_tool",
            "metadata": "bad",
            "pattern_state": {"active_step_pattern_states": ["bad"]},
        },
    ],
)
def test_missing_or_malformed_fields_mean_no_call_in_flight(checkpoint: Any) -> None:
    assert checkpoint_tool_call_in_flight(checkpoint) is False


# --- mark_possibly_started_tool_calls ---------------------------------------


def _marks(state: Any) -> bool:
    return state.get(TOOL_CALLS_MAY_HAVE_STARTED_KEY) is True


@pytest.mark.parametrize(
    "label, expected",
    [
        ("before_tool", True),
        ("before_tool_batch", True),
        # Planned after the LLM response, not yet started: replayed as it is.
        ("after_llm", False),
        ("after_tool", False),
        ("interrupted", False),
    ],
)
def test_single_react_state_is_marked_from_the_tool_start_labels(
    label: str, expected: bool
) -> None:
    checkpoint = {"label": label, "pattern_state": copy.deepcopy(_PENDING)}

    marked = mark_possibly_started_tool_calls(checkpoint)

    assert _marks(marked["pattern_state"]) is expected
    assert checkpoint_tool_call_in_flight(checkpoint) is expected


def test_marking_does_not_modify_the_checkpoint_it_is_given() -> None:
    # The caller may hold the checkpoint elsewhere (a runtime's last
    # checkpoint, a merge baseline); a flag left there would outlive the resume.
    checkpoint = {"label": "before_tool", "pattern_state": copy.deepcopy(_PENDING)}
    original = copy.deepcopy(checkpoint)

    marked = mark_possibly_started_tool_calls(checkpoint)

    assert marked is not checkpoint
    assert checkpoint == original
    assert TOOL_CALLS_MAY_HAVE_STARTED_KEY not in checkpoint["pattern_state"]


def test_a_checkpoint_with_nothing_to_mark_is_returned_as_it_is() -> None:
    checkpoint = {"label": "after_llm", "pattern_state": copy.deepcopy(_PENDING)}

    assert mark_possibly_started_tool_calls(checkpoint) is checkpoint


def test_marking_ignores_idle_react_states() -> None:
    checkpoint = {"label": "before_tool", "pattern_state": copy.deepcopy(_IDLE)}

    assert mark_possibly_started_tool_calls(checkpoint) is checkpoint


@pytest.mark.parametrize(
    "label, marked_steps",
    [
        # The writer started its calls; the idle sibling has none to mark.
        ("dag_before_tool", {"a"}),
        ("dag_before_tool_batch", {"a"}),
        # The writer's calls had not started.
        ("dag_after_llm", set()),
    ],
)
def test_dag_marks_the_writer_step_by_label(label: str, marked_steps: set[str]) -> None:
    checkpoint = _dag_checkpoint(
        label, "a", {"a": copy.deepcopy(_PENDING), "b": copy.deepcopy(_IDLE)}
    )

    marked = mark_possibly_started_tool_calls(checkpoint)

    steps = marked["pattern_state"]["active_step_pattern_states"]
    assert {step for step, state in steps.items() if _marks(state)} == marked_steps


def test_dag_marks_a_concurrent_step_even_when_the_writer_had_not_started() -> None:
    # "a" wrote after its LLM response, so its calls are replayed; "b" ran
    # concurrently and may be inside a tool call, so its calls are closed.
    checkpoint = _dag_checkpoint(
        "dag_after_llm",
        "a",
        {"a": copy.deepcopy(_PENDING), "b": copy.deepcopy(_PENDING)},
    )

    marked = mark_possibly_started_tool_calls(checkpoint)

    steps = marked["pattern_state"]["active_step_pattern_states"]
    assert not _marks(steps["a"])
    assert _marks(steps["b"])
    # Neither the checkpoint nor its step states are touched.
    assert not _marks(checkpoint["pattern_state"]["active_step_pattern_states"]["b"])


def test_dag_marks_both_when_the_writer_started_and_a_sibling_ran() -> None:
    checkpoint = _dag_checkpoint(
        "dag_before_tool",
        "a",
        {"a": copy.deepcopy(_PENDING), "b": copy.deepcopy(_PENDING)},
    )

    steps = mark_possibly_started_tool_calls(checkpoint)["pattern_state"][
        "active_step_pattern_states"
    ]

    assert _marks(steps["a"]) and _marks(steps["b"])


def test_dag_legacy_singular_step_state_is_marked_where_load_state_reads_it() -> None:
    checkpoint = {
        "label": "dag_before_tool",
        "metadata": {"active_step_id": "a"},
        "pattern_state": {
            "active_step_id": "a",
            "active_step_pattern_state": copy.deepcopy(_PENDING),
        },
    }

    marked = mark_possibly_started_tool_calls(checkpoint)

    assert _marks(marked["pattern_state"]["active_step_pattern_state"])


@pytest.mark.parametrize(
    "label, expected",
    [
        ("auto_child_before_tool", True),
        ("auto_child_before_tool_batch", True),
        ("auto_child_after_llm", False),
    ],
)
def test_auto_nested_react_state_is_marked_by_its_own_label(
    label: str, expected: bool
) -> None:
    checkpoint = {
        "label": label,
        "pattern_state": {"react_state": copy.deepcopy(_PENDING), "dag_state": None},
    }

    marked = mark_possibly_started_tool_calls(checkpoint)

    assert _marks(marked["pattern_state"]["react_state"]) is expected


def test_auto_nested_dag_step_state_is_marked() -> None:
    checkpoint = {
        "label": "auto_child_dag_before_tool",
        "metadata": {"active_step_id": "a"},
        "pattern_state": {
            "react_state": None,
            "dag_state": {
                "active_step_pattern_states": {
                    "a": copy.deepcopy(_PENDING),
                    "b": copy.deepcopy(_PENDING),
                }
            },
        },
    }

    steps = mark_possibly_started_tool_calls(checkpoint)["pattern_state"]["dag_state"][
        "active_step_pattern_states"
    ]

    assert _marks(steps["a"]) and _marks(steps["b"])


@pytest.mark.parametrize(
    "checkpoint",
    [
        # A DAG checkpoint written right after a resume restored the step
        # (``dag_before_step``) or an Auto one before its child ran
        # (``auto_before_child``): its label says nothing about the calls, the
        # mark on the restored dict is what remains of "may have started".
        _dag_checkpoint(
            "dag_before_step",
            "a",
            {"a": {**_PENDING, TOOL_CALLS_MAY_HAVE_STARTED_KEY: True}},
        ),
        {
            "label": "auto_before_child",
            "pattern_state": {
                "react_state": {**_PENDING, TOOL_CALLS_MAY_HAVE_STARTED_KEY: True}
            },
        },
    ],
)
def test_a_state_that_is_already_marked_stays_in_flight_under_any_label(
    checkpoint: dict[str, Any],
) -> None:
    assert checkpoint_tool_call_in_flight(checkpoint) is True

    # Marking again is idempotent.
    again = mark_possibly_started_tool_calls(checkpoint)
    assert again["pattern_state"] == checkpoint["pattern_state"]


def test_an_unmarked_state_beside_a_marked_one_follows_its_own_label() -> None:
    checkpoint = _dag_checkpoint(
        "dag_before_step",
        "a",
        {"a": {**_PENDING, TOOL_CALLS_MAY_HAVE_STARTED_KEY: True}, "b": _IDLE},
    )

    steps = mark_possibly_started_tool_calls(checkpoint)["pattern_state"][
        "active_step_pattern_states"
    ]

    assert _marks(steps["a"])
    assert not _marks(steps["b"])


@pytest.mark.parametrize(
    "checkpoint",
    [
        {},
        {"label": "before_tool", "pattern_state": None},
        {"label": "before_tool", "pattern_state": {"pending_tool_calls": "bad"}},
        {
            "label": "dag_before_tool",
            "metadata": "bad",
            "pattern_state": {"active_step_pattern_states": ["bad"]},
        },
    ],
)
def test_marking_tolerates_missing_or_malformed_fields(checkpoint: Any) -> None:
    assert mark_possibly_started_tool_calls(checkpoint) is checkpoint


# --- rewrite_checkpoint -------------------------------------------------------


@pytest.mark.parametrize(
    "label, expected",
    [("before_tool", True), ("before_tool_batch", True), ("after_llm", False)],
)
def test_a_rewritten_checkpoint_keeps_the_in_flight_fact_of_its_base(
    label: str, expected: bool
) -> None:
    # A user message injected into the run re-persists the last checkpoint
    # under a label of its own; the pending calls it carries are still the
    # base's.
    base = {"label": label, "pattern_state": copy.deepcopy(_PENDING)}

    rewritten = rewrite_checkpoint(base, label="user_message_injected", context={})

    assert rewritten["label"] == "user_message_injected"
    assert rewritten["metadata"][BASE_LABEL_METADATA_KEY] == label
    assert checkpoint_tool_call_in_flight(rewritten) is expected
    marked = mark_possibly_started_tool_calls(rewritten)
    assert _marks(marked["pattern_state"]) is expected


def test_rewriting_a_rewrite_keeps_the_original_label() -> None:
    base = {"label": "before_tool", "pattern_state": copy.deepcopy(_PENDING)}
    first = rewrite_checkpoint(base, label="user_message_injected")

    second = rewrite_checkpoint(first, label="user_message_trace_watermark")

    assert second["metadata"][BASE_LABEL_METADATA_KEY] == "before_tool"
    assert checkpoint_tool_call_in_flight(second) is True


def test_rewrite_keeps_the_base_metadata_and_does_not_modify_the_base() -> None:
    base = {
        "label": "dag_before_tool",
        "metadata": {"active_step_id": "a", "child_label": "before_tool"},
        "pattern_state": {
            "plan": {"steps": [{"id": "a"}]},
            "active_step_pattern_states": {"a": copy.deepcopy(_PENDING)},
        },
    }
    original = copy.deepcopy(base)

    rewritten = rewrite_checkpoint(base, label="user_message_injected", status="x")

    assert base == original
    assert rewritten["status"] == "x"
    assert rewritten["metadata"]["active_step_id"] == "a"
    assert checkpoint_tool_call_in_flight(rewritten) is True


def test_rewrite_without_a_base_has_nothing_to_remember() -> None:
    rewritten = rewrite_checkpoint(None, label="user_message_injected", context={})

    assert rewritten == {"label": "user_message_injected", "context": {}}
