import httpx
import pytest

from xagent.web.services.openai_codex_oauth import (
    CodexOAuthResponsesLLM,
    CodexOAuthTokenManager,
)


@pytest.mark.asyncio
async def test_codex_oauth_llm_refreshes_and_retries_on_401(mocker):
    token_manager = CodexOAuthTokenManager(
        access_token="old-access",
        refresh_token="refresh",
        expires_at=None,
        account_id=None,
    )
    token_manager.refresh = mocker.AsyncMock()  # type: ignore[method-assign]

    llm = CodexOAuthResponsesLLM(
        model_name="gpt-5.2-codex",
        base_url="https://chatgpt.com/backend-api/codex",
        token_manager=token_manager,
        timeout=30.0,
    )

    from openai import AuthenticationError

    resp = httpx.Response(
        status_code=401,
        request=httpx.Request(
            "POST", "https://chatgpt.com/backend-api/codex/responses"
        ),
        text="unauthorized",
    )
    llm._delegate.chat = mocker.AsyncMock(  # type: ignore[attr-defined]
        side_effect=[
            AuthenticationError("unauthorized", response=resp, body=None),
            {"type": "text", "content": "ok", "raw": {}},
        ]
    )

    out = await llm.chat([{"role": "user", "content": "hi"}])
    assert out["type"] == "text"
    assert out["content"] == "ok"
    token_manager.refresh.assert_awaited_once()
    assert llm._delegate.chat.await_count == 2  # type: ignore[attr-defined]
