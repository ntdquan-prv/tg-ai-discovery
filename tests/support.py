"""Helpers shared by test modules (conftest fixtures are not importable, this module is)."""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from tg_discovery.llm.bedrock_responses import LlmResult, LlmUsage
from tg_discovery.providers.base import PriceSpec, ProviderCall
from tg_discovery.providers.metering import MeteredHttp

FIXTURES = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)


class HttpFactory(Protocol):
    """Builds a ``MeteredHttp`` for an adapter under test (see the ``make_http`` fixture)."""

    def __call__(
        self, provider: str, price: PriceSpec, *, max_concurrency: int = 4, min_interval_s: float = 0.0
    ) -> MeteredHttp: ...


def load_fixture(provider: str, name: str) -> object:
    """Load a hand-written JSON response fixture from ``tests/fixtures/<provider>/<name>``."""
    return json.loads((FIXTURES / provider / name).read_text(encoding="utf-8"))


class FakeLlm:
    """A ``FunctionCaller`` that returns canned arguments and records every call it receives."""

    model = "fake-model"

    def __init__(self, arguments: dict[str, object] | None = None) -> None:
        self.arguments: dict[str, object] = arguments or {}
        self.calls: list[dict[str, object]] = []

    async def call_function(
        self, *, system: str, prompt: str, tool_name: str, parameters: dict[str, object], max_output_tokens: int
    ) -> LlmResult:
        self.calls.append(
            {
                "system": system,
                "prompt": prompt,
                "tool_name": tool_name,
                "parameters": parameters,
                "max_output_tokens": max_output_tokens,
            }
        )
        call = ProviderCall(
            provider=f"llm:{self.model}",
            endpoint="responses",
            units=120,
            cost_usd=0.0001,
            latency_ms=5,
            status="ok",
            at=NOW,
        )
        return LlmResult(
            model=self.model,
            arguments=self.arguments,
            usage=LlmUsage(input_tokens=100, output_tokens=20),
            call=call,
        )
