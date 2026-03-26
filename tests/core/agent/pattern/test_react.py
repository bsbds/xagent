from typing import Any

import pytest

from xagent.core.agent.context import AgentContext
from xagent.core.agent.pattern.react import ReActPattern
from xagent.core.memory.base import MemoryResponse, MemoryStore
from xagent.core.model.chat.basic.base import BaseLLM
from xagent.core.model.chat.basic.openai_responses import OpenAIResponsesLLM
from xagent.core.model.chat.types import ChunkType, StreamChunk
from xagent.core.tools.adapters.vibe import Tool, ToolMetadata


class MockReActLLM(BaseLLM):
    def __init__(self, responses=None):
        self.responses = responses or []
        self.call_count = 0
        self._abilities = ["chat", "tool_calling"]
        self._model_name = "mock_react_llm"

    @property
    def supports_thinking_mode(self) -> bool:
        """Mock LLM doesn't support thinking mode"""
        return False

    @property
    def abilities(self) -> list[str]:
        """Get the list of abilities supported by this Mock LLM implementation."""
        return self._abilities

    @property
    def model_name(self) -> str:
        """Get the model name/identifier."""
        return self._model_name

    async def chat(self, messages: list[dict[str, str]], **kwargs) -> str:
        if self.call_count < len(self.responses):
            response = self.responses[self.call_count]
            self.call_count += 1
            return response

        # Default final answer matching new Action schema
        return '{"type": "final_answer", "reasoning": "Task completed successfully", "answer": "Task completed successfully", "success": true, "error": null}'


class MockCalculatorTool(Tool):
    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(name="calculator", description="Simple calculator")

    def args_type(self):
        return dict

    def return_type(self):
        return dict

    def state_type(self):
        return None

    def is_async(self):
        return True

    async def run_json_async(self, args: dict[str, Any]) -> Any:
        expression = args.get("expression", "")
        try:
            # Simple evaluation for testing
            result = eval(expression)  # Note: Only for testing, never use in production
            return {"result": result, "expression": expression}
        except Exception as e:
            return {"error": str(e), "expression": expression}

    def run_json_sync(self, args: dict[str, Any]) -> Any:
        return {"result": 42}

    async def save_state_json(self):
        return {}

    async def load_state_json(self, state: dict[str, Any]):
        pass

    def return_value_as_string(self, value: Any) -> str:
        return str(value)


class DummyMemoryStore(MemoryStore):
    def add(self, note):
        return MemoryResponse(success=True)

    def get(self, note_id: str):
        return MemoryResponse(success=True)

    def update(self, note):
        return MemoryResponse(success=True)

    def delete(self, note_id: str):
        return MemoryResponse(success=True)

    def search(self, query: str, k: int = 5, filters=None):
        return []

    def clear(self):
        pass

    def get_stats(self):
        return {}

    def list_all(self, limit: int = 100, offset: int = 0):
        return []


class MockWrappedResponsesLLM(BaseLLM):
    def __init__(self) -> None:
        self._model_name = "mock_wrapped_responses_llm"
        self._abilities = ["chat"]
        self._inner = OpenAIResponsesLLM(model_name="gpt-5.4-mini")
        self.captured_kwargs: dict[str, Any] | None = None

    @property
    def supports_thinking_mode(self) -> bool:
        return False

    @property
    def abilities(self) -> list[str]:
        return self._abilities

    @property
    def model_name(self) -> str:
        return self._model_name

    async def chat(self, messages: list[dict[str, str]], **kwargs) -> str:
        raise NotImplementedError

    async def stream_chat(self, messages: list[dict[str, str]], **kwargs):
        self.captured_kwargs = {"messages": messages, **kwargs}
        yield StreamChunk(
            type=ChunkType.TOKEN,
            delta='{"type":"final_answer","reasoning":"done","answer":"ok","success":true,"error":null}',
        )
        yield StreamChunk(type=ChunkType.USAGE, usage={})


@pytest.mark.asyncio
async def test_react_basic_execution():
    """Test basic ReAct pattern execution"""
    responses = [
        '{"type": "tool_call", "reasoning": "I need to calculate 2+2", "tool_name": "calculator", "tool_args": {"expression": "2+2"}}',
        '{"type": "final_answer", "reasoning": "The calculation is complete", "answer": "The result is 4", "success": true, "error": null}',
    ]

    llm = MockReActLLM(responses)
    memory = DummyMemoryStore()
    tools = [MockCalculatorTool()]
    pattern = ReActPattern(llm, max_iterations=5)

    result = await pattern.run(
        task="Calculate 2+2",
        memory=memory,
        tools=tools,
        context=AgentContext(),
    )

    assert result["success"] is True
    assert result["output"] == "The result is 4"
    assert result["iterations"] == 2
    assert "execution_history" in result


