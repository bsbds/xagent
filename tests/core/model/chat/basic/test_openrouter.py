"""Test cases for OpenRouter LLM provider behavior."""

import json
from types import SimpleNamespace

import pytest

from xagent.core.model.chat.basic.openrouter import OpenRouterLLM
from xagent.core.model.chat.types import ChunkType


def _browser_extract_text_tool() -> dict:
    return {
        "type": "function",
        "function": {
            "name": "browser_extract_text",
            "description": "Extract text from a browser session",
            "parameters": {
                "type": "object",
                "properties": {"session_id": {"type": "string"}},
                "required": ["session_id"],
            },
        },
    }


def _browser_extract_text_dsml() -> str:
    return """
<｜｜DSML｜｜tool_calls>
<｜｜DSML｜｜invoke name="browser_extract_text">
<｜｜DSML｜｜parameter name="session_id" string="true">821:react_468b98b2</｜｜DSML｜｜parameter>
</｜｜DSML｜｜invoke>
</｜｜DSML｜｜tool_calls>
""".strip()


@pytest.mark.asyncio
async def test_openrouter_official_provider_pinning_disabled_by_default(
    mock_chat_completion, mocker, monkeypatch
):
    """OpenRouter provider pinning is opt-in to preserve fallback behavior."""

    monkeypatch.delenv("XAGENT_OPENROUTER_OFFICIAL_PROVIDERS_ONLY", raising=False)
    mock_client = mocker.AsyncMock()
    mock_client.chat.completions.create.return_value = mock_chat_completion
    mocker.patch(
        "xagent.core.model.chat.basic.openai.AsyncOpenAI",
        return_value=mock_client,
    )

    llm = OpenRouterLLM(
        model_name="deepseek/deepseek-v4-flash",
        api_key="test-key",
    )

    await llm.chat([{"role": "user", "content": "Hello"}])

    call_kwargs = mock_client.chat.completions.create.call_args.kwargs
    assert "extra_body" not in call_kwargs


@pytest.mark.asyncio
async def test_openrouter_deepseek_uses_official_provider(
    mock_chat_completion, mocker, monkeypatch
):
    """OpenRouter DeepSeek slugs should avoid third-party host fallbacks."""

    monkeypatch.setenv("XAGENT_OPENROUTER_OFFICIAL_PROVIDERS_ONLY", "true")
    mock_client = mocker.AsyncMock()
    mock_client.chat.completions.create.return_value = mock_chat_completion
    mocker.patch(
        "xagent.core.model.chat.basic.openai.AsyncOpenAI",
        return_value=mock_client,
    )

    llm = OpenRouterLLM(
        model_name="deepseek/deepseek-v4-flash",
        api_key="test-key",
    )

    await llm.chat([{"role": "user", "content": "Hello"}])

    call_kwargs = mock_client.chat.completions.create.call_args.kwargs
    assert call_kwargs["extra_body"]["provider"] == {
        "only": ["deepseek"],
        "allow_fallbacks": False,
        "require_parameters": True,
    }


def test_openrouter_official_provider_mapping_covers_auto_router_authors(
    monkeypatch,
):
    """Auto-selected official slugs should pin to official OpenRouter providers."""

    monkeypatch.setenv("XAGENT_OPENROUTER_OFFICIAL_PROVIDERS_ONLY", "true")
    cases = {
        "anthropic/claude-sonnet-4.6": ["anthropic"],
        "deepseek/deepseek-v4-flash": ["deepseek"],
        "google/gemini-3-flash-preview": ["google-ai-studio", "google-vertex"],
        "minimax/minimax-m3": ["minimax"],
        "openai/gpt-5.5": ["openai"],
        "z-ai/glm-5.2": ["z-ai"],
    }

    for model_name, expected_providers in cases.items():
        llm = OpenRouterLLM(
            model_name=model_name,
            api_key="test-key",
        )

        extra_body = llm._prepare_extra_body({})

        assert extra_body["provider"]["only"] == expected_providers
        assert extra_body["provider"]["allow_fallbacks"] is False
        assert extra_body["provider"]["require_parameters"] is True


@pytest.mark.asyncio
async def test_openrouter_provider_override_is_preserved(
    mock_chat_completion, mocker, monkeypatch
):
    """Explicit provider routing should win over automatic official pinning."""

    monkeypatch.setenv("XAGENT_OPENROUTER_OFFICIAL_PROVIDERS_ONLY", "true")
    mock_client = mocker.AsyncMock()
    mock_client.chat.completions.create.return_value = mock_chat_completion
    mocker.patch(
        "xagent.core.model.chat.basic.openai.AsyncOpenAI",
        return_value=mock_client,
    )

    llm = OpenRouterLLM(
        model_name="deepseek/deepseek-v4-flash",
        api_key="test-key",
    )

    await llm.chat(
        [{"role": "user", "content": "Hello"}],
        extra_body={"provider": {"only": ["deepinfra"]}, "trace_id": "manual"},
    )

    call_kwargs = mock_client.chat.completions.create.call_args.kwargs
    assert call_kwargs["extra_body"] == {
        "provider": {"only": ["deepinfra"]},
        "trace_id": "manual",
    }


