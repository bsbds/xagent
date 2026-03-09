from typing import Optional

# Provider default base URLs for OpenAI-compatible "coding plan" endpoints.
# These are separate from the standard Zhipu "paas/v4" endpoints.
_DEFAULT_BASE_URL_BY_PROVIDER: dict[str, str] = {
    # Opencode / models.dev naming
    "zai-coding-plan": "https://api.z.ai/api/coding/paas/v4",
    "zhipuai-coding-plan": "https://open.bigmodel.cn/api/coding/paas/v4",
    # Underscore aliases (in case callers normalize differently)
    "zai_coding_plan": "https://api.z.ai/api/coding/paas/v4",
    "zhipuai_coding_plan": "https://open.bigmodel.cn/api/coding/paas/v4",
    # Alibaba Bailian (Model Studio) coding plan
    "alibaba-coding-plan": "https://coding-intl.dashscope.aliyuncs.com/v1",
    "alibaba-coding-plan-cn": "https://coding.dashscope.aliyuncs.com/v1",
    "alibaba_coding_plan": "https://coding-intl.dashscope.aliyuncs.com/v1",
    "alibaba_coding_plan_cn": "https://coding.dashscope.aliyuncs.com/v1",
    # MiniMax coding plan (Anthropic-compatible)
    "minimax-coding-plan": "https://api.minimax.io/anthropic/v1",
    "minimax-cn-coding-plan": "https://api.minimaxi.com/anthropic/v1",
    "minimax_coding_plan": "https://api.minimax.io/anthropic/v1",
    "minimax_cn_coding_plan": "https://api.minimaxi.com/anthropic/v1",
    # Kimi for Coding (Anthropic-compatible)
    "kimi-for-coding": "https://api.kimi.com/coding/v1",
    "kimi_for_coding": "https://api.kimi.com/coding/v1",
    # OpenAI Codex via ChatGPT OAuth
    "openai-codex-oauth": "https://chatgpt.com/backend-api/codex",
    "openai_codex_oauth": "https://chatgpt.com/backend-api/codex",
    # OpenAI Responses API via API key
    "openai-responses": "https://api.openai.com/v1",
    "openai_responses": "https://api.openai.com/v1",
}


def default_base_url_for_provider(provider: str) -> Optional[str]:
    provider_norm = provider.lower().strip()
    return _DEFAULT_BASE_URL_BY_PROVIDER.get(provider_norm)