@pytest.mark.asyncio
async def test_react_with_context():
    """Test ReAct pattern with pre-built context"""
    responses = [
        '{"type": "final_answer", "reasoning": "Based on the context, the answer is 42", "answer": "Based on the context, the answer is 42", "success": true, "error": null}'
    ]

    llm = MockReActLLM(responses)
    tools = [MockCalculatorTool()]
    pattern = ReActPattern(llm, max_iterations=3)

    # Set step context as required
    pattern.set_step_context(step_id="test_step_1", step_name="test_step")

    messages = [
        {"role": "system", "content": "You are a helpful assistant"},
        {"role": "user", "content": "What is the ultimate answer?"},
        {"role": "assistant", "content": "Let me think about this..."},
    ]

    result = await pattern.run_with_context(
        messages=messages,
        tools=tools,
    )

    assert result["success"] is True
    assert result["output"] == "Based on the context, the answer is 42"
    assert "execution_history" in result


@pytest.mark.asyncio
async def test_react_tool_execution():
    """Test ReAct pattern with tool execution"""
    responses = [
        '{"type": "tool_call", "reasoning": "I need to calculate something", "tool_name": "calculator", "tool_args": {"expression": "10*5"}}',
        '{"type": "final_answer", "reasoning": "The calculation is complete", "answer": "The calculation result is 50", "success": true, "error": null}',
    ]

    llm = MockReActLLM(responses)
    memory = DummyMemoryStore()
    tools = [MockCalculatorTool()]
    pattern = ReActPattern(llm, max_iterations=5)

    result = await pattern.run(
        task="Calculate 10 times 5",
        memory=memory,
        tools=tools,
        context=AgentContext(),
    )

    assert result["success"] is True
    assert result["output"] == "The calculation result is 50"
    assert result["pattern"] == "react"


@pytest.mark.asyncio
async def test_react_max_iterations():
    """Test ReAct pattern hitting max iterations"""
    # Return tool calls that never lead to final answer
    responses = [
        '{"type": "tool_call", "reasoning": "I need to calculate something", "tool_name": "calculator", "tool_args": {"expression": "1+1"}}',
        '{"type": "tool_call", "reasoning": "I need to calculate more", "tool_name": "calculator", "tool_args": {"expression": "2+2"}}',
        '{"type": "tool_call", "reasoning": "Still calculating", "tool_name": "calculator", "tool_args": {"expression": "3+3"}}',
    ]

    llm = MockReActLLM(responses)
    memory = DummyMemoryStore()
    tools = [MockCalculatorTool()]
    pattern = ReActPattern(llm, max_iterations=2)  # Low max to trigger limit

    with pytest.raises(Exception):  # Should raise MaxIterationsError
        await pattern.run(
            task="Keep calculating without final answer",
            memory=memory,
            tools=tools,
            context=AgentContext(),
        )


@pytest.mark.asyncio
async def test_react_invalid_json():
    """Test ReAct pattern with invalid JSON response - should retry and eventually fail with MaxIterationsError"""
    responses = [
        "invalid json response",  # Invalid JSON triggers retry
        "invalid json response",  # Invalid JSON triggers retry
        "invalid json response",  # Invalid JSON triggers retry
    ]

    llm = MockReActLLM(responses)
    memory = DummyMemoryStore()
    tools = [MockCalculatorTool()]
    pattern = ReActPattern(llm, max_iterations=3)

    # With new retry logic, invalid JSON triggers retries until max_iterations
    # The default final answer will be used after exhausting responses
    result = await pattern.run(
        task="Test invalid response",
        memory=memory,
        tools=tools,
        context=AgentContext(),
    )

    # Should complete successfully after retries using default final answer
    assert result["success"] is True
    assert result["output"] == "Task completed successfully"


@pytest.mark.asyncio
async def test_react_tool_not_found():
    """Test ReAct pattern with non-existent tool"""
    responses = [
        '{"type": "tool_call", "reasoning": "Trying to use non-existent tool", "tool_name": "nonexistent", "tool_args": {}}',
        '{"type": "final_answer", "reasoning": "Could not complete task due to missing tool", "answer": "Could not complete task due to missing tool"}',
    ]

    llm = MockReActLLM(responses)
    memory = DummyMemoryStore()
    tools = [MockCalculatorTool()]
    pattern = ReActPattern(llm, max_iterations=3)

    # With backward compatibility, missing tool is handled gracefully
    result = await pattern.run(
        task="Use non-existent tool",
        memory=memory,
        tools=tools,
        context=AgentContext(),
    )

    # Should complete successfully with error message in output
    assert result["success"] is True
    assert "Could not complete task due to missing tool" in result["output"]


