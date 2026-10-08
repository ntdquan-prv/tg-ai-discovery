"""Client for the OpenAI-compatible Responses API on Amazon Bedrock, used for structured answers by function call.

Structured output is a function call: the tool is declared flat (no nested ``function`` key) and the instructions tell
the model to answer only by calling it (guide section 13.2). ``tool_choice`` is ``auto``: gpt-oss models on Bedrock run
in the Harmony format, which rejects a named tool ("Only 'auto' tool_choice is supported", seen live on 2026-10-08), so
``forced_tool_choice`` is for models that accept one. Retries follow the guide: four attempts with two to twenty seconds
of exponential backoff on 429, 5xx and connection errors. Calls go through ``MeteredHttp`` so they are budgeted, timed
and logged like every provider call.
"""

import json
import math
from dataclasses import dataclass
from typing import Protocol, cast

import httpx
from pydantic import BaseModel, ConfigDict

from tg_discovery.providers.base import BudgetGuard, PriceSpec, ProviderCall
from tg_discovery.providers.metering import MeteredHttp

# Worst case when estimating the input size before a call: CJK text is close to one token per character.
ESTIMATED_CHARS_PER_TOKEN = 2
_PRICE_PAGE = "https://aws.amazon.com/bedrock/pricing/"
# With ``tool_choice`` left to the model, the instructions are what makes it answer through the function.
_CALL_INSTRUCTION = (
    "Answer only by calling the function {tool} exactly once, with every record in its arguments. Write no other text."
)


@dataclass(frozen=True, slots=True)
class TokenPrice:
    """USD per million tokens, as configured in ``ProviderSettings``."""

    input_usd_per_1m: float
    output_usd_per_1m: float

    def cost(self, input_tokens: int, output_tokens: int) -> float:
        return (input_tokens * self.input_usd_per_1m + output_tokens * self.output_usd_per_1m) / 1_000_000


class LlmUsage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0


class LlmResult(BaseModel):
    """The arguments the model passed to the forced function, with usage and the call record."""

    model_config = ConfigDict(frozen=True)

    model: str
    arguments: dict[str, object]
    usage: LlmUsage
    call: ProviderCall


class LlmError(Exception):
    """The call reached the model but did not produce a usable function call; the usage was still billed."""

    def __init__(self, message: str, *, call: ProviderCall, usage: LlmUsage) -> None:
        super().__init__(message)
        self.call = call
        self.usage = usage


class FunctionCaller(Protocol):
    """Anything that can force a model to answer through one function; the extractor depends on this only."""

    @property
    def model(self) -> str: ...

    async def call_function(
        self,
        *,
        system: str,
        prompt: str,
        tool_name: str,
        parameters: dict[str, object],
        max_output_tokens: int,
    ) -> LlmResult: ...


def _as_dict(value: object) -> dict[str, object]:
    return cast(dict[str, object], value) if isinstance(value, dict) else {}


def _as_int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _usage(payload: dict[str, object]) -> LlmUsage:
    usage = _as_dict(payload.get("usage"))
    return LlmUsage(input_tokens=_as_int(usage.get("input_tokens")), output_tokens=_as_int(usage.get("output_tokens")))


