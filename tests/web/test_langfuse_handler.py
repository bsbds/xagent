from __future__ import annotations

from itertools import count
from typing import Any

import pytest

from xagent.core.agent.trace import (
    TASK_END_GENERAL,
    TraceAction,
    TraceCategory,
    TraceEvent,
    TraceEventType,
    TraceScope,
)
from xagent.core.tracing.langfuse import (
    create_langfuse_trace_handler,
    initialize_langfuse,
    reset_langfuse_client,
)


class FakeObservation:
    _ids = count(1)

    def __init__(
        self,
        *,
        name: str,
        as_type: str,
        input: Any = None,
        output: Any = None,
        metadata: Any = None,
        **kwargs: Any,
    ) -> None:
        self.id = f"obs-{next(self._ids)}"
        self.trace_id = kwargs.pop("trace_id", self.id)
        self.name = name
        self.as_type = as_type
        self.input = input
        self.output = output
        self.metadata = metadata
        self.kwargs = kwargs
        self.children: list[FakeObservation] = []
        self.update_calls: list[dict[str, Any]] = []
        self.trace_update_calls: list[dict[str, Any]] = []
        self.end_calls = 0

    def start_observation(self, **kwargs: Any) -> "FakeObservation":
        child = FakeObservation(**kwargs)
        self.children.append(child)
        return child

    def update(self, **kwargs: Any) -> "FakeObservation":
        self.update_calls.append(kwargs)
        return self

    def update_trace(self, **kwargs: Any) -> "FakeObservation":
        self.trace_update_calls.append(kwargs)
        return self

    def end(self, **kwargs: Any) -> "FakeObservation":
        del kwargs
        self.end_calls += 1
        return self


class FakeLangfuseClient:
    def __init__(self) -> None:
        self.root_observations: list[FakeObservation] = []
        self.observations_by_id: dict[str, FakeObservation] = {}
        self.flush_calls = 0

    def start_observation(self, **kwargs: Any) -> FakeObservation:
        trace_context = kwargs.pop("trace_context", None)
        trace_id = (
            trace_context["trace_id"]
            if isinstance(trace_context, dict) and "trace_id" in trace_context
            else None
        )
        observation = FakeObservation(trace_id=trace_id, **kwargs)
        self.observations_by_id[observation.id] = observation

        if (
            isinstance(trace_context, dict)
            and trace_context.get("parent_span_id") in self.observations_by_id
        ):
            parent = self.observations_by_id[trace_context["parent_span_id"]]
            parent.children.append(observation)
        else:
            self.root_observations.append(observation)

        return observation

    def flush(self) -> None:
        self.flush_calls += 1


@pytest.fixture(autouse=True)
def reset_shared_langfuse_state() -> None:
    reset_langfuse_client()
    yield
    reset_langfuse_client()


def test_langfuse_disabled_without_required_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_TRACING_ENABLED", raising=False)

    assert initialize_langfuse() is False
    assert (
        create_langfuse_trace_handler(task_id="task-1", user_id=1, tags=["xagent"])
        is None
    )


@pytest.mark.asyncio
async def test_langfuse_handler_forwards_llm_and_completion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_client = FakeLangfuseClient()

    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-test")
    monkeypatch.setattr(
        "xagent.core.tracing.langfuse.client.Langfuse", lambda: fake_client
    )

    handler = create_langfuse_trace_handler(
        task_id="task-1",
        user_id=7,
        trace_name="xagent-web-task-task-1",
        session_id="task:task-1",
        tags=["xagent", "web", "task"],
    )
    assert handler is not None

    user_message_event = TraceEvent(
        TraceEventType(TraceScope.TASK, TraceAction.START, TraceCategory.MESSAGE),
        task_id="task-1",
        data={"message": "hello world"},
    )
    llm_start_event = TraceEvent(
        TraceEventType(TraceScope.ACTION, TraceAction.START, TraceCategory.LLM),
        task_id="task-1",
        step_id="step-1",
        data={
            "model_name": "gpt-4o-mini",
            "messages": [{"role": "user", "content": "hello world"}],
        },
    )
    llm_end_event = TraceEvent(
        TraceEventType(TraceScope.ACTION, TraceAction.END, TraceCategory.LLM),
        task_id="task-1",
        step_id="step-1",
        data={
            "model_name": "gpt-4o-mini",
            "response": {"answer": "done"},
            "usage": {"prompt_tokens": 3, "completion_tokens": 5},
        },
    )
    completion_event = TraceEvent(
        TASK_END_GENERAL,
        task_id="task-1",
        data={"result": "final result", "success": True},
    )

    await handler.handle_event(user_message_event)
    await handler.handle_event(llm_start_event)
    await handler.handle_event(llm_end_event)
    await handler.handle_event(completion_event)

    assert len(fake_client.root_observations) == 1
    root = fake_client.root_observations[0]

    assert root.trace_update_calls[0]["user_id"] == "7"
    assert root.trace_update_calls[0]["session_id"] == "task:task-1"
    assert root.trace_update_calls[-1]["output"] == "final result"
    assert root.end_calls == 1

    generation_children = [
        child for child in root.children if child.as_type == "generation"
    ]
    assert len(generation_children) == 1
    generation = generation_children[0]
    assert generation.kwargs["model"] == "gpt-4o-mini"
    assert generation.update_calls[-1]["output"]["response"] == {"answer": "done"}
    assert generation.update_calls[-1]["usage_details"] == {
        "prompt_tokens": 3,
        "completion_tokens": 5,
    }
    assert generation.end_calls == 1


@pytest.mark.asyncio
async def test_langfuse_handler_maps_task_scope_llm_to_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_client = FakeLangfuseClient()

    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-test")
    monkeypatch.setattr(
        "xagent.core.tracing.langfuse.client.Langfuse", lambda: fake_client
    )

    handler = create_langfuse_trace_handler(
        task_id="task-2",
        user_id=9,
        trace_name="xagent-web-task-task-2",
        session_id="task:task-2",
        tags=["xagent", "web", "task"],
    )
    assert handler is not None

    task_llm_start = TraceEvent(
        TraceEventType(TraceScope.TASK, TraceAction.START, TraceCategory.LLM),
        task_id="plan_generation_call",
        data={
            "task_type": "plan_generation",
            "model_name": "gpt-4o-mini",
            "messages": [{"role": "user", "content": "plan this"}],
        },
    )
    task_llm_end = TraceEvent(
        TraceEventType(TraceScope.TASK, TraceAction.END, TraceCategory.LLM),
        task_id="plan_generation_call",
        data={
            "task_type": "plan_generation",
            "model_name": "gpt-4o-mini",
            "response": {"plan": {"steps": []}},
            "usage": {"prompt_tokens": 11, "completion_tokens": 17},
            "success": True,
        },
    )

    await handler.handle_event(task_llm_start)
    await handler.handle_event(task_llm_end)

    root = fake_client.root_observations[0]
    generation_children = [
        child for child in root.children if child.as_type == "generation"
    ]
    assert len(generation_children) == 1
    generation = generation_children[0]
    assert generation.name == "llm_plan_generation_start"
    assert generation.update_calls[-1]["output"]["response"] == {"plan": {"steps": []}}
    assert generation.update_calls[-1]["usage_details"] == {
        "prompt_tokens": 11,
        "completion_tokens": 17,
    }
    assert generation.end_calls == 1
