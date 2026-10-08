"""Fixtures for adapter contract tests: a metered HTTP layer over a respx-mocked client and a real budget."""

from collections.abc import AsyncIterator

import httpx
import pytest

from tests.support import HttpFactory
from tg_discovery.pipelines.common.budget import Budget
from tg_discovery.providers.base import PriceSpec
from tg_discovery.providers.metering import MeteredHttp


@pytest.fixture
def budget() -> Budget:
    return Budget(5.0)


@pytest.fixture
async def make_http(budget: Budget) -> AsyncIterator[HttpFactory]:
    """Build a ``MeteredHttp`` for an adapter under test; retries wait zero seconds so failure tests stay fast."""
    async with httpx.AsyncClient() as client:

        def make(
            provider: str, price: PriceSpec, *, max_concurrency: int = 4, min_interval_s: float = 0.0
        ) -> MeteredHttp:
            return MeteredHttp(
                provider,
                price,
                client,
                budget,
                max_concurrency=max_concurrency,
                min_interval_s=min_interval_s,
                retry_wait_s=0.0,
            )

        yield make