class BedrockResponsesClient:
    """Gets a Bedrock-hosted model to answer by calling one function."""

    def __init__(
        self,
        *,
        base_url: str,
        api_token: str,
        model: str,
        price: TokenPrice,
        client: httpx.AsyncClient,
        guard: BudgetGuard,
        reasoning_effort: str | None = "low",
        forced_tool_choice: bool = False,
        timeout_s: float = 120.0,
        max_concurrency: int = 4,
        retry_wait_s: float = 2.0,
    ) -> None:
        self.model = model
        self._url = f"{base_url.rstrip('/')}/responses"
        self._api_token = api_token
        self._price = price
        self._reasoning_effort = reasoning_effort
        self._forced_tool_choice = forced_tool_choice
        self._http = MeteredHttp(
            f"llm:{model}",
            PriceSpec(
                unit="token",
                usd_per_unit=0.0,
                verified_on=None,
                source_url=_PRICE_PAGE,
                notes="billed per token: the real cost is computed from the usage in each response",
            ),
            client,
            guard,
            timeout_s=timeout_s,
            max_concurrency=max_concurrency,
            retries=3,
            retry_wait_s=retry_wait_s,
            max_retry_wait_s=20.0,
        )

    async def call_function(
        self,
        *,
        system: str,
        prompt: str,
        tool_name: str,
        parameters: dict[str, object],
        max_output_tokens: int,
    ) -> LlmResult:
        """Ask the model to call ``tool_name`` with arguments matching ``parameters`` (a JSON schema)."""
        body: dict[str, object] = {
            "model": self.model,
            "instructions": f"{system}\n\n{_CALL_INSTRUCTION.format(tool=tool_name)}",
            "input": prompt,
            "max_output_tokens": max_output_tokens,
            "tools": [{"type": "function", "name": tool_name, "parameters": parameters}],
            "tool_choice": {"type": "function", "name": tool_name} if self._forced_tool_choice else "auto",
        }
        if self._reasoning_effort:
            body["reasoning"] = {"effort": self._reasoning_effort}

        estimated_input_tokens = math.ceil((len(system) + len(prompt)) / ESTIMATED_CHARS_PER_TOKEN)
        response, call = await self._http.request(
            "POST",
            self._url,
            endpoint="responses",
            headers={"Authorization": f"Bearer {self._api_token}"},
            json=body,
            estimated_cost_usd=self._price.cost(estimated_input_tokens, max_output_tokens),
            units_from=lambda r: _total_tokens(_usage(_as_dict(r.json()))),
            cost_from=lambda r: self._price.cost(*_tokens(_usage(_as_dict(r.json())))),
        )
        payload = _as_dict(response.json())
        usage = _usage(payload)
        if payload.get("status") == "failed":
            # Bedrock answers 200 and puts the refusal in the body, e.g. an unsupported ``tool_choice``.
            detail = str(_as_dict(payload.get("error")).get("message", "no reason given"))[:300]
            failed = call.model_copy(update={"status": "error", "error": f"the model call failed: {detail}"[:200]})
            raise LlmError(f"the model call failed: {detail}", call=failed, usage=usage)
        if payload.get("status") == "incomplete":
            reason = _as_dict(payload.get("incomplete_details")).get("reason", "unknown")
            msg = f"the response is incomplete ({reason}); raise max_output_tokens or lower the reasoning effort"
            raise LlmError(msg, call=call, usage=usage)
        return LlmResult(
            model=self.model, arguments=_function_arguments(payload, tool_name, call, usage), usage=usage, call=call
        )


def _tokens(usage: LlmUsage) -> tuple[int, int]:
    return usage.input_tokens, usage.output_tokens


def _total_tokens(usage: LlmUsage) -> float:
    return float(usage.input_tokens + usage.output_tokens)


def _function_arguments(
    payload: dict[str, object], tool_name: str, call: ProviderCall, usage: LlmUsage
) -> dict[str, object]:
    """Find the forced function call among the output items (reasoning items may come first) and decode it."""
    output = payload.get("output")
    for item in cast(list[object], output) if isinstance(output, list) else []:
        fields = _as_dict(item)
        if fields.get("type") == "function_call" and fields.get("name") == tool_name:
            try:
                arguments = json.loads(str(fields.get("arguments", "")))
            except json.JSONDecodeError as error:
                msg = f"the function arguments are not valid JSON: {error}"
                raise LlmError(msg, call=call, usage=usage) from error
            if isinstance(arguments, dict):
                return cast(dict[str, object], arguments)
            break
    msg = f"the response holds no call to {tool_name}"
    raise LlmError(msg, call=call, usage=usage)
