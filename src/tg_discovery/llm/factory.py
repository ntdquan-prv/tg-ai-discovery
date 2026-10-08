"""Builds the extraction model client from the settings; the service and the bake-off harness share it."""

import httpx

from tg_discovery.config import ProviderSettings
from tg_discovery.llm.bedrock_responses import BedrockResponsesClient, TokenPrice
from tg_discovery.providers.base import BudgetGuard


class LlmNotConfiguredError(RuntimeError):
    """The token or the endpoint of the extraction model is missing."""


def bedrock_from_settings(
    settings: ProviderSettings, client: httpx.AsyncClient, budget: BudgetGuard
) -> BedrockResponsesClient:
    """The Bedrock client the shared extractor talks to, spending from ``budget``."""
    token, url = settings.aws_bedrock_api_token, settings.aws_bedrock_openai_url
    if token is None or not url:
        msg = "extraction needs a model: set AWS_BEDROCK_API_TOKEN and AWS_BEDROCK_OPENAI_URL (see .env.sample)"
        raise LlmNotConfiguredError(msg)
    effort = None if settings.extractor_reasoning_effort == "none" else settings.extractor_reasoning_effort
    return BedrockResponsesClient(
        base_url=url,
        api_token=token.get_secret_value(),
        model=settings.extractor_model_id,
        price=TokenPrice(settings.extractor_input_usd_per_1m_tokens, settings.extractor_output_usd_per_1m_tokens),
        client=client,
        guard=budget,
        reasoning_effort=effort,
        forced_tool_choice=settings.extractor_forced_tool_choice,
    )