@pytest.mark.asyncio
async def test_react_none_response():
    """Test ReAct pattern with None response from LLM - should retry and eventually complete"""
    responses = [None, None, None]  # LLM returns None multiple times

    llm = MockReActLLM(responses)
    memory = DummyMemoryStore()
    tools = [MockCalculatorTool()]
    pattern = ReActPattern(llm, max_iterations=3)

    # With new retry logic, None triggers retries
    # After exhausting responses, default final answer is used
    result = await pattern.run(
        task="Test None response",
        memory=memory,
        tools=tools,
        context=AgentContext(),
    )

    # Should complete successfully after retries using default final answer
    assert result["success"] is True
    assert result["output"] == "Task completed successfully"


@pytest.mark.asyncio
async def test_react_self_reflection():
    """Test ReAct pattern with self-reflection on failure"""
    responses = [
        '{"type": "tool_call", "reasoning": "Trying calculator", "tool_name": "calculator", "tool_args": {"expression": "invalid expression"}}',
        '{"type": "tool_call", "reasoning": "The previous action failed, let me try a different approach", "tool_name": "calculator", "tool_args": {"expression": "2+2"}}',
        '{"type": "final_answer", "reasoning": "After retrying, the answer is 4", "answer": "After retrying, the answer is 4"}',
    ]

    llm = MockReActLLM(responses)
    memory = DummyMemoryStore()
    tools = [MockCalculatorTool()]
    pattern = ReActPattern(llm, max_iterations=5)

    result = await pattern.run(
        task="Calculate something with reflection",
        memory=memory,
        tools=tools,
        context=AgentContext(),
    )

    assert result["success"] is True
    assert result["output"] == "After retrying, the answer is 4"


@pytest.mark.asyncio
async def test_react_analysis_step():
    """Test ReAct pattern with analysis step (no tools)"""
    responses = [
        '{"type": "final_answer", "reasoning": "Based on the provided context, I can analyze this task", "answer": "This is an analysis result that synthesizes the information without using any tools."}',
    ]

    llm = MockReActLLM(responses)
    memory = DummyMemoryStore()
    tools = []  # No tools - this is an analysis step
    pattern = ReActPattern(llm, max_iterations=3)

    result = await pattern.run(
        task="Analyze the following information and provide a summary",
        memory=memory,
        tools=tools,
        context=AgentContext(),
    )

    # Should complete successfully with direct analysis
    assert result["success"] is True
    assert (
        result["output"]
        == "This is an analysis result that synthesizes the information without using any tools."
    )
    assert result["iterations"] == 1  # Should complete in one iteration for analysis


@pytest.mark.asyncio
async def test_react_analysis_step_with_context():
    """Test ReAct pattern with analysis step using context builder"""
    responses = [
        '{"type": "final_answer", "reasoning": "Based on the context from previous steps, I can provide a comprehensive analysis", "answer": "The analysis shows that the previous calculations were successful and the results are consistent."}',
    ]

    llm = MockReActLLM(responses)
    tools = []  # No tools - this is an analysis step
    pattern = ReActPattern(llm, max_iterations=3)

    # Set step context as required
    pattern.set_step_context(step_id="test_analysis_step", step_name="analysis_step")

    # Test with context messages (like from DAG plan execute)
    context_messages = [
        {
            "role": "system",
            "content": "You are a helpful assistant that analyzes results.",
        },
        {"role": "user", "content": "Previous step results: Calculation result was 42"},
        {
            "role": "user",
            "content": "Analyze the following information and provide a summary",
        },
    ]

    result = await pattern.run_with_context(
        messages=context_messages,
        tools=tools,
        max_iterations=3,
    )

    # Should complete successfully with contextual analysis
    assert result["success"] is True
    assert (
        "The analysis shows that the previous calculations were successful"
        in result["output"]
    )
    assert result["iterations"] == 1  # Should complete in one iteration for analysis


@pytest.mark.asyncio
async def test_react_failure_detection():
    """Test ReAct pattern failure detection when final_answer indicates failure"""
    responses = [
        '{"type": "final_answer", "reasoning": "The task cannot be completed", "answer": "I was unable to complete the task", "success": false, "error": "Required tool not available"}',
    ]

    llm = MockReActLLM(responses)
    memory = DummyMemoryStore()
    tools = []  # No tools available
    pattern = ReActPattern(llm, max_iterations=3)

    # With new behavior: success: false in final_answer returns failure directly
    # No retry occurs, LLM's assessment is respected
    result = await pattern.run(
        task="Complete a task that will fail",
        memory=memory,
        tools=tools,
        context=AgentContext(),
    )

    # Should return the failure result as provided by LLM
    assert result["success"] is False
    assert "unable to complete the task" in result["output"].lower()


