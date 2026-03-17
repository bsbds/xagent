from __future__ import annotations

import copy
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Awaitable, Callable, List, Optional, cast

import openai
from openai import AsyncOpenAI

from ..exceptions import LLMRetryableError
from ..timeout_config import TimeoutConfig
from ..token_context import add_token_usage
from ..types import ChunkType, StreamChunk
from .base import BaseLLM

HeadersProvider = Callable[[dict[str, Any]], Awaitable[dict[str, str]]]
_CODEX_BASE_URL_FRAGMENT = "chatgpt.com/backend-api/codex"
_DEFAULT_INSTRUCTIONS = "You are a helpful assistant."


@dataclass
class _ResponsesStreamState:
    first_content_event: bool = True
    text_by_item: dict[str, str] = field(default_factory=dict)
    args_by_item: dict[str, str] = field(default_factory=dict)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    start_time: float = 0.0
    last_token_time: float | None = None


class OpenAIResponsesLLM(BaseLLM):
    """
    OpenAI Responses API client using the official OpenAI Python SDK.
    """

    def __init__(
        self,
        model_name: str,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        default_temperature: Optional[float] = None,
        default_max_tokens: Optional[int] = None,
        default_store: Optional[bool] = None,
        timeout: float = 180.0,
        abilities: Optional[List[str]] = None,
        timeout_config: Optional[TimeoutConfig] = None,
        default_headers: Optional[dict[str, str]] = None,
        extra_headers_provider: Optional[HeadersProvider] = None,
    ):
        self._model_name = model_name
        self._api_key = api_key
        self._base_url = base_url.rstrip("/") if isinstance(base_url, str) else None
        self._default_temperature = default_temperature
        self._default_max_tokens = default_max_tokens
        self._default_store = default_store
        self._timeout = timeout
        self._timeout_config = timeout_config or TimeoutConfig()
        self._abilities = abilities or ["chat", "tool_calling"]
        self._default_headers = default_headers or {}
        self._extra_headers_provider = extra_headers_provider
        self._client: AsyncOpenAI | None = None

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def abilities(self) -> List[str]:
        return self._abilities

    @property
    def supports_thinking_mode(self) -> bool:
        return False

    def _ensure_client(self) -> None:
        if self._client is None:
            self._client = AsyncOpenAI(
                api_key=self._api_key,
                base_url=self._base_url,
                timeout=self._timeout,
                default_headers=self._default_headers or None,
            )

    async def _build_extra_headers(
        self, request_context: dict[str, Any]
    ) -> dict[str, str]:
        extra: dict[str, str] = {}
        if self._extra_headers_provider is not None:
            provided = await self._extra_headers_provider(request_context)
            if isinstance(provided, dict):
                extra.update(
                    {str(k): str(v) for k, v in provided.items() if v is not None}
                )
        user_extra = request_context.get("extra_headers")
        if isinstance(user_extra, dict):
            extra.update(
                {str(k): str(v) for k, v in user_extra.items() if v is not None}
            )
        session_id = request_context.get("session_id")
        if isinstance(session_id, str) and session_id:
            extra.setdefault("session_id", session_id)
        return extra

    def _resolve_instructions_and_input(
        self,
        *,
        messages: list[dict[str, Any]],
        explicit_instructions: Any,
    ) -> tuple[str, list[dict[str, Any]]]:
        instructions = (
            explicit_instructions.strip()
            if isinstance(explicit_instructions, str) and explicit_instructions.strip()
            else _DEFAULT_INSTRUCTIONS
        )
        responses_messages: list[dict[str, Any]] = []
        for message in messages:
            if not isinstance(message, dict):
                continue
            role = message.get("role")
            content = message.get("content")
            if not isinstance(role, str) or content is None:
                continue
            mapped_role = "developer" if role == "system" else role
            responses_messages.append(
                {
                    "type": "message",
                    "role": mapped_role,
                    "content": content,
                }
            )
        return instructions, responses_messages

    def _force_additional_properties_false(self, schema: Any) -> Any:
        def force(node: Any) -> Any:
            if isinstance(node, list):
                return [force(item) for item in node]
            if not isinstance(node, dict):
                return node

            for key in ("anyOf", "oneOf", "allOf"):
                if isinstance(node.get(key), list):
                    node[key] = [force(item) for item in node[key]]
            for key in ("not", "if", "then", "else", "items"):
                if key in node:
                    node[key] = force(node[key])
            if isinstance(node.get("properties"), dict):
                node["properties"] = {
                    key: force(value) for key, value in node["properties"].items()
                }
            if isinstance(node.get("patternProperties"), dict):
                node["patternProperties"] = {
                    key: force(value)
                    for key, value in node["patternProperties"].items()
                }
            if isinstance(node.get("additionalProperties"), dict):
                node["additionalProperties"] = force(node["additionalProperties"])
            if isinstance(node.get("prefixItems"), list):
                node["prefixItems"] = [force(item) for item in node["prefixItems"]]
            if node.get("type") == "object" or "properties" in node:
                node["additionalProperties"] = False
            return node

        if not isinstance(schema, (dict, list)):
            return schema
        return force(copy.deepcopy(schema))

    def _normalize_tools(
        self, tools: list[dict[str, Any]] | None
    ) -> list[dict[str, Any]] | None:
        if not tools:
            return None

        normalized: list[dict[str, Any]] = []
        for tool in tools:
            if not isinstance(tool, dict):
                continue
            if tool.get("type") == "function" and isinstance(
                tool.get("function"), dict
            ):
                fn = cast(dict[str, Any], tool["function"])
                name = fn.get("name")
                if not isinstance(name, str) or not name:
                    continue
                normalized.append(
                    {
                        "type": "function",
                        "name": name,
                        "description": fn.get("description"),
                        "parameters": self._force_additional_properties_false(
                            fn.get("parameters")
                        ),
                        "strict": bool(fn.get("strict", tool.get("strict", False))),
                    }
                )
                continue
            if tool.get("type") == "function" and isinstance(tool.get("name"), str):
                tool_copy = dict(tool)
                tool_copy.setdefault("strict", False)
                if "parameters" in tool_copy:
                    tool_copy["parameters"] = self._force_additional_properties_false(
                        tool_copy.get("parameters")
                    )
                normalized.append(tool_copy)
                continue
            normalized.append(tool)
        return normalized or None

    def _normalize_tool_choice(
        self, tool_choice: str | dict[str, Any] | None
    ) -> str | dict[str, Any] | None:
        if tool_choice is None:
            return None
        if isinstance(tool_choice, str):
            if tool_choice in ("auto", "none", "required"):
                return tool_choice
            if tool_choice == "any":
                return "required"
            return {"type": "function", "name": tool_choice}
        if isinstance(tool_choice, dict):
            if tool_choice.get("type") == "function" and isinstance(
                tool_choice.get("function"), dict
            ):
                fn = cast(dict[str, Any], tool_choice["function"])
                name = fn.get("name")
                if isinstance(name, str) and name:
                    return {"type": "function", "name": name}
            return tool_choice
        return None

    def _resolve_text_param(
        self,
        response_format: dict[str, Any] | None,
        output_config: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        fmt = None
        if isinstance(output_config, dict):
            fmt = output_config.get("format")
        if isinstance(response_format, dict):
            fmt = response_format
        if isinstance(fmt, dict) and isinstance(fmt.get("type"), str):
            return {"format": fmt}
        return None

    def _build_passthrough_kwargs(self, kwargs: dict[str, Any]) -> dict[str, Any]:
        passthrough = {
            key: value
            for key, value in kwargs.items()
            if key not in ("session_id", "extra_headers", "instructions")
        }
        if "store" not in passthrough and self._default_store is not None:
            passthrough["store"] = bool(self._default_store)
        return passthrough

    async def _prepare_request(
        self,
        *,
        messages: list[dict[str, str]],
        temperature: float | None,
        max_tokens: int | None,
        tools: list[dict[str, Any]] | None,
        tool_choice: str | dict[str, Any] | None,
        response_format: dict[str, Any] | None,
        output_config: dict[str, Any] | None,
        kwargs: dict[str, Any],
    ) -> dict[str, Any]:
        request_context = dict(kwargs)
        request_context["messages"] = messages
        extra_headers = await self._build_extra_headers(request_context)

        explicit_instructions = kwargs.get("instructions")
        instructions, input_items = self._resolve_instructions_and_input(
            messages=cast(
                list[dict[str, Any]],
                self._sanitize_unicode_content(messages),
            ),
            explicit_instructions=explicit_instructions,
        )

        request_data: dict[str, Any] = {
            "model": self._model_name,
            "input": input_items,
            **self._build_passthrough_kwargs(kwargs),
        }
        if instructions:
            request_data["instructions"] = instructions

        normalized_tools = self._normalize_tools(tools)
        if normalized_tools is not None:
            request_data["tools"] = normalized_tools

        normalized_tool_choice = self._normalize_tool_choice(tool_choice)
        if normalized_tool_choice is not None:
            request_data["tool_choice"] = normalized_tool_choice

        text_param = self._resolve_text_param(response_format, output_config)
        if text_param is not None:
            request_data["text"] = text_param

        resolved_temperature = (
            temperature if temperature is not None else self._default_temperature
        )
        if resolved_temperature is not None:
            request_data["temperature"] = resolved_temperature

        resolved_max_tokens = (
            max_tokens if max_tokens is not None else self._default_max_tokens
        )
        if resolved_max_tokens is not None:
            request_data["max_output_tokens"] = resolved_max_tokens

        if extra_headers:
            request_data["extra_headers"] = extra_headers

        return request_data

    def _response_to_raw(self, response: Any) -> dict[str, Any]:
        if isinstance(response, dict):
            return response
        if hasattr(response, "to_dict"):
            try:
                dumped = response.to_dict()
            except Exception:
                dumped = None
            if isinstance(dumped, dict):
                return cast(dict[str, Any], dumped)
        if hasattr(response, "model_dump"):
            try:
                dumped = response.model_dump()
            except Exception:
                dumped = None
            if isinstance(dumped, dict):
                return cast(dict[str, Any], dumped)
        try:
            data = dict(response.__dict__)
        except Exception:
            data = None
        if isinstance(data, dict):
            return cast(dict[str, Any], data)
        return {}

    def _response_output_items(self, response: Any) -> list[Any]:
        output = getattr(response, "output", None)
        if isinstance(output, list):
            return output
        raw = self._response_to_raw(response)
        raw_output = raw.get("output")
        if isinstance(raw_output, list):
            return raw_output
        return []

    def _extract_tool_calls(self, response: Any) -> list[dict[str, Any]]:
        tool_calls: list[dict[str, Any]] = []
        for item in self._response_output_items(response):
            item_type = getattr(item, "type", None)
            if item_type is None and isinstance(item, dict):
                item_type = item.get("type")
            if item_type != "function_call":
                continue

            name = getattr(item, "name", None)
            if name is None and isinstance(item, dict):
                name = item.get("name")
            if not isinstance(name, str) or not name:
                continue

            arguments = getattr(item, "arguments", None)
            if arguments is None and isinstance(item, dict):
                arguments = item.get("arguments")
            if not isinstance(arguments, str):
                arguments = "" if arguments is None else str(arguments)

            call_id = getattr(item, "call_id", None) or getattr(item, "id", None)
            if call_id is None and isinstance(item, dict):
                call_id = item.get("call_id") or item.get("id")

            tool_calls.append(
                {
                    "id": str(call_id or ""),
                    "type": "function",
                    "function": {"name": name, "arguments": arguments},
                }
            )
        return tool_calls

    def _extract_output_text(self, response: Any) -> str:
        output_text = getattr(response, "output_text", None)
        if isinstance(output_text, str):
            return output_text

        raw = self._response_to_raw(response)
        if isinstance(raw.get("output_text"), str):
            return cast(str, raw["output_text"])
        return ""

    def _extract_usage(self, response: Any) -> dict[str, int]:
        usage = getattr(response, "usage", None)
        if usage is None:
            raw = self._response_to_raw(response)
            usage = raw.get("usage")
        if usage is None:
            return {}

        if isinstance(usage, dict):
            input_tokens = int(usage.get("input_tokens") or 0)
            output_tokens = int(usage.get("output_tokens") or 0)
            total_tokens = int(usage.get("total_tokens") or 0)
        else:
            input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
            output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
            total_tokens = int(getattr(usage, "total_tokens", 0) or 0)

        return {
            "prompt_tokens": input_tokens,
            "completion_tokens": output_tokens,
            "total_tokens": total_tokens,
        }

    def _parse_chat_response(self, response: Any) -> dict[str, Any]:
        raw = self._response_to_raw(response)
        tool_calls = self._extract_tool_calls(response)
        if tool_calls:
            return {"type": "tool_call", "tool_calls": tool_calls, "raw": raw}
        return {
            "type": "text",
            "content": self._extract_output_text(response),
            "raw": raw,
        }

    def _record_token_usage(self, response: Any, *, call_type: str) -> None:
        usage = self._extract_usage(response)
        if not usage:
            return
        add_token_usage(
            input_tokens=usage.get("prompt_tokens", 0),
            output_tokens=usage.get("completion_tokens", 0),
            model=self._model_name,
            call_type=call_type,
        )

    def _parse_stream_event(
        self,
        *,
        event: Any,
        state: _ResponsesStreamState,
        now: float,
    ) -> list[StreamChunk]:
        ev_type = getattr(event, "type", None)
        is_content_event = ev_type in (
            "response.output_text.delta",
            "response.output_text.done",
            "response.refusal.delta",
            "response.refusal.done",
            "response.function_call_arguments.delta",
            "response.function_call_arguments.done",
        )
        if state.first_content_event and is_content_event:
            state.first_content_event = False
            elapsed = now - state.start_time
            if elapsed > self._timeout_config.first_token_timeout:
                raise RuntimeError(
                    f"First token timeout: {elapsed}s > {self._timeout_config.first_token_timeout}s"
                )
        if state.last_token_time is not None:
            interval = now - state.last_token_time
            if interval > self._timeout_config.token_interval_timeout:
                raise RuntimeError(
                    f"Token interval timeout: {interval}s > {self._timeout_config.token_interval_timeout}s"
                )

        if ev_type == "response.output_text.delta":
            return self._handle_output_text_delta(event, state, now)
        if ev_type == "response.output_text.done":
            return self._handle_output_text_done(event, state, now)
        if ev_type == "response.refusal.delta":
            return self._handle_refusal_delta(event, state, now)
        if ev_type == "response.refusal.done":
            return self._handle_refusal_done(event, state, now)
        if ev_type == "response.function_call_arguments.delta":
            item_id = cast(str, getattr(event, "item_id", ""))
            delta = cast(str, getattr(event, "delta", ""))
            if item_id and delta:
                state.args_by_item[item_id] = (
                    state.args_by_item.get(item_id, "") + delta
                )
            return []
        if ev_type == "response.function_call_arguments.done":
            return self._handle_function_call_done(event, state)
        if ev_type == "response.output_item.done":
            return self._handle_output_item_done(event, state, now)
        return []

    def _handle_output_text_delta(
        self, event: Any, state: _ResponsesStreamState, now: float
    ) -> list[StreamChunk]:
        delta = cast(str, getattr(event, "delta", ""))
        if not delta:
            return []
        item_id = cast(str, getattr(event, "item_id", ""))
        if item_id:
            state.text_by_item[item_id] = state.text_by_item.get(item_id, "") + delta
        state.last_token_time = now
        return [
            StreamChunk(
                type=ChunkType.TOKEN,
                content=delta,
                delta=delta,
                raw=event,
            )
        ]

    def _handle_output_text_done(
        self, event: Any, state: _ResponsesStreamState, now: float
    ) -> list[StreamChunk]:
        item_id = cast(str, getattr(event, "item_id", ""))
        text = cast(str, getattr(event, "text", ""))
        if not text:
            return []
        previous = state.text_by_item.get(item_id, "") if item_id else ""
        delta = (
            text[len(previous) :] if previous and text.startswith(previous) else text
        )
        if not delta:
            return []
        if item_id:
            state.text_by_item[item_id] = text
        state.last_token_time = now
        return [
            StreamChunk(
                type=ChunkType.TOKEN,
                content=delta,
                delta=delta,
                raw=event,
            )
        ]

    def _handle_refusal_delta(
        self, event: Any, state: _ResponsesStreamState, now: float
    ) -> list[StreamChunk]:
        delta = cast(str, getattr(event, "delta", ""))
        if not delta:
            return []
        state.last_token_time = now
        return [
            StreamChunk(
                type=ChunkType.TOKEN,
                content=delta,
                delta=delta,
                raw=event,
            )
        ]

    def _handle_refusal_done(
        self, event: Any, state: _ResponsesStreamState, now: float
    ) -> list[StreamChunk]:
        refusal = cast(str, getattr(event, "refusal", ""))
        if not refusal:
            return []
        state.last_token_time = now
        return [
            StreamChunk(
                type=ChunkType.TOKEN,
                content=refusal,
                delta=refusal,
                raw=event,
            )
        ]

    def _handle_function_call_done(
        self, event: Any, state: _ResponsesStreamState
    ) -> list[StreamChunk]:
        item_id = cast(str, getattr(event, "item_id", ""))
        name = cast(str, getattr(event, "name", ""))
        arguments = cast(str, getattr(event, "arguments", ""))
        if item_id and item_id in state.args_by_item:
            arguments = state.args_by_item[item_id]
        if not name:
            return []
        state.tool_calls.append(
            {
                "id": item_id,
                "type": "function",
                "function": {"name": name, "arguments": arguments or ""},
            }
        )
        return [
            StreamChunk(
                type=ChunkType.TOOL_CALL,
                tool_calls=list(state.tool_calls),
                raw=event,
            )
        ]

    def _handle_output_item_done(
        self, event: Any, state: _ResponsesStreamState, now: float
    ) -> list[StreamChunk]:
        item = self._response_to_raw(getattr(event, "item", None))
        item_type = item.get("type")
        if item_type == "function_call":
            name = item.get("name")
            if not isinstance(name, str) or not name:
                return []
            state.tool_calls.append(
                {
                    "id": str(item.get("call_id") or item.get("id") or ""),
                    "type": "function",
                    "function": {
                        "name": name,
                        "arguments": str(item.get("arguments") or ""),
                    },
                }
            )
            return [
                StreamChunk(
                    type=ChunkType.TOOL_CALL,
                    tool_calls=list(state.tool_calls),
                    raw=event,
                )
            ]
        if item_type != "message":
            return []

        item_id = item.get("id")
        content = item.get("content")
        if not isinstance(content, list):
            return []

        text_parts: list[str] = []
        for block in content:
            block_data = self._response_to_raw(block)
            if block_data.get("type") == "output_text" and isinstance(
                block_data.get("text"), str
            ):
                text_parts.append(block_data["text"])
            if block_data.get("type") == "refusal" and isinstance(
                block_data.get("refusal"), str
            ):
                text_parts.append(block_data["refusal"])

        text = "".join(text_parts)
        if not text:
            return []

        previous = (
            state.text_by_item.get(str(item_id), "") if item_id is not None else ""
        )
        delta = (
            text[len(previous) :] if previous and text.startswith(previous) else text
        )
        if not delta:
            return []

        if item_id is not None:
            state.text_by_item[str(item_id)] = text
        state.last_token_time = now
        return [
            StreamChunk(
                type=ChunkType.TOKEN,
                content=delta,
                delta=delta,
                raw=event,
            )
        ]

    def _finalize_stream(
        self, final_response: Any, state: _ResponsesStreamState
    ) -> list[StreamChunk]:
        chunks: list[StreamChunk] = []

        if not state.tool_calls:
            tool_calls = self._extract_tool_calls(final_response)
            if tool_calls:
                state.tool_calls.extend(tool_calls)
                chunks.append(
                    StreamChunk(
                        type=ChunkType.TOOL_CALL,
                        tool_calls=list(state.tool_calls),
                        raw=self._response_to_raw(final_response),
                    )
                )

        text = self._extract_output_text(final_response)
        streamed_text = "".join(state.text_by_item.values())
        if text and not streamed_text:
            chunks.append(
                StreamChunk(
                    type=ChunkType.TOKEN,
                    content=text,
                    delta=text,
                    raw=self._response_to_raw(final_response),
                )
            )

        usage = self._extract_usage(final_response)
        if usage:
            self._record_token_usage(final_response, call_type="stream_chat")
            chunks.append(
                StreamChunk(
                    type=ChunkType.USAGE,
                    usage=usage,
                    raw=self._response_to_raw(final_response),
                )
            )
        return chunks

    async def chat(
        self,
        messages: list[dict[str, str]],
        temperature: float | None = None,
        max_tokens: int | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        response_format: dict[str, Any] | None = None,
        thinking: dict[str, Any] | None = None,
        output_config: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        _ = thinking

        self._ensure_client()
        assert self._client is not None
        request_data = await self._prepare_request(
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            tools=tools,
            tool_choice=tool_choice,
            response_format=response_format,
            output_config=output_config,
            kwargs=dict(kwargs),
        )

        try:
            async with self._client.responses.stream(**request_data) as stream:
                async for _event in stream:
                    pass
                final_response = await stream.get_final_response()
                self._record_token_usage(final_response, call_type="chat")
                return self._parse_chat_response(final_response)
        except openai.APITimeoutError as e:
            raise LLMRetryableError(f"OpenAI Responses API timeout: {e}") from e
        except openai.RateLimitError as e:
            raise LLMRetryableError(f"OpenAI rate limit exceeded: {e}") from e

    async def stream_chat(
        self,
        messages: list[dict[str, str]],
        temperature: float | None = None,
        max_tokens: int | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        response_format: dict[str, Any] | None = None,
        thinking: dict[str, Any] | None = None,
        output_config: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[StreamChunk]:
        _ = thinking

        self._ensure_client()
        assert self._client is not None
        request_data = await self._prepare_request(
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            tools=tools,
            tool_choice=tool_choice,
            response_format=response_format,
            output_config=output_config,
            kwargs=dict(kwargs),
        )
        state = _ResponsesStreamState(start_time=time.time())

        try:
            async with self._client.responses.stream(**request_data) as stream:
                async for event in stream:
                    try:
                        chunks = self._parse_stream_event(
                            event=event,
                            state=state,
                            now=time.time(),
                        )
                    except RuntimeError as e:
                        raise LLMRetryableError(str(e)) from e
                    for chunk in chunks:
                        yield chunk

                final_response = await stream.get_final_response()
                for chunk in self._finalize_stream(final_response, state):
                    yield chunk
        except openai.APITimeoutError as e:
            raise LLMRetryableError(f"OpenAI Responses API timeout: {e}") from e
        except openai.RateLimitError as e:
            raise LLMRetryableError(f"OpenAI rate limit exceeded: {e}") from e

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None

    async def __aenter__(self) -> "OpenAIResponsesLLM":
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await self.close()
