from __future__ import annotations

import asyncio
import base64
import json
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Callable, List, Optional, cast

import httpx
import openai

from xagent.core.model.chat.basic.base import BaseLLM
from xagent.core.model.chat.basic.openai_responses import OpenAIResponsesLLM

CODEX_OAUTH_PROVIDER_ID = "openai-codex-oauth"

_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
_ISSUER = "https://auth.openai.com"
_DEFAULT_CODEX_BASE_URL = "https://chatgpt.com/backend-api/codex"


@dataclass
class TokenResponse:
    access_token: str
    refresh_token: str
    id_token: Optional[str] = None
    expires_in: Optional[int] = None


async def exchange_code_for_tokens(
    code: str, redirect_uri: str, code_verifier: str
) -> TokenResponse:
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            f"{_ISSUER}/oauth/token",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "client_id": _CLIENT_ID,
                "code_verifier": code_verifier,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        return TokenResponse(
            access_token=str(data["access_token"]),
            refresh_token=str(data["refresh_token"]),
            id_token=str(data.get("id_token")) if data.get("id_token") else None,
            expires_in=int(data["expires_in"]) if data.get("expires_in") else None,
        )


async def refresh_access_token(refresh_token: str) -> TokenResponse:
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            f"{_ISSUER}/oauth/token",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": _CLIENT_ID,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        return TokenResponse(
            access_token=str(data["access_token"]),
            refresh_token=str(data.get("refresh_token") or refresh_token),
            id_token=str(data.get("id_token")) if data.get("id_token") else None,
            expires_in=int(data["expires_in"]) if data.get("expires_in") else None,
        )


async def start_device_auth() -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            f"{_ISSUER}/api/accounts/deviceauth/usercode",
            headers={"Content-Type": "application/json"},
            json={"client_id": _CLIENT_ID},
        )
        resp.raise_for_status()
        data = resp.json()
        return {
            "device_auth_id": data.get("device_auth_id"),
            "user_code": data.get("user_code"),
            "interval": int(data.get("interval") or 5),
            "verification_url": f"{_ISSUER}/codex/device",
        }


async def poll_device_auth(device_auth_id: str, user_code: str) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            f"{_ISSUER}/api/accounts/deviceauth/token",
            headers={"Content-Type": "application/json"},
            json={"device_auth_id": device_auth_id, "user_code": user_code},
        )
        if resp.status_code in (403, 404):
            return {"status": "pending"}
        resp.raise_for_status()
        data = resp.json()
        authorization_code = data.get("authorization_code")
        code_verifier = data.get("code_verifier")
        if not authorization_code or not code_verifier:
            raise RuntimeError("Invalid device auth token response")

        tokens = await exchange_code_for_tokens(
            str(authorization_code),
            f"{_ISSUER}/deviceauth/callback",
            str(code_verifier),
        )
        return {"status": "success", "tokens": tokens}


def _parse_jwt_claims(token: str) -> Optional[dict[str, Any]]:
    parts = token.split(".")
    if len(parts) != 3:
        return None
    try:
        payload = parts[1]
        payload += "=" * (-len(payload) % 4)
        raw = base64.urlsafe_b64decode(payload.encode("ascii"))
        decoded = json.loads(raw.decode("utf-8"))
        if isinstance(decoded, dict):
            return cast(dict[str, Any], decoded)
        return None
    except Exception:
        return None


def _extract_account_id_from_claims(claims: dict[str, Any]) -> Optional[str]:
    if isinstance(claims.get("chatgpt_account_id"), str):
        return str(claims["chatgpt_account_id"])
    api_auth = claims.get("https://api.openai.com/auth")
    if isinstance(api_auth, dict) and isinstance(
        api_auth.get("chatgpt_account_id"), str
    ):
        return str(api_auth["chatgpt_account_id"])
    orgs = claims.get("organizations")
    if isinstance(orgs, list) and orgs:
        first = orgs[0]
        if isinstance(first, dict) and isinstance(first.get("id"), str):
            return str(first["id"])
    return None


def extract_account_id(tokens: TokenResponse) -> Optional[str]:
    if tokens.id_token:
        claims = _parse_jwt_claims(tokens.id_token)
        if claims:
            account_id = _extract_account_id_from_claims(claims)
            if account_id:
                return account_id
    if tokens.access_token:
        claims = _parse_jwt_claims(tokens.access_token)
        if claims:
            return _extract_account_id_from_claims(claims)
    return None


def compute_expires_at(expires_in: Optional[int]) -> Optional[datetime]:
    if not expires_in:
        return None
    return datetime.now(tz=UTC) + timedelta(seconds=int(expires_in))


def _ensure_utc_aware(value: datetime) -> datetime:
    # SQLite may return timezone-naive datetimes even when storing timezone-aware.
    # Treat naive values as UTC since we always compute expiry in UTC.
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _normalize_codex_base_url(base_url: str | None) -> str:
    if not base_url:
        return _DEFAULT_CODEX_BASE_URL
    url = str(base_url).strip().rstrip("/")
    if url.endswith("/responses"):
        url = url[: -len("/responses")]
    return url or _DEFAULT_CODEX_BASE_URL


