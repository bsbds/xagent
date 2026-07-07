import json

import pytest

from xagent.core.model.chat.basic.deepseek_dsml import (
    DeepSeekDSMLParseError,
    normalize_deepseek_dsml_response,
    parse_deepseek_dsml_tool_calls,
)


def _tool_schema(name: str) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": f"{name} tool",
            "parameters": {"type": "object", "properties": {}},
        },
    }


def _tools(*names: str) -> list[dict]:
    return [_tool_schema(name) for name in names]


def _dsml_block(token: str, *, tool_name: str = "get_weather") -> str:
    return f"""
<{token}tool_calls>
<{token}invoke name="{tool_name}">
<{token}parameter name="location" string="true">Boston</{token}parameter>
<{token}parameter name="limit" string="false">5</{token}parameter>
<{token}parameter name="options" string="false">{{"fresh":true}}</{token}parameter>
</{token}invoke>
</{token}tool_calls>
""".strip()


@pytest.mark.parametrize(
    "token",
    [
        "｜DSML｜",
        "｜｜DSML｜｜",
        "|DSML|",
        "||DSML||",
    ],
)
def test_parse_deepseek_dsml_tool_calls_supports_known_token_variants(token):
    parsed = parse_deepseek_dsml_tool_calls(
        _dsml_block(token),
        tools=_tools("get_weather"),
        model_name="deepseek-v4-flash",
    )

    assert parsed["content"] == ""
    assert len(parsed["tool_calls"]) == 1
    tool_call = parsed["tool_calls"][0]
    assert tool_call["type"] == "function"
    assert tool_call["function"]["name"] == "get_weather"
    assert tool_call["id"].startswith("call_dsml_")
    assert json.loads(tool_call["function"]["arguments"]) == {
        "location": "Boston",
        "limit": 5,
        "options": {"fresh": True},
    }


def test_parse_deepseek_dsml_tool_calls_supports_multiple_invokes():
    token = "｜｜DSML｜｜"
    text = f"""
<{token}tool_calls>
<{token}invoke name="get_weather">
<{token}parameter name="location" string="true">Boston</{token}parameter>
</{token}invoke>
<{token}invoke name="browser_extract_text">
<{token}parameter name="session_id" string="true">821:react_468b98b2</{token}parameter>
</{token}invoke>
</{token}tool_calls>
""".strip()

    parsed = parse_deepseek_dsml_tool_calls(
        text,
        tools=_tools("get_weather", "browser_extract_text"),
        model_name="deepseek-v4-flash",
    )

    assert [call["function"]["name"] for call in parsed["tool_calls"]] == [
        "get_weather",
        "browser_extract_text",
    ]
    assert json.loads(parsed["tool_calls"][1]["function"]["arguments"]) == {
        "session_id": "821:react_468b98b2"
    }


def test_normalize_deepseek_dsml_response_strips_markup_from_visible_content():
    response = {
        "type": "text",
        "content": _dsml_block("｜｜DSML｜｜"),
        "raw": {"id": "deepseek-text"},
    }

    normalized, parsed = normalize_deepseek_dsml_response(
        response,
        tools=_tools("get_weather"),
        model_name="deepseek-v4-flash",
    )

    assert parsed is True
    assert normalized["type"] == "tool_call"
    assert normalized["content"] is None
    assert "DSML" not in json.dumps(normalized["content"])
    assert normalized["raw"] == {"id": "deepseek-text"}


def test_parse_deepseek_dsml_tool_calls_falls_back_to_string_for_invalid_json_value():
    token = "｜DSML｜"
    text = f"""
<{token}tool_calls>
<{token}invoke name="get_weather">
<{token}parameter name="query" string="false">not json</{token}parameter>
</{token}invoke>
</{token}tool_calls>
""".strip()

    parsed = parse_deepseek_dsml_tool_calls(
        text,
        tools=_tools("get_weather"),
        model_name="deepseek-v4-flash",
    )

    assert json.loads(parsed["tool_calls"][0]["function"]["arguments"]) == {
        "query": "not json"
    }


def test_parse_deepseek_dsml_tool_calls_preserves_string_parameter_whitespace():
    token = "｜DSML｜"
    text = f"""
<{token}tool_calls>
<{token}invoke name="write_file">
<{token}parameter name="content" string="true">  keep exact text
</{token}parameter>
</{token}invoke>
</{token}tool_calls>
""".strip()

    parsed = parse_deepseek_dsml_tool_calls(
        text,
        tools=_tools("write_file"),
        model_name="deepseek-v4-flash",
    )

    assert json.loads(parsed["tool_calls"][0]["function"]["arguments"]) == {
        "content": "  keep exact text\n"
    }


def test_parse_deepseek_dsml_tool_calls_rejects_duplicate_parameters():
    token = "｜DSML｜"
    text = f"""
<{token}tool_calls>
<{token}invoke name="get_weather">
<{token}parameter name="location" string="true">Boston</{token}parameter>
<{token}parameter name="location" string="true">Paris</{token}parameter>
</{token}invoke>
</{token}tool_calls>
""".strip()

    with pytest.raises(DeepSeekDSMLParseError, match="duplicate DSML parameter"):
        parse_deepseek_dsml_tool_calls(
            text,
            tools=_tools("get_weather"),
            model_name="deepseek-v4-flash",
        )


def test_parse_deepseek_dsml_tool_calls_rejects_unknown_tools():
    with pytest.raises(DeepSeekDSMLParseError, match="unknown tool"):
        parse_deepseek_dsml_tool_calls(
            _dsml_block("｜DSML｜", tool_name="unknown_tool"),
            tools=_tools("get_weather"),
            model_name="deepseek-v4-flash",
        )


def test_parse_deepseek_dsml_tool_calls_rejects_incomplete_blocks():
    text = '<｜DSML｜tool_calls><｜DSML｜invoke name="get_weather">'

    with pytest.raises(DeepSeekDSMLParseError, match="incomplete"):
        parse_deepseek_dsml_tool_calls(
            text,
            tools=_tools("get_weather"),
            model_name="deepseek-v4-flash",
        )


def test_parse_deepseek_dsml_tool_calls_rejects_missing_request_tools():
    with pytest.raises(DeepSeekDSMLParseError, match="no tools were supplied"):
        parse_deepseek_dsml_tool_calls(
            _dsml_block("｜DSML｜"),
            tools=None,
            model_name="deepseek-v4-flash",
        )