@pytest.mark.asyncio
async def test_react_failure_detection_with_context():
    """Test ReAct pattern with success:false returns failure directly"""
    # LLM returns success:false, which should be returned as-is
    # No retry occurs, LLM's assessment is respected
    responses = [
        '{"type": "final_answer", "reasoning": "Cannot proceed due to missing dependencies", "answer": "TASK FAILED: Missing required data from previous steps", "success": false, "error": "Missing required data from previous steps"}',
    ]

    llm = MockReActLLM(responses)
    tools = []  # No tools available
    pattern = ReActPattern(llm, max_iterations=3)

    # Set step context as required
    pattern.set_step_context(step_id="test_failure_step", step_name="failure_step")

    # Test with context messages
    context_messages = [
        {
            "role": "system",
            "content": "You are a helpful assistant that analyzes results.",
        },
        {"role": "user", "content": "Complete a task that will fail"},
    ]

    # With new behavior: failure is returned directly, no retry
    result = await pattern.run_with_context(
        messages=context_messages,
        tools=tools,
        max_iterations=3,
    )

    # Should return the failure result as provided by LLM
    assert result["success"] is False
    assert "TASK FAILED" in result["output"]
    assert "Missing required data from previous steps" in result["output"]


@pytest.mark.asyncio
async def test_react_truncated_json():
    """Test ReAct pattern with truncated JSON response - should be repaired"""
    # Truncated JSON (missing closing brace)
    truncated_json = '{"type": "tool_call", "reasoning": "I need to calculate", "tool_name": "calculator", "tool_args": {"expression": "2+2"'

    responses = [truncated_json]

    llm = MockReActLLM(responses)
    memory = DummyMemoryStore()
    tools = [MockCalculatorTool()]
    pattern = ReActPattern(llm, max_iterations=3)

    # JSON is repaired
    result = await pattern.run(
        task="Test truncated JSON",
        memory=memory,
        tools=tools,
        context=AgentContext(),
    )

    assert result["success"] is True
    assert result["output"] == "Task completed successfully"
    assert "execution_history" in result


@pytest.mark.asyncio
async def test_react_successful_final_answer():
    """Test ReAct pattern successful final_answer with explicit success: true"""
    responses = [
        '{"type": "final_answer", "reasoning": "Task completed successfully", "answer": "The task has been completed successfully", "success": true, "error": null}',
    ]

    llm = MockReActLLM(responses)
    memory = DummyMemoryStore()
    tools = []
    pattern = ReActPattern(llm, max_iterations=3)

    result = await pattern.run(
        task="Complete a simple task",
        memory=memory,
        tools=tools,
        context=AgentContext(),
    )

    # Should complete successfully
    assert result["success"] is True
    assert result["output"] == "The task has been completed successfully"
    assert result["iterations"] == 1


@pytest.mark.asyncio
async def test_react_uses_action_schema_for_openai_responses():
    llm = MockWrappedResponsesLLM()
    pattern = ReActPattern(llm, max_iterations=1)
    pattern.set_step_context(step_id="schema_step", step_name="schema_test")

    result = await pattern.run_with_context(
        messages=[
            {"role": "system", "content": "You are a helpful assistant"},
            {"role": "user", "content": "Return a final answer"},
        ],
        tools=[],
        max_iterations=1,
    )

    assert result["success"] is True
    assert llm.captured_kwargs is not None
    assert "output_config" in llm.captured_kwargs
    assert "response_format" not in llm.captured_kwargs
    assert (
        llm.captured_kwargs["output_config"]["format"]["schema"]["properties"]["type"]["title"]
        == "Type"
    )
    schema = llm.captured_kwargs["output_config"]["format"]["schema"]
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(schema["properties"].keys())
    assert "default" not in schema["properties"]["tool_name"]


@pytest.mark.asyncio
async def test_react_forwards_tools_to_openai_responses_models():
    llm = MockWrappedResponsesLLM()
    pattern = ReActPattern(llm, max_iterations=1)
    pattern.set_step_context(step_id="tool_step", step_name="tool_test")

    result = await pattern.run_with_context(
        messages=[
            {"role": "system", "content": "You are a helpful assistant"},
            {"role": "user", "content": "Use the calculator if needed"},
        ],
        tools=[MockCalculatorTool()],
        max_iterations=1,
    )

    assert result["success"] is True
    assert llm.captured_kwargs is not None
    assert "tools" in llm.captured_kwargs
    assert llm.captured_kwargs["tool_choice"] == "auto"
    assert llm.captured_kwargs["tools"][0]["type"] == "function"
    assert llm.captured_kwargs["tools"][0]["function"]["name"] == "calculator"


if __name__ == "__main__":
    pytest.main([__file__])
