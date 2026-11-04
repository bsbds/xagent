"""Resuming a run whose tool calls may have started closes them, not replays them.

Without a per-attempt tool ledger (conversation storage version 1) a checkpoint
written at ``before_tool`` / ``before_tool_batch`` cannot say whether its pending
calls ran, or what they did. The resume path therefore gives each of them a
failed "interrupted, outcome unknown" result and lets the model replan, rather
than executing a message send or a write a second time. Calls the checkpoint
says had not started (``after_llm``) are still replayed as they were planned.

Every case here is a real run killed at a checkpoint (``CrashAtCheckpoint``) and
resumed from what it left behind, the way the runner does it:
``load_pattern_checkpoint`` and then ``pattern.run``.
"""

from __future__ import annotations

import copy
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import BaseModel

from tests.core.agent.crash_resume_harness import Crash, CrashAtCheckpoint
from xagent.core.agent import ExecutionContext, PatternRuntime, ReActPattern
from xagent.core.agent.checkpoint import TOOL_CALLS_MAY_HAVE_STARTED_KEY
from xagent.core.agent.pattern.react.react import INTERRUPTED_TOOL_CALL_ERROR
from xagent.core.agent.runtime import load_pattern_checkpoint


class _Args(BaseModel):
    value: str = ""


class _Tool:
    """A tool that records what it was asked to do."""

    def __init__(self, name: str, *, concurrency_safe: bool = False) -> None:
        self.calls: list[dict[str, Any]] = []
        self.metadata = SimpleNamespace(
            name=name,
            description=f"{name} test tool.",
            concurrency_safe=concurrency_safe,
            read_only=concurrency_safe,
        )

    def args_type(self) -> type[BaseModel]:
        return _Args

    async def run_json_async(self, args: dict[str, Any]) -> Any:
        self.calls.append(args)
        return {"success": True, "tool": self.metadata.name, "args": args}


