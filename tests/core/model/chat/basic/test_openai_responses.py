"""Test cases for OpenAI Responses API LLM implementation using OpenAI SDK."""

import types
from unittest.mock import MagicMock

import pytest

from xagent.core.model.chat.basic.adapter import create_base_llm
from xagent.core.model.chat.basic.openai_responses import OpenAIResponsesLLM
from xagent.core.model.chat.basic.schema_utils import normalize_structured_output_schema
from xagent.core.model.chat.token_context import get_and_reset_token_usage
from xagent.core.model.model import ChatModelConfig


class _AsyncStream:
    def __init__(self, events, final_response):
        self._events = list(events)
        self._final = final_response

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def __aiter__(self):
        async def gen():
            for ev in self._events:
                yield ev

        return gen()

    async def get_final_response(self):
        return self._final


class TestOpenAIResponsesLLM:
    @pytest.fixture
    def llm(self):
        return OpenAIResponsesLLM(
            model_name="gpt-4o-mini",
            base_url="https://api.openai.com/v1",
            api_key="test-api-key",
            default_temperature=0.7,
            default_max_tokens=1024,
            timeout=30.0,
        )

    def test_create_base_llm_supports_openai_responses_provider(self):
        llm = create_base_llm(
            ChatModelConfig(
                id="test-openai-responses",
                model_name="gpt-4.1",
                model_provider="openai-responses",
                api_key="test-api-key",
                base_url="https://api.openai.com/v1",
                abilities=["chat", "tool_calling"],
            )
        )

        wrapped = getattr(llm, "_inner", llm)
        assert isinstance(wrapped, OpenAIResponsesLLM)

    @pytest.mark.asyncio
    async def test_basic_responses_create(self, llm, mocker):
        get_and_reset_token_usage()
        mock_client = mocker.AsyncMock()

        resp = MagicMock()
        resp.output_text = "Hello World"
        resp.model_dump.return_value = {
            "output_text": "Hello World",
            "usage": {"input_tokens": 11, "output_tokens": 7, "total_tokens": 18},
            "output": [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "Hello World"}],
                }
            ],
        }
        mocker.patch(
            "xagent.core.model.chat.basic.openai_responses.AsyncOpenAI",
            return_value=mock_client,
        )

        mock_client.responses.stream = MagicMock(return_value=_AsyncStream([], resp))

        messages = [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "Say Hello World."},
        ]

        out = await llm.chat(messages)
        assert out["type"] == "text"
        assert out["content"] == "Hello World"

        mock_client.responses.stream.assert_called_once()
        call = mock_client.responses.stream.call_args.kwargs
        assert call["model"] == "gpt-4o-mini"
        assert call["instructions"] == "You are a helpful assistant."
        assert call["temperature"] == 0.7
        assert call["max_output_tokens"] == 1024
        assert isinstance(call["input"], list)
        assert call["input"][0]["type"] == "message"
        assert call["input"][0]["role"] == "developer"
        assert call["input"][0]["content"] == "You are a helpful assistant."
        assert call["input"][1]["type"] == "message"
        assert call["input"][1]["role"] == "user"
        assert call["input"][1]["content"] == "Say Hello World."
        usage = get_and_reset_token_usage()
        assert usage.input_tokens == 11
        assert usage.output_tokens == 7
        assert usage.llm_calls == 1

    @pytest.mark.asyncio
    async def test_codex_backend_maps_system_messages_to_developer(self, mocker):
        llm = OpenAIResponsesLLM(
            model_name="gpt-5-codex",
            base_url="https://chatgpt.com/backend-api/codex",
            api_key="test-api-key",
        )
        mock_client = mocker.AsyncMock()

        resp = MagicMock()
        resp.output_text = "Hello World"
        resp.model_dump.return_value = {"output_text": "Hello World", "output": []}
        final_resp = MagicMock()
        final_resp.output_text = "Hello World"
        final_resp.model_dump.return_value = {
            "output_text": "Hello World",
            "output": [],
        }
        mock_client.responses.stream = MagicMock(
            return_value=_AsyncStream([], final_resp)
        )
        mocker.patch(
            "xagent.core.model.chat.basic.openai_responses.AsyncOpenAI",
            return_value=mock_client,
        )

        await llm.chat(
            [
                {"role": "system", "content": "You are a helpful assistant."},
                {"role": "user", "content": "Say Hello World."},
            ]
        )

        call = mock_client.responses.stream.call_args.kwargs
        assert call["instructions"] == "You are a helpful assistant."
        assert call["input"][0]["role"] == "developer"

    @pytest.mark.asyncio
    async def test_codex_backend_chat_uses_streaming_api(self, mocker):
        llm = OpenAIResponsesLLM(
            model_name="gpt-5-codex",
            base_url="https://chatgpt.com/backend-api/codex",
            api_key="test-api-key",
        )
        mock_client = mocker.AsyncMock()

        final_resp = MagicMock()
        final_resp.output_text = "Hello from stream"
        final_resp.model_dump.return_value = {
            "output_text": "Hello from stream",
            "output": [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "Hello from stream"}],
                }
            ],
        }

        mock_client.responses.stream = MagicMock(
            return_value=_AsyncStream([], final_resp)
        )
        mocker.patch(
            "xagent.core.model.chat.basic.openai_responses.AsyncOpenAI",
            return_value=mock_client,
        )

        out = await llm.chat([{"role": "user", "content": "hi"}])

        assert out["type"] == "text"
        assert out["content"] == "Hello from stream"
        mock_client.responses.stream.assert_called_once()
        mock_client.responses.stream.assert_called_once()

    @pytest.mark.asyncio
    async def test_explicit_instructions_are_sent_top_level(self, llm, mocker):
        mock_client = mocker.AsyncMock()

        resp = MagicMock()
        resp.output_text = "Hello"
        resp.model_dump.return_value = {"output_text": "Hello", "output": []}
        mock_client.responses.stream = MagicMock(return_value=_AsyncStream([], resp))
        mocker.patch(
            "xagent.core.model.chat.basic.openai_responses.AsyncOpenAI",
            return_value=mock_client,
        )

        await llm.chat(
            [{"role": "user", "content": "Say hello."}],
            instructions="Return concise output.",
        )

        call = mock_client.responses.stream.call_args.kwargs
        assert call["instructions"] == "Return concise output."

    @pytest.mark.asyncio
    async def test_tool_call_extraction(self, llm, mocker):
        mock_client = mocker.AsyncMock()
        resp = MagicMock()
        resp.output_text = ""
        resp.model_dump.return_value = {
            "output": [
                {
                    "type": "function_call",
                    "name": "get_weather",
                    "arguments": '{"location":"Boston"}',
                    "call_id": "call_1",
                }
            ]
        }
        mock_client.responses.stream = MagicMock(return_value=_AsyncStream([], resp))
        mocker.patch(
            "xagent.core.model.chat.basic.openai_responses.AsyncOpenAI",
            return_value=mock_client,
        )

        tools = [
            {
                "type": "function",
                "function": {
                    "name": "get_weather",
                    "description": "Get weather",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ]
        out = await llm.chat([{"role": "user", "content": "weather"}], tools=tools)
        assert out["type"] == "tool_call"
        assert out["tool_calls"][0]["function"]["name"] == "get_weather"
        assert out["tool_calls"][0]["id"] == "call_1"

        call = mock_client.responses.stream.call_args.kwargs
        assert "tools" in call
        assert call["tools"][0]["type"] == "function"
        assert call["tools"][0]["name"] == "get_weather"
        assert call["tools"][0]["strict"] is False
        assert call["tools"][0]["parameters"]["additionalProperties"] is False

    @pytest.mark.asyncio
    async def test_json_object_format_does_not_inject_json_word_into_input(
        self, llm, mocker
    ):
        mock_client = mocker.AsyncMock()

        resp = MagicMock()
        resp.output_text = "{}"
        resp.model_dump.return_value = {"output_text": "{}", "output": []}
        mock_client.responses.stream = MagicMock(return_value=_AsyncStream([], resp))
        mocker.patch(
            "xagent.core.model.chat.basic.openai_responses.AsyncOpenAI",
            return_value=mock_client,
        )

        await llm.chat(
            [
                {"role": "system", "content": "Return the result."},
                {"role": "user", "content": "Say hello."},
            ],
            response_format={"type": "json_object"},
        )

        call = mock_client.responses.stream.call_args.kwargs
        assert call["text"]["format"]["type"] == "json_object"
        combined = str(call["input"]).lower()
        assert "respond with json" not in combined

    @pytest.mark.asyncio
    async def test_json_object_format_preserves_existing_json_word_without_injection(
        self, llm, mocker
    ):
        mock_client = mocker.AsyncMock()

        resp = MagicMock()
        resp.output_text = "{}"
        resp.model_dump.return_value = {"output_text": "{}", "output": []}
        mock_client.responses.stream = MagicMock(return_value=_AsyncStream([], resp))
        mocker.patch(
            "xagent.core.model.chat.basic.openai_responses.AsyncOpenAI",
            return_value=mock_client,
        )

        await llm.chat(
            [
                {"role": "system", "content": "Return JSON."},
                {"role": "user", "content": "Say hello."},
            ],
            response_format={"type": "json_object"},
        )

        call = mock_client.responses.stream.call_args.kwargs
        combined = str(call["input"]).lower()
        assert "respond with json" not in combined
        assert "json" in combined

    @pytest.mark.asyncio
    async def test_streaming_parses_text_and_tool_calls(self, llm, mocker):
        get_and_reset_token_usage()
        mock_client = mocker.AsyncMock()

        ev_text = types.SimpleNamespace(type="response.output_text.delta", delta="Hi")
        ev_args_done = types.SimpleNamespace(
            type="response.function_call_arguments.done",
            item_id="item_1",
            name="do_thing",
            arguments='{"x":1}',
        )

        final_resp = MagicMock()
        final_resp.model_dump.return_value = {
            "usage": {"input_tokens": 1, "output_tokens": 2, "total_tokens": 3}
        }

        # `.responses.stream(...)` returns an async context manager, not an awaitable.
        mock_client.responses.stream = MagicMock(
            return_value=_AsyncStream([ev_text, ev_args_done], final_resp)
        )
        mocker.patch(
            "xagent.core.model.chat.basic.openai_responses.AsyncOpenAI",
            return_value=mock_client,
        )

        chunks = []
        async for chunk in llm.stream_chat([{"role": "user", "content": "hi"}]):
            chunks.append(chunk)

        assert any(c.is_token() and c.delta == "Hi" for c in chunks)
        tool_chunks = [c for c in chunks if c.is_tool_call()]
        assert tool_chunks
        assert tool_chunks[-1].tool_calls[-1]["function"]["name"] == "do_thing"
        usage_chunks = [c for c in chunks if c.is_usage()]
        assert usage_chunks
        assert usage_chunks[-1].usage["total_tokens"] == 3
        usage = get_and_reset_token_usage()
        assert usage.input_tokens == 1
        assert usage.output_tokens == 2
        assert usage.llm_calls == 1


def test_normalize_structured_output_schema_enforces_strict_object_rules():
    schema = {
        "type": "object",
        "properties": {
            "answer": {
                "type": "string",
                "default": "hello",
            },
            "nested": {
                "type": "object",
                "properties": {
                    "flag": {"type": "boolean", "default": False},
                },
            },
        },
    }

    normalized = normalize_structured_output_schema(schema)

    assert normalized["additionalProperties"] is False
    assert set(normalized["required"]) == {"answer", "nested"}
    assert "default" not in normalized["properties"]["answer"]
    assert normalized["properties"]["nested"]["additionalProperties"] is False
    assert normalized["properties"]["nested"]["required"] == ["flag"]
    assert "default" not in normalized["properties"]["nested"]["properties"]["flag"]

    @pytest.mark.asyncio
    async def test_streaming_emits_text_done_when_no_delta(self, llm, mocker):
        mock_client = mocker.AsyncMock()

        ev_done = types.SimpleNamespace(
            type="response.output_text.done",
            item_id="item_1",
            text='{"ok":true}',
        )
        final_resp = MagicMock()
        final_resp.model_dump.return_value = {
            "usage": {"input_tokens": 1, "output_tokens": 2, "total_tokens": 3}
        }

        mock_client.responses.stream = MagicMock(
            return_value=_AsyncStream([ev_done], final_resp)
        )
        mocker.patch(
            "xagent.core.model.chat.basic.openai_responses.AsyncOpenAI",
            return_value=mock_client,
        )

        chunks = []
        async for chunk in llm.stream_chat([{"role": "user", "content": "hi"}]):
            chunks.append(chunk)

        assert any(c.is_token() and c.delta == '{"ok":true}' for c in chunks)
        usage_chunks = [c for c in chunks if c.is_usage()]
        assert usage_chunks
        assert usage_chunks[-1].usage["total_tokens"] == 3

    @pytest.mark.asyncio
    async def test_streaming_emits_final_response_text_when_stream_has_no_text_events(
        self, llm, mocker
    ):
        mock_client = mocker.AsyncMock()

        final_resp = MagicMock()
        final_resp.output_text = (
            '{"type":"final_answer","reasoning":"done","answer":"ok"}'
        )
        final_resp.model_dump.return_value = {
            "output_text": '{"type":"final_answer","reasoning":"done","answer":"ok"}',
            "usage": {"input_tokens": 1, "output_tokens": 2, "total_tokens": 3},
            "output": [],
        }

        mock_client.responses.stream = MagicMock(
            return_value=_AsyncStream([], final_resp)
        )
        mocker.patch(
            "xagent.core.model.chat.basic.openai_responses.AsyncOpenAI",
            return_value=mock_client,
        )

        chunks = []
        async for chunk in llm.stream_chat([{"role": "user", "content": "hi"}]):
            chunks.append(chunk)

        assert any(
            c.is_token()
            and c.delta == '{"type":"final_answer","reasoning":"done","answer":"ok"}'
            for c in chunks
        )
        assert any(c.is_usage() for c in chunks)

    @pytest.mark.asyncio
    async def test_streaming_emits_text_from_output_item_done_message(
        self, llm, mocker
    ):
        mock_client = mocker.AsyncMock()

        ev_done = types.SimpleNamespace(
            type="response.output_item.done",
            item=types.SimpleNamespace(
                type="message",
                id="msg_1",
                content=[types.SimpleNamespace(type="output_text", text='{"ok":true}')],
            ),
        )
        final_resp = MagicMock()
        final_resp.model_dump.return_value = {
            "usage": {"input_tokens": 1, "output_tokens": 2, "total_tokens": 3}
        }

        mock_client.responses.stream = MagicMock(
            return_value=_AsyncStream([ev_done], final_resp)
        )
        mocker.patch(
            "xagent.core.model.chat.basic.openai_responses.AsyncOpenAI",
            return_value=mock_client,
        )

        chunks = []
        async for chunk in llm.stream_chat([{"role": "user", "content": "hi"}]):
            chunks.append(chunk)

        assert any(c.is_token() and c.delta == '{"ok":true}' for c in chunks)
        assert any(c.is_usage() for c in chunks)