@pytest.mark.asyncio
async def test_openrouter_stream_deepseek_uses_official_provider(mocker, monkeypatch):
    """Streaming calls should carry the same OpenRouter provider routing."""

    monkeypatch.setenv("XAGENT_OPENROUTER_OFFICIAL_PROVIDERS_ONLY", "true")

    async def empty_stream():
        if False:
            yield None

    mock_client = mocker.AsyncMock()
    mock_client.chat.completions.create.return_value = empty_stream()
    mocker.patch(
        "xagent.core.model.chat.basic.openai.AsyncOpenAI",
        return_value=mock_client,
    )

    llm = OpenRouterLLM(
        model_name="deepseek/deepseek-v4-flash",
        api_key="test-key",
    )

    _ = [
        chunk async for chunk in llm.stream_chat([{"role": "user", "content": "Hello"}])
    ]

    call_kwargs = mock_client.chat.completions.create.call_args.kwargs
    assert call_kwargs["extra_body"]["provider"] == {
        "only": ["deepseek"],
        "allow_fallbacks": False,
        "require_parameters": True,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "thinking",
    [
        {"type": "disabled", "enable": False},
        {"type": "omit"},
    ],
)
@pytest.mark.parametrize(
    "model_name",
    [
        "deepseek/deepseek-v4-flash",
        "openrouter/deepseek/deepseek-v4-flash",
    ],
)
async def test_openrouter_deepseek_stream_uses_disabled_thinking_payload(
    mocker, monkeypatch, thinking, model_name
):
    monkeypatch.setenv("XAGENT_OPENROUTER_OFFICIAL_PROVIDERS_ONLY", "false")

    async def empty_stream():
        if False:
            yield None

    mock_client = mocker.AsyncMock()
    mock_client.chat.completions.create.return_value = empty_stream()
    mocker.patch(
        "xagent.core.model.chat.basic.openai.AsyncOpenAI",
        return_value=mock_client,
    )

    llm = OpenRouterLLM(
        model_name=model_name,
        api_key="test-key",
        abilities=["chat", "tool_calling", "thinking_mode"],
    )

    _ = [
        chunk
        async for chunk in llm.stream_chat(
            [{"role": "user", "content": "Hello"}],
            tool_choice="required",
            thinking=thinking,
        )
    ]

    call_kwargs = mock_client.chat.completions.create.call_args.kwargs
    assert call_kwargs["extra_body"] == {
        "reasoning": {"enabled": False},
        "thinking": {"type": "disabled"},
    }
    assert "enable_thinking" not in call_kwargs["extra_body"]
    assert call_kwargs["tool_choice"] == "required"


def test_openrouter_reasoning_hook_enables_reasoning_payload(monkeypatch):
    monkeypatch.setenv("XAGENT_OPENROUTER_OFFICIAL_PROVIDERS_ONLY", "false")
    llm = OpenRouterLLM(
        model_name="deepseek/deepseek-v4-flash",
        api_key="test-key",
        abilities=["chat", "tool_calling", "thinking_mode"],
    )

    extra_body = llm._prepare_provider_reasoning_extra_body(
        extra_body={"trace_id": "abc", "enable_thinking": False},
        thinking={"type": "enabled"},
        tools=None,
        response_format=None,
        output_config=None,
        is_streaming=True,
    )

    assert extra_body == {
        "trace_id": "abc",
        "reasoning": {"enabled": True},
        "thinking": {"type": "enabled"},
    }


@pytest.mark.asyncio
async def test_structured_output_retry_disables_openrouter_reasoning(
    mocker, monkeypatch
):
    monkeypatch.setenv("XAGENT_OPENROUTER_OFFICIAL_PROVIDERS_ONLY", "false")

    first_message = SimpleNamespace(
        content="not json",
        tool_calls=None,
        reasoning_content="reasoning here",
    )
    second_message = SimpleNamespace(
        content='{"status": "ok"}',
        tool_calls=None,
        reasoning_content=None,
    )
    first_response = SimpleNamespace(
        choices=[SimpleNamespace(message=first_message)],
        usage=None,
        model_dump=lambda: {"id": "openrouter-first"},
    )
    second_response = SimpleNamespace(
        choices=[SimpleNamespace(message=second_message)],
        usage=None,
        model_dump=lambda: {"id": "openrouter-second"},
    )

    mock_client = mocker.AsyncMock()
    mock_client.chat.completions.create.side_effect = [first_response, second_response]
    mocker.patch(
        "xagent.core.model.chat.basic.openai.AsyncOpenAI",
        return_value=mock_client,
    )

    llm = OpenRouterLLM(
        model_name="deepseek/deepseek-v4-flash",
        api_key="test-key",
        abilities=["chat", "tool_calling", "thinking_mode"],
    )

    result = await llm.chat(
        [{"role": "user", "content": "Return JSON"}],
        response_format={"type": "json_object"},
        thinking={"type": "enabled"},
    )

    assert result["type"] == "text"
    assert result["content"] == '{"status": "ok"}'
    second_call = mock_client.chat.completions.create.call_args_list[1].kwargs
    assert second_call["extra_body"]["reasoning"] == {"enabled": False}
    assert second_call["extra_body"]["thinking"] == {"type": "disabled"}