class CodexOAuthTokenManager:
    def __init__(
        self,
        *,
        access_token: str,
        refresh_token: str,
        expires_at: Optional[datetime],
        account_id: Optional[str],
        on_refresh: Optional[
            Callable[[str, str, Optional[datetime], Optional[str]], None]
        ] = None,
        refresh_skew_seconds: int = 60,
    ) -> None:
        self._access_token = access_token
        self._refresh_token = refresh_token
        self._expires_at = expires_at
        self._account_id = account_id
        self._on_refresh = on_refresh
        self._refresh_skew_seconds = int(refresh_skew_seconds)
        self._lock = asyncio.Lock()

    @property
    def account_id(self) -> Optional[str]:
        return self._account_id

    def _needs_refresh(self) -> bool:
        if not self._access_token:
            return True
        if self._expires_at is None:
            return False
        expiry = _ensure_utc_aware(self._expires_at)
        return expiry <= datetime.now(tz=UTC) + timedelta(
            seconds=self._refresh_skew_seconds
        )

    async def refresh(self) -> None:
        tokens = await refresh_access_token(self._refresh_token)
        self._access_token = tokens.access_token
        self._refresh_token = tokens.refresh_token
        self._expires_at = compute_expires_at(tokens.expires_in)
        new_account_id = extract_account_id(tokens)
        if new_account_id:
            self._account_id = new_account_id
        if self._on_refresh is not None:
            try:
                self._on_refresh(
                    self._access_token,
                    self._refresh_token,
                    self._expires_at,
                    self._account_id,
                )
            except Exception:
                pass

    async def ensure_valid(self) -> None:
        if not self._needs_refresh():
            return
        async with self._lock:
            if not self._needs_refresh():
                return
            await self.refresh()

    async def build_auth_headers(
        self, request_context: dict[str, Any]
    ) -> dict[str, str]:
        await self.ensure_valid()
        headers: dict[str, str] = {
            # OpenAI SDK sets `Authorization` internally from `api_key`.
            # Use the same canonical header name to override it and avoid duplicates
            # like `Authorization` + `authorization` which can trigger edge/WAF 400s.
            "Authorization": f"Bearer {self._access_token}",
        }
        session_id = request_context.get("session_id")
        if isinstance(session_id, str) and session_id:
            headers["session_id"] = session_id
        if self._account_id:
            headers["ChatGPT-Account-Id"] = self._account_id
        return headers


class CodexOAuthResponsesLLM(BaseLLM):
    def __init__(
        self,
        *,
        model_name: str,
        base_url: Optional[str],
        token_manager: CodexOAuthTokenManager,
        timeout: float = 180.0,
        abilities: Optional[List[str]] = None,
        originator: str = "xagent",
        user_agent: str = "xagent",
        session_id: Optional[str] = None,
    ) -> None:
        self._model_name = model_name
        self._token_manager = token_manager
        self._originator = originator
        self._user_agent = user_agent
        self._session_id = session_id or secrets.token_hex(16)

        self._delegate = OpenAIResponsesLLM(
            model_name=model_name,
            # OpenAI SDK requires an api_key; we override Authorization per-request.
            api_key="__oauth__",
            base_url=_normalize_codex_base_url(base_url),
            default_store=False,
            timeout=timeout,
            abilities=abilities or ["chat", "tool_calling"],
            default_headers={
                "originator": self._originator,
                "User-Agent": self._user_agent,
                # Default session id can still be overridden per call via kwargs.
                "session_id": self._session_id,
            },
            extra_headers_provider=self._token_manager.build_auth_headers,
        )

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def abilities(self) -> List[str]:
        return self._delegate.abilities

    @property
    def supports_thinking_mode(self) -> bool:
        return False

    async def chat(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        try:
            return await self._delegate.chat(*args, **kwargs)
        except openai.AuthenticationError:
            # Mirror Codex/OpenCode behavior: refresh and retry once on 401.
            await self._token_manager.refresh()
            return await self._delegate.chat(*args, **kwargs)
        except openai.APIStatusError as e:
            if getattr(e, "status_code", None) == 401:
                await self._token_manager.refresh()
                return await self._delegate.chat(*args, **kwargs)
            raise

    async def stream_chat(self, *args: Any, **kwargs: Any) -> Any:
        try:
            async for chunk in self._delegate.stream_chat(*args, **kwargs):
                yield chunk
        except openai.AuthenticationError:
            await self._token_manager.refresh()
            async for chunk in self._delegate.stream_chat(*args, **kwargs):
                yield chunk
        except openai.APIStatusError as e:
            if getattr(e, "status_code", None) == 401:
                await self._token_manager.refresh()
                async for chunk in self._delegate.stream_chat(*args, **kwargs):
                    yield chunk
            else:
                raise
