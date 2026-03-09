"""Service to fetch available models from various providers using their SDKs."""

import logging
import re
from typing import Any, Dict, List, Optional

import httpx

from ...core.model.providers import (
    curated_models_for_provider,
    default_base_url_for_provider,
    get_supported_provider_metadata,
)
from ...core.utils.security import redact_sensitive_text
from .. import __version__ as WEB_VERSION

logger = logging.getLogger(__name__)

_MINIMAX_CODING_PLAN_MODELS = (
    "MiniMax-M2",
    "MiniMax-M2.1",
    "MiniMax-M2.5",
)


def _static_model_list(models: tuple[str, ...], owned_by: str) -> List[Dict[str, Any]]:
    return [{"id": model_id, "created": 0, "owned_by": owned_by} for model_id in models]


def _whole_client_version(version: str) -> str:
    match = re.match(r"^(\d+)\.(\d+)\.(\d+)", version or "")
    if match:
        return ".".join(match.groups())
    return "0.0.0"


async def fetch_openai_models(
    api_key: str, base_url: Optional[str] = None
) -> List[Dict[str, Any]]:
    """Fetch available models from OpenAI using OpenAILLM.list_available_models().

    Args:
        api_key: OpenAI API key
        base_url: Custom base URL (optional)

    Returns:
        List of available models with their information
    """
    from ...core.model.chat.basic.openai import OpenAILLM

    return await OpenAILLM.list_available_models(api_key, base_url)


async def fetch_openai_responses_models(
    api_key: str, base_url: Optional[str] = None
) -> List[Dict[str, Any]]:
    """Fetch available models for the OpenAI Responses API provider."""
    from ...core.model.chat.basic.openai import OpenAILLM

    return await OpenAILLM.list_available_models(api_key, base_url)


async def fetch_openai_codex_oauth_models(
    api_key: str, base_url: Optional[str] = None
) -> List[Dict[str, Any]]:
    """Fetch available models from the ChatGPT Codex backend."""
    from .openai_codex_oauth import _normalize_codex_base_url

    client_version = _whole_client_version(WEB_VERSION)
    url = (
        _normalize_codex_base_url(base_url) + f"/models?client_version={client_version}"
    )
    headers = {
        "Authorization": f"Bearer {api_key}",
        "originator": "xagent",
        "User-Agent": "xagent",
    }

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(url, headers=headers)
            response.raise_for_status()
            data = response.json()

            raw_models = data.get("models", data.get("data", data))
            if not isinstance(raw_models, list):
                return []

            models = []
            for model in raw_models:
                if not isinstance(model, dict):
                    continue
                model_id = model.get("slug") or model.get("id")
                if not isinstance(model_id, str) or not model_id:
                    continue
                models.append(
                    {
                        "id": model_id,
                        "created": int(model.get("created") or 0),
                        "owned_by": model.get("owned_by") or "openai-codex-oauth",
                        "display_name": model.get("display_name"),
                        "description": model.get("description"),
                        "priority": int(model.get("priority") or 0),
                        "supported_in_api": bool(model.get("supported_in_api", True)),
                    }
                )

            models.sort(
                key=lambda x: (x.get("created", 0), x.get("priority", 0)),
                reverse=True,
            )
            return models

    except httpx.HTTPStatusError as e:
        logger.error(
            f"HTTP error fetching Codex OAuth models: {e.response.status_code}"
        )
        if e.response.status_code == 401:
            raise ValueError("Invalid API key") from e
        raise
    except Exception as e:
        logger.error(f"Failed to fetch Codex OAuth models: {e}")
        return []


async def fetch_zhipu_models(
    api_key: str, base_url: Optional[str] = None
) -> List[Dict[str, Any]]:
    """Fetch available models from Zhipu AI using ZhipuLLM.list_available_models().

    Args:
        api_key: Zhipu API key
        base_url: Custom base URL (optional)

    Returns:
        List of available Zhipu models
    """
    from ...core.model.chat.basic.zhipu import ZhipuLLM

    return await ZhipuLLM.list_available_models(api_key, base_url)


async def fetch_claude_models(
    api_key: str, base_url: Optional[str] = None
) -> List[Dict[str, Any]]:
    """Fetch available models from Anthropic Claude using ClaudeLLM.list_available_models().

    Args:
        api_key: Anthropic API key
        base_url: Custom base URL (optional)

    Returns:
        List of available Claude models
    """
    from ...core.model.chat.basic.claude import ClaudeLLM

    return await ClaudeLLM.list_available_models(api_key, base_url)


