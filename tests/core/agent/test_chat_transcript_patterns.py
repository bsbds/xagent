from typing import Any, Dict, List

import pytest

from tests.utils.mock_llm import MockLLM
from xagent.core.agent.context import AgentContext
from xagent.core.agent.pattern.react import ReActPattern
from xagent.core.agent.service import AgentService
from xagent.core.memory.in_memory import InMemoryMemoryStore


@pytest.mark.asyncio
async def test_dag_pattern_continuation_records_user_turn():
    agent_service = AgentService(
        name="transcript_agent",
        id="transcript_agent_id",
        llm=MockLLM(),
        tools=[],
        use_dag_pattern=True,
    )

    agent_service.set_conversation_history(
        [{"role": "assistant", "content": "The top risks are A, B, and C."}]
    )

    dag_pattern = agent_service.get_dag_pattern()
    assert dag_pattern is not None

    dag_pattern.request_continuation("Expand risk B")

    assert dag_pattern._get_messages_for_llm() == [
        {"role": "assistant", "content": "The top risks are A, B, and C."},
        {"role": "user", "content": "Expand risk B"},
    ]


@pytest.mark.asyncio
async def test_react_pattern_includes_loaded_transcript_before_current_turn():
    captured_messages: Dict[str, List[Dict[str, str]]] = {}

    react_pattern = ReActPattern(llm=MockLLM())
    react_pattern.set_conversation_history(
        [{"role": "assistant", "content": "The previous answer was about persistence."}]
    )

    async def fake_execute_react_loop(
        messages: List[Dict[str, str]],
        task_id: str,
        step_id: str,
        max_iterations: int,
        task_description: str = "task",
    ) -> Dict[str, Any]:
        captured_messages["messages"] = messages
        return {"success": True, "output": "ok"}

    react_pattern._execute_react_loop = fake_execute_react_loop  # type: ignore[method-assign]

    await react_pattern.run(
        task="Continue that explanation",
        memory=InMemoryMemoryStore(),
        tools=[],
        context=AgentContext(task_id="react_task"),
    )

    assert captured_messages["messages"][1:] == [
        {"role": "assistant", "content": "The previous answer was about persistence."},
        {"role": "user", "content": "Continue that explanation"},
    ]


def test_dag_pattern_reset_can_preserve_loaded_transcript():
    agent_service = AgentService(
        name="transcript_agent",
        id="transcript_agent_id",
        llm=MockLLM(),
        tools=[],
        use_dag_pattern=True,
    )

    agent_service.set_conversation_history(
        [
            {
                "role": "assistant",
                "content": "The previous answer was about persistence.",
            },
            {"role": "user", "content": "Continue that explanation"},
        ]
    )

    dag_pattern = agent_service.get_dag_pattern()
    assert dag_pattern is not None

    dag_pattern.current_plan = object()  # type: ignore[assignment]
    dag_pattern.step_execution_results["step_1"] = object()  # type: ignore[assignment]

    dag_pattern.reset_execution_state(preserve_conversation_history=True)

    assert dag_pattern.current_plan is None
    assert dag_pattern.step_execution_results == {}
    assert dag_pattern._get_messages_for_llm() == [
        {
            "role": "assistant",
            "content": "The previous answer was about persistence.",
        },
        {"role": "user", "content": "Continue that explanation"},
    ]


def test_dag_pattern_includes_execution_context_before_transcript():
    agent_service = AgentService(
        name="transcript_agent",
        id="transcript_agent_id",
        llm=MockLLM(),
        tools=[],
        use_dag_pattern=True,
    )

    agent_service.set_execution_context_messages(
        [
            {
                "role": "system",
                "content": "Previous execution found files: foo.docx, bar.pdf",
            }
        ]
    )
    agent_service.set_conversation_history(
        [{"role": "assistant", "content": "The previous answer was about persistence."}]
    )

    dag_pattern = agent_service.get_dag_pattern()
    assert dag_pattern is not None
    assert dag_pattern._get_messages_for_llm() == [
        {
            "role": "system",
            "content": "Previous execution found files: foo.docx, bar.pdf",
        },
        {
            "role": "assistant",
            "content": "The previous answer was about persistence.",
        },
    ]