@pytest.mark.asyncio
async def test_openrouter_deepseek_parses_raw_dsml_content(mocker, monkeypatch):
    monkeypatch.setenv("XAGENT_OPENROUTER_OFFICIAL_PROVIDERS_ONLY", "false")
    message = SimpleNamespace(
        content=_browser_extract_text_dsml(),
        tool_calls=None,
        reasoning_content=None,
    )
    response = SimpleNamespace(
        choices=[SimpleNamespace(message=message)],
        usage=None,
        model_dump=lambda: {"id": "openrouter-dsml"},
    )

    mock_client = mocker.AsyncMock()
    mock_client.chat.completions.create.return_value = response
    mocker.patch(
        "xagent.core.model.chat.basic.openai.AsyncOpenAI",
        return_value=mock_client,
    )

    llm = OpenRouterLLM(
        model_name="deepseek/deepseek-v4-flash",
        api_key="test-key",
    )

    result = await llm.chat(
        [{"role": "user", "content": "Read the page"}],
        tools=[_browser_extract_text_tool()],
    )

    assert result["type"] == "tool_call"
    assert result["content"] is None
    assert result["tool_calls"][0]["function"]["name"] == "browser_extract_text"
    assert json.loads(result["tool_calls"][0]["function"]["arguments"]) == {
        "session_id": "821:react_468b98b2"
    }


@pytest.mark.asyncio
async def test_openrouter_non_deepseek_does_not_parse_raw_dsml_content(
    mocker, monkeypatch
):
    monkeypatch.setenv("XAGENT_OPENROUTER_OFFICIAL_PROVIDERS_ONLY", "false")
    message = SimpleNamespace(
        content=_browser_extract_text_dsml(),
        tool_calls=None,
        reasoning_content=None,
    )
    response = SimpleNamespace(
        choices=[SimpleNamespace(message=message)],
        usage=None,
        model_dump=lambda: {"id": "openrouter-text"},
    )

    mock_client = mocker.AsyncMock()
    mock_client.chat.completions.create.return_value = response
    mocker.patch(
        "xagent.core.model.chat.basic.openai.AsyncOpenAI",
        return_value=mock_client,
    )

    llm = OpenRouterLLM(
        model_name="openai/gpt-5.5",
        api_key="test-key",
    )

    result = await llm.chat(
        [{"role": "user", "content": "Read the page"}],
        tools=[_browser_extract_text_tool()],
    )

    assert result["type"] == "text"
    assert "DSML" in result["content"]


@pytest.mark.asyncio
async def test_openrouter_deepseek_stream_with_tools_uses_non_streaming_dsml_parser(
    mocker, monkeypatch
):
    monkeypatch.setenv("XAGENT_OPENROUTER_OFFICIAL_PROVIDERS_ONLY", "false")
    message = SimpleNamespace(
        content=_browser_extract_text_dsml(),
        tool_calls=None,
        reasoning_content=None,
    )
    response = SimpleNamespace(
        choices=[SimpleNamespace(message=message)],
        usage=None,
        model_dump=lambda: {"id": "openrouter-dsml"},
    )

    mock_client = mocker.AsyncMock()
    mock_client.chat.completions.create.return_value = response
    mocker.patch(
        "xagent.core.model.chat.basic.openai.AsyncOpenAI",
        return_value=mock_client,
    )

    llm = OpenRouterLLM(
        model_name="deepseek/deepseek-v4-flash",
        api_key="test-key",
    )

    chunks = [
        chunk
        async for chunk in llm.stream_chat(
            [{"role": "user", "content": "Read the page"}],
            tools=[_browser_extract_text_tool()],
        )
    ]

    assert len(chunks) == 1
    assert chunks[0].type == ChunkType.TOOL_CALL
    assert chunks[0].tool_calls[0]["function"]["name"] == "browser_extract_text"
    call_kwargs = mock_client.chat.completions.create.call_args.kwargs
    assert "stream" not in call_kwargs
