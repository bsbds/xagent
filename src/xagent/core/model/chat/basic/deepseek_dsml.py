from __future__ import annotations

import html
import json
import re
import uuid
from typing import Any, Iterable

from ..types import ChunkType, StreamChunk


class DeepSeekDSMLParseError(RuntimeError):
    """Raised when DeepSeek emits DSML text that cannot safely become tool calls."""


_DSML_TOKEN_RE = r"(?:｜｜DSML｜｜|｜DSML｜|\|\|DSML\|\||\|DSML\|)"
_DSML_TOOL_CALLS_BLOCK_RE = re.compile(
    rf"<\s*{_DSML_TOKEN_RE}\s*tool_calls\s*>"
    rf"(.*?)"
    rf"</\s*{_DSML_TOKEN_RE}\s*tool_calls\s*>",
    re.IGNORECASE | re.DOTALL,
)
_DSML_INVOKE_RE = re.compile(
    rf"<\s*{_DSML_TOKEN_RE}\s*invoke\b([^>]*)>"
    rf"(.*?)"
    rf"</\s*{_DSML_TOKEN_RE}\s*invoke\s*>",
    re.IGNORECASE | re.DOTALL,
)
_DSML_PARAMETER_RE = re.compile(
    rf"<\s*{_DSML_TOKEN_RE}\s*parameter\b([^>]*)>"
    rf"(.*?)"
    rf"</\s*{_DSML_TOKEN_RE}\s*parameter\s*>",
    re.IGNORECASE | re.DOTALL,
)
_DSML_ATTR_RE = re.compile(r"""([A-Za-z_][\w:-]*)\s*=\s*(?:"([^"]*)"|'([^']*)')""")
_DSML_MARKER_RE = re.compile(
    rf"<\s*/?\s*{_DSML_TOKEN_RE}\s*(?:tool_calls|invoke|parameter)\b",
    re.IGNORECASE,
)


def contains_deepseek_dsml_tool_markup(text: Any) -> bool:
    """Return true when text contains a DeepSeek DSML tool-call marker."""

    return isinstance(text, str) and _DSML_MARKER_RE.search(text) is not None


def normalize_deepseek_dsml_response(
    response: Any,
    *,
    tools: list[dict[str, Any]] | None,
    model_name: str,
) -> tuple[Any, bool]:
    """Convert DeepSeek DSML assistant content into Xagent tool-call payloads.

    The OpenAI-compatible base parser can only see official ``tool_calls``. Some
    DeepSeek V4 routes leak their DSML tool grammar into ``message.content``
    instead. This helper is deliberately provider-scoped: callers must opt in
    from DeepSeek-specific code paths and provide the tool schemas from the
    current request.
    """

    if isinstance(response, str):
        if not contains_deepseek_dsml_tool_markup(response):
            return response, False
        parsed = parse_deepseek_dsml_tool_calls(
            response,
            tools=tools,
            model_name=model_name,
        )
        return {
            "type": "tool_call",
            "content": parsed["content"] or None,
            "tool_calls": parsed["tool_calls"],
            "raw": response,
        }, True

    if not isinstance(response, dict):
        return response, False

    if response.get("tool_calls"):
        return response, False

    content = response.get("content")
    if not isinstance(content, str) or not contains_deepseek_dsml_tool_markup(content):
        return response, False

    parsed = parse_deepseek_dsml_tool_calls(
        content,
        tools=tools,
        model_name=model_name,
    )
    normalized = dict(response)
    normalized["type"] = "tool_call"
    normalized["content"] = parsed["content"] or None
    normalized["tool_calls"] = parsed["tool_calls"]
    return normalized, True


def parse_deepseek_dsml_tool_calls(
    text: str,
    *,
    tools: list[dict[str, Any]] | None,
    model_name: str,
) -> dict[str, Any]:
    """Parse complete DeepSeek DSML ``tool_calls`` blocks from assistant text."""

    if not tools:
        raise DeepSeekDSMLParseError(
            f"{model_name} returned DSML tool markup, but no tools were supplied."
        )

    available_tools = _available_tool_names(tools)
    if not available_tools:
        raise DeepSeekDSMLParseError(
            f"{model_name} returned DSML tool markup, but no callable tool schemas were supplied."
        )

    matches = list(_DSML_TOOL_CALLS_BLOCK_RE.finditer(text))
    if not matches:
        raise DeepSeekDSMLParseError(
            f"{model_name} returned incomplete DeepSeek DSML tool markup."
        )

    clean_parts: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    last_index = 0

    for match in matches:
        clean_parts.append(text[last_index : match.start()])
        tool_calls.extend(
            _parse_dsml_invokes(
                match.group(1),
                available_tools=available_tools,
                model_name=model_name,
            )
        )
        last_index = match.end()

    clean_parts.append(text[last_index:])
    clean_content = "".join(clean_parts).strip()
    if contains_deepseek_dsml_tool_markup(clean_content):
        raise DeepSeekDSMLParseError(
            f"{model_name} returned unparsed DeepSeek DSML tool markup."
        )
    if not tool_calls:
        raise DeepSeekDSMLParseError(
            f"{model_name} returned DSML tool markup without any valid invoke blocks."
        )

    return {"content": clean_content, "tool_calls": tool_calls}