class _LLM:
    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self.responses = responses
        self.calls: list[dict[str, Any]] = []

    async def chat(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return self.responses.pop(0)


def _calls_response(*names: str) -> dict[str, Any]:
    """One model turn that calls each named tool once, ids ``call_<name>``."""
    return {
        "content": "",
        "tool_calls": [
            {
                "id": f"call_{name}",
                "function": {"name": name, "arguments": '{"value": "go"}'},
            }
            for name in names
        ],
    }


_DONE = {"content": "Replanned and done.", "done": True}


def _pattern(*, parallel: bool = False) -> ReActPattern:
    return ReActPattern(
        max_iterations=5,
        tool_parallel_enabled=parallel,
        repeated_tool_decision_after_consecutive_tool_calls=None,
        repeated_tool_decision_after_consecutive_work_tool_calls=None,
    )


def _context() -> ExecutionContext:
    context = ExecutionContext(execution_id="resume-interrupted")
    context.add_user_message("Do the work")
    return context


async def _crash_at(
    label: str,
    tools: list[_Tool],
    *,
    names: tuple[str, ...] = ("alpha", "beta"),
    occurrence: int = 1,
    parallel: bool = False,
) -> dict[str, Any]:
    """Run until the process would die at ``label``; return that checkpoint."""
    runtime = CrashAtCheckpoint(label, occurrence=occurrence)
    with pytest.raises(Crash):
        await _pattern(parallel=parallel).run(
            context=_context(),
            tools=tools,
            llm=_LLM([_calls_response(*names), _DONE]),
            runtime=runtime,
        )
    assert runtime.last_checkpoint is not None
    assert runtime.last_checkpoint["label"] == label
    return runtime.last_checkpoint


async def _resume(
    checkpoint: dict[str, Any],
    tools: list[_Tool],
    responses: list[dict[str, Any]],
    *,
    close_started_tool_calls: bool = True,
    parallel: bool = False,
) -> tuple[ReActPattern, dict[str, Any], _LLM, PatternRuntime]:
    pattern = _pattern(parallel=parallel)
    load_pattern_checkpoint(
        pattern, checkpoint, close_started_tool_calls=close_started_tool_calls
    )
    llm = _LLM(responses)
    runtime = PatternRuntime()
    result = await pattern.run(
        context=ExecutionContext.from_dict(checkpoint["context"]),
        tools=tools,
        llm=llm,
        runtime=runtime,
    )
    return pattern, result, llm, runtime


def _tool_messages(llm: _LLM, call_index: int = 0) -> list[dict[str, Any]]:
    return [m for m in llm.calls[call_index]["messages"] if m["role"] == "tool"]


def _assert_closed_as_interrupted(
    pattern: ReActPattern, llm: _LLM, call_ids: list[str]
) -> None:
    # Exactly one result per call (I2), in call order, each telling the model
    # the outcome is unknown, and the ledger agrees.
    messages = _tool_messages(llm)
    assert [m["tool_call_id"] for m in messages] == call_ids
    assert all("interrupted" in m["content"] for m in messages)
    assert all("may or may not have taken effect" in m["content"] for m in messages)
    for call_id in call_ids:
        record = pattern._record_for_tool_call_id(call_id)
        assert record is not None
        assert record.status == "interrupted"
        assert record.result == {
            "success": False,
            "status": "interrupted",
            "error": INTERRUPTED_TOOL_CALL_ERROR,
        }


def _assert_no_mark_or_stale_plan_in_later_checkpoints(runtime: PatternRuntime) -> None:
    assert runtime.checkpoints
    for checkpoint in runtime.checkpoints:
        state = checkpoint["pattern_state"]
        assert TOOL_CALLS_MAY_HAVE_STARTED_KEY not in state
        assert state["pending_tool_calls"] == []


@pytest.mark.asyncio
async def test_serial_calls_that_may_have_started_are_closed_and_the_model_replans() -> (
    None
):
    checkpoint = await _crash_at("before_tool", [_Tool("alpha"), _Tool("beta")])
    original = copy.deepcopy(checkpoint)
    tools = [_Tool("alpha"), _Tool("beta")]

    pattern, result, llm, runtime = await _resume(checkpoint, tools, [_DONE])

    assert result["success"] is True
    assert result["response"] == "Replanned and done."
    # Nothing ran: the next thing that happened was the model call.
    assert [tool.calls for tool in tools] == [[], []]
    assert len(llm.calls) == 1
    _assert_closed_as_interrupted(pattern, llm, ["call_alpha", "call_beta"])
    _assert_no_mark_or_stale_plan_in_later_checkpoints(runtime)
    # The tool round is over, as after a normal pass: the next turn is the next
    # iteration.
    assert runtime.checkpoints[0]["pattern_state"]["current_iteration"] == 1
    # The checkpoint the runner loaded is left as it was.
    assert checkpoint == original


@pytest.mark.asyncio
async def test_a_concurrent_batch_that_may_have_started_is_closed() -> None:
    def tools() -> list[_Tool]:
        return [
            _Tool("alpha", concurrency_safe=True),
            _Tool("beta", concurrency_safe=True),
        ]

    checkpoint = await _crash_at("before_tool_batch", tools(), parallel=True)
    resumed_tools = tools()

    pattern, result, llm, runtime = await _resume(
        checkpoint, resumed_tools, [_DONE], parallel=True
    )

    assert result["success"] is True
    assert [tool.calls for tool in resumed_tools] == [[], []]
    _assert_closed_as_interrupted(pattern, llm, ["call_alpha", "call_beta"])
    _assert_no_mark_or_stale_plan_in_later_checkpoints(runtime)


@pytest.mark.asyncio
async def test_only_the_calls_still_pending_are_closed_and_finished_ones_keep_theirs() -> (
    None
):
    # alpha ran and its result is in the context; the process died at beta's
    # own before_tool. alpha is not touched, beta is closed.
    first_tools = [_Tool("alpha"), _Tool("beta")]
    checkpoint = await _crash_at("before_tool", first_tools, occurrence=2)
    assert len(first_tools[0].calls) == 1
    resumed_tools = [_Tool("alpha"), _Tool("beta")]

    pattern, result, llm, _ = await _resume(checkpoint, resumed_tools, [_DONE])

    assert result["success"] is True
    assert [tool.calls for tool in resumed_tools] == [[], []]
    messages = _tool_messages(llm)
    assert [m["tool_call_id"] for m in messages] == ["call_alpha", "call_beta"]
    assert "interrupted" not in messages[0]["content"]
    assert "interrupted" in messages[1]["content"]
    assert pattern._record_for_tool_call_id("call_alpha").status == "completed"
    assert pattern._record_for_tool_call_id("call_beta").status == "interrupted"


@pytest.mark.asyncio
async def test_calls_planned_after_the_llm_response_are_replayed_as_planned() -> None:
    checkpoint = await _crash_at("after_llm", [_Tool("alpha"), _Tool("beta")])
    tools = [_Tool("alpha"), _Tool("beta")]

    pattern, result, llm, runtime = await _resume(checkpoint, tools, [_DONE])

    assert result["success"] is True
    # They had not started, so they run now, once each, with the planned args.
    assert [tool.calls for tool in tools] == [[{"value": "go"}], [{"value": "go"}]]
    assert all("interrupted" not in m["content"] for m in _tool_messages(llm))
    assert pattern._record_for_tool_call_id("call_alpha").status == "completed"
    assert all(
        TOOL_CALLS_MAY_HAVE_STARTED_KEY not in checkpoint["pattern_state"]
        for checkpoint in runtime.checkpoints
    )


@pytest.mark.asyncio
async def test_a_run_that_records_tool_attempts_replays_its_pending_calls() -> None:
    # Version-2 storage judges started attempts from its own ledger, so the
    # runner does not ask for closing there: the state loads as it is.
    checkpoint = await _crash_at("before_tool", [_Tool("alpha"), _Tool("beta")])
    tools = [_Tool("alpha"), _Tool("beta")]

    _, result, llm, _ = await _resume(
        checkpoint, tools, [_DONE], close_started_tool_calls=False
    )

    assert result["success"] is True
    assert [tool.calls for tool in tools] == [[{"value": "go"}], [{"value": "go"}]]
    assert all("interrupted" not in m["content"] for m in _tool_messages(llm))


@pytest.mark.asyncio
async def test_the_mark_is_consumed_by_one_run_and_never_written_back() -> None:
    checkpoint = await _crash_at("before_tool", [_Tool("alpha"), _Tool("beta")])
    pattern = _pattern()
    load_pattern_checkpoint(pattern, checkpoint, close_started_tool_calls=True)

    # Loaded, but not part of anything the pattern emits.
    assert pattern._pending_calls_may_have_started is True
    assert TOOL_CALLS_MAY_HAVE_STARTED_KEY not in pattern.get_state()

    replan = _calls_response("alpha")
    replan["tool_calls"][0]["id"] = "call_replanned"
    replanned_tool = _Tool("alpha")
    await pattern.run(
        context=ExecutionContext.from_dict(checkpoint["context"]),
        tools=[replanned_tool, _Tool("beta")],
        llm=_LLM([replan, _DONE]),
        runtime=PatternRuntime(),
    )

    # The plan the model made after the replan runs normally: the consumed
    # mark did not close it too.
    assert pattern._pending_calls_may_have_started is False
    assert replanned_tool.calls == [{"value": "go"}]
    assert pattern._record_for_tool_call_id("call_replanned").status == "completed"