async def fetch_gemini_models(
    api_key: str, base_url: Optional[str] = None
) -> List[Dict[str, Any]]:
    """Fetch available models from Google Gemini using GeminiLLM.list_available_models().

    Args:
        api_key: Google API key
        base_url: Custom base URL (optional)

    Returns:
        List of available Gemini models
    """
    from ...core.model.chat.basic.gemini import GeminiLLM

    return await GeminiLLM.list_available_models(api_key, base_url)


async def fetch_xinference_models(
    api_key: str, base_url: Optional[str] = None
) -> List[Dict[str, Any]]:
    """Fetch available models from Xinference using XinferenceLLM.list_available_models().

    Args:
        api_key: Xinference API key (optional)
        base_url: Xinference server base URL (required)

    Returns:
        List of available Xinference models
    """
    if not base_url:
        raise ValueError("base_url is required for Xinference")

    from ...core.model.chat.basic.xinference import XinferenceLLM

    return await XinferenceLLM.list_available_models(base_url=base_url, api_key=api_key)


async def fetch_alibaba_coding_plan_models(
    api_key: str, base_url: Optional[str] = None
) -> List[Dict[str, Any]]:
    """Return curated Alibaba Bailian coding plan models."""
    _ = api_key, base_url
    return _static_model_list(
        curated_models_for_provider("alibaba-coding-plan"),
        owned_by="alibaba-coding-plan",
    )


async def fetch_alibaba_coding_plan_cn_models(
    api_key: str, base_url: Optional[str] = None
) -> List[Dict[str, Any]]:
    """Return curated Alibaba Bailian coding plan models (China)."""
    _ = api_key, base_url
    return _static_model_list(
        curated_models_for_provider("alibaba-coding-plan-cn"),
        owned_by="alibaba-coding-plan-cn",
    )


async def fetch_minimax_coding_plan_models(
    api_key: str, base_url: Optional[str] = None
) -> List[Dict[str, Any]]:
    """Return curated MiniMax coding plan models (minimax.io)."""
    _ = api_key, base_url
    return _static_model_list(
        _MINIMAX_CODING_PLAN_MODELS, owned_by="minimax-coding-plan"
    )


async def fetch_minimax_cn_coding_plan_models(
    api_key: str, base_url: Optional[str] = None
) -> List[Dict[str, Any]]:
    """Return curated MiniMax coding plan models (minimaxi.com)."""
    _ = api_key, base_url
    return _static_model_list(
        _MINIMAX_CODING_PLAN_MODELS, owned_by="minimax-cn-coding-plan"
    )


async def fetch_kimi_for_coding_models(
    api_key: str, base_url: Optional[str] = None
) -> List[Dict[str, Any]]:
    """Fetch available Kimi For Coding models via the Claude-compatible API."""
    return await fetch_claude_models(api_key, base_url)


# Provider registry mapping provider names to their fetch functions
PROVIDER_FETCHERS: Dict[str, Any] = {
    "openai": fetch_openai_models,
    "openai-responses": fetch_openai_responses_models,
    "openai-codex-oauth": fetch_openai_codex_oauth_models,
    "zhipu": fetch_zhipu_models,
    "claude": fetch_claude_models,
    "anthropic": fetch_claude_models,
    "gemini": fetch_gemini_models,
    "google": fetch_gemini_models,
    "xinference": fetch_xinference_models,
    "zai-coding-plan": fetch_openai_models,
    "zhipuai-coding-plan": fetch_openai_models,
    "alibaba-coding-plan": fetch_alibaba_coding_plan_models,
    "alibaba-coding-plan-cn": fetch_alibaba_coding_plan_cn_models,
    "minimax-coding-plan": fetch_minimax_coding_plan_models,
    "minimax-cn-coding-plan": fetch_minimax_cn_coding_plan_models,
    "kimi-for-coding": fetch_kimi_for_coding_models,
}


async def fetch_models_from_provider(
    provider: str,
    api_key: str,
    base_url: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Fetch available models from a specific provider.

    Args:
        provider: Provider name (openai, zhipu, claude, etc.)
        api_key: API key for the provider
        base_url: Custom base URL (optional)

    Returns:
        List of available models
    """
    provider_id = provider.lower()
    fetcher = PROVIDER_FETCHERS.get(provider_id)

    if not fetcher:
        logger.warning(f"Unknown provider: {provider}")
        return []

    try:
        resolved_base_url = base_url or default_base_url_for_provider(provider_id)
        result: List[Dict[str, Any]] = await fetcher(api_key, resolved_base_url)
        return result
    except Exception as e:
        logger.error(
            "Error fetching models from %s: %s",
            provider,
            redact_sensitive_text(str(e)),
        )
        raise


def get_supported_providers() -> List[Dict[str, Any]]:
    """Get list of supported providers.

    Returns:
        List of provider information
    """
    return get_supported_provider_metadata()