def stream_chunks_from_chat_result(response: Any) -> Iterable[StreamChunk]:
    """Represent a non-streaming DeepSeek chat result as stream chunks."""

    if isinstance(response, dict):
        content = _response_content(response)
        if response.get("tool_calls"):
            if content:
                yield StreamChunk(
                    type=ChunkType.TOKEN,
                    content=content,
                    delta=content,
                    raw=response,
                )
            yield StreamChunk(
                type=ChunkType.TOOL_CALL,
                tool_calls=list(response.get("tool_calls") or []),
                finish_reason="tool_calls",
                raw=response,
            )
            return

        if content:
            yield StreamChunk(
                type=ChunkType.TOKEN,
                content=content,
                delta=content,
                raw=response,
            )
        yield StreamChunk(type=ChunkType.END, finish_reason="stop", raw=response)
        return

    content = str(response)
    if content:
        yield StreamChunk(
            type=ChunkType.TOKEN,
            content=content,
            delta=content,
            raw=response,
        )
    yield StreamChunk(type=ChunkType.END, finish_reason="stop", raw=response)


def _parse_dsml_invokes(
    block_content: str,
    *,
    available_tools: set[str],
    model_name: str,
) -> list[dict[str, Any]]:
    invoke_matches = list(_DSML_INVOKE_RE.finditer(block_content))
    if not invoke_matches:
        raise DeepSeekDSMLParseError(
            f"{model_name} returned a DSML tool_calls block without invoke blocks."
        )

    tool_calls: list[dict[str, Any]] = []
    for match in invoke_matches:
        attrs = _parse_dsml_attrs(match.group(1))
        tool_name = attrs.get("name", "").strip()
        if not tool_name:
            raise DeepSeekDSMLParseError(
                f"{model_name} returned a DSML invoke without a tool name."
            )
        if tool_name not in available_tools:
            raise DeepSeekDSMLParseError(
                f"{model_name} returned a DSML invoke for unknown tool '{tool_name}'."
            )

        arguments = _parse_dsml_parameters(
            match.group(2),
            model_name=model_name,
            tool_name=tool_name,
        )
        tool_calls.append(
            {
                "id": f"call_dsml_{uuid.uuid4().hex[:24]}",
                "type": "function",
                "function": {
                    "name": tool_name,
                    "arguments": json.dumps(arguments, ensure_ascii=False),
                },
            }
        )

    return tool_calls


def _parse_dsml_parameters(
    invoke_content: str,
    *,
    model_name: str,
    tool_name: str,
) -> dict[str, Any]:
    arguments: dict[str, Any] = {}
    for match in _DSML_PARAMETER_RE.finditer(invoke_content):
        attrs = _parse_dsml_attrs(match.group(1))
        name = attrs.get("name", "").strip()
        string_attr = attrs.get("string", "").strip().lower()
        if not name:
            raise DeepSeekDSMLParseError(
                f"{model_name} returned a DSML parameter without a name for tool '{tool_name}'."
            )
        if name in arguments:
            raise DeepSeekDSMLParseError(
                f"{model_name} returned duplicate DSML parameter '{name}' for tool '{tool_name}'."
            )
        if string_attr not in {"true", "false"}:
            raise DeepSeekDSMLParseError(
                f"{model_name} returned DSML parameter '{name}' with invalid string attribute."
            )

        raw_value = html.unescape(match.group(2))
        if string_attr == "true":
            arguments[name] = raw_value
        else:
            arguments[name] = _parse_json_value_or_string(raw_value.strip())

    return arguments


def _parse_dsml_attrs(raw_attrs: str) -> dict[str, str]:
    attrs: dict[str, str] = {}
    for match in _DSML_ATTR_RE.finditer(raw_attrs):
        value = match.group(2) if match.group(2) is not None else match.group(3)
        attrs[match.group(1).strip().lower()] = html.unescape(value or "")
    return attrs


def _parse_json_value_or_string(value: str) -> Any:
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def _available_tool_names(tools: list[dict[str, Any]]) -> set[str]:
    names: set[str] = set()
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        function = tool.get("function")
        if not isinstance(function, dict):
            continue
        name = function.get("name")
        if isinstance(name, str) and name.strip():
            names.add(name.strip())
    return names


def _response_content(response: dict[str, Any]) -> str:
    for key in ("content", "answer", "output", "message"):
        value = response.get(key)
        if isinstance(value, str):
            return value
    return ""
