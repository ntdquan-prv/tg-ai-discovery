import asyncio
import time
from collections.abc import AsyncIterator
from datetime import date
from typing import Protocol

import httpx
import pytest
import respx

from tg_discovery.pipelines.common.budget import Budget
from tg_discovery.providers.base import BudgetExceededError, PriceSpec, ProviderError
from tg_discovery.providers.metering import MeteredHttp

PRICE = PriceSpec(unit="request", usd_per_unit=0.005, verified_on=date(2026, 10, 8), source_url="https://example.com")
URL = "https://api.example.com/search"


class MakeHttp(Protocol):
    def __call__(
        self, *, timeout_s: float = 30.0, max_concurrency: int = 4, min_interval_s: float = 0.0, retries: int = 1
    ) -> MeteredHttp: ...


@pytest.fixture
def budget() -> Budget:
    return Budget(1.0)


@pytest.fixture
async def make_http(budget: Budget) -> AsyncIterator[MakeHttp]:
    async with httpx.AsyncClient() as client:

        def make(
            *, timeout_s: float = 30.0, max_concurrency: int = 4, min_interval_s: float = 0.0, retries: int = 1
        ) -> MeteredHttp:
            return MeteredHttp(
                "example",
                PRICE,
                client,
                budget,
                timeout_s=timeout_s,
                max_concurrency=max_concurrency,
                min_interval_s=min_interval_s,
                retries=retries,
                retry_wait_s=0.0,
            )

        yield make


@respx.mock
async def test_a_successful_call_is_priced_and_settled(make_http: MakeHttp, budget: Budget) -> None:
    respx.get(URL).mock(return_value=httpx.Response(200, json={"results": []}))

    response, call = await make_http().request("GET", URL, endpoint="search")

    assert response.json() == {"results": []}
    assert (call.status, call.http_status, call.attempts) == ("ok", 200, 1)
    assert (call.provider, call.endpoint, call.units) == ("example", "search", 1.0)
    assert call.cost_usd == pytest.approx(0.005)
    assert budget.spent_by_provider() == pytest.approx({"example": 0.005})
    assert budget.reserved_usd == 0


@respx.mock
async def test_usage_reported_by_the_response_replaces_the_estimate(make_http: MakeHttp, budget: Budget) -> None:
    respx.get(URL).mock(return_value=httpx.Response(200, json={"usage": {"units": 3}}))

    _, call = await make_http().request(
        "GET", URL, endpoint="search", estimated_units=10, units_from=lambda r: r.json()["usage"]["units"]
    )

    assert call.units == 3
    assert call.cost_usd == pytest.approx(0.015)
    assert budget.spent_usd == pytest.approx(0.015)


@respx.mock
async def test_an_estimate_stated_in_dollars_is_what_gets_billed_when_nothing_is_reported(
    make_http: MakeHttp, budget: Budget
) -> None:
    respx.get(URL).mock(return_value=httpx.Response(200, json={}))

    _, call = await make_http().request("GET", URL, endpoint="responses", estimated_cost_usd=0.02)

    assert call.cost_usd == pytest.approx(0.02)
    assert budget.spent_usd == pytest.approx(0.02)


@respx.mock
async def test_dollars_reported_by_the_response_win_over_units(make_http: MakeHttp) -> None:
    respx.get(URL).mock(return_value=httpx.Response(200, json={"cost": 0.0123}))

    _, call = await make_http().request("GET", URL, endpoint="search", cost_from=lambda r: r.json()["cost"])

    assert call.cost_usd == pytest.approx(0.0123)


@respx.mock
async def test_a_response_without_usage_keeps_the_estimate(make_http: MakeHttp) -> None:
    respx.get(URL).mock(return_value=httpx.Response(200, json={}))

    _, call = await make_http().request(
        "GET", URL, endpoint="search", estimated_units=2, units_from=lambda r: r.json()["usage"]["units"]
    )

    assert call.units == 2
    assert call.cost_usd == pytest.approx(0.01)


@respx.mock
async def test_a_server_error_is_retried_once_and_billed_once(make_http: MakeHttp, budget: Budget) -> None:
    route = respx.get(URL).mock(side_effect=[httpx.Response(503), httpx.Response(200, json={})])

    _, call = await make_http().request("GET", URL, endpoint="search")

    assert route.call_count == 2
    assert (call.status, call.attempts) == ("ok", 2)
    assert budget.spent_usd == pytest.approx(0.005)


@respx.mock
async def test_repeated_rate_limiting_ends_in_an_unbilled_provider_error(make_http: MakeHttp, budget: Budget) -> None:
    route = respx.get(URL).mock(return_value=httpx.Response(429, text="slow down"))

    with pytest.raises(ProviderError, match="HTTP 429") as caught:
        await make_http().request("GET", URL, endpoint="search")

    assert route.call_count == 2
    call = caught.value.call
    assert (call.status, call.http_status, call.attempts, call.cost_usd) == ("error", 429, 2, 0)
    assert budget.spent_usd == 0
    assert budget.reserved_usd == 0


@respx.mock
async def test_client_errors_are_not_retried(make_http: MakeHttp) -> None:
    route = respx.get(URL).mock(return_value=httpx.Response(401, json={"error": "invalid key"}))

    with pytest.raises(ProviderError, match="HTTP 401") as caught:
        await make_http().request("GET", URL, endpoint="search")

    assert route.call_count == 1
    assert caught.value.call.attempts == 1


@respx.mock
async def test_transport_errors_are_retried_then_reported(make_http: MakeHttp) -> None:
    route = respx.get(URL).mock(side_effect=httpx.ConnectError("connection refused"))

    with pytest.raises(ProviderError, match="ConnectError") as caught:
        await make_http().request("GET", URL, endpoint="search")

    assert route.call_count == 2
    assert caught.value.call.status == "error"


@respx.mock
async def test_a_timeout_is_charged_at_the_estimate(make_http: MakeHttp, budget: Budget) -> None:
    respx.get(URL).mock(side_effect=httpx.ReadTimeout("too slow"))

    with pytest.raises(ProviderError) as caught:
        await make_http().request("GET", URL, endpoint="search")

    assert caught.value.call.status == "timeout"
    assert caught.value.call.cost_usd == pytest.approx(0.005)
    assert budget.spent_usd == pytest.approx(0.005)


@respx.mock
async def test_the_asyncio_timeout_also_counts_as_a_timeout(make_http: MakeHttp) -> None:
    async def slow(_request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(1)
        return httpx.Response(200)

    respx.get(URL).mock(side_effect=slow)

    with pytest.raises(ProviderError) as caught:
        await make_http(timeout_s=0.01, retries=0).request("GET", URL, endpoint="search")

    assert caught.value.call.status == "timeout"


@respx.mock
async def test_a_call_that_does_not_fit_the_budget_sends_nothing(budget: Budget) -> None:
    route = respx.get(URL).mock(return_value=httpx.Response(200))
    tiny = Budget(0.001)

    async with httpx.AsyncClient() as client:
        http = MeteredHttp("example", PRICE, client, tiny)
        with pytest.raises(BudgetExceededError):
            await http.request("GET", URL, endpoint="search")

    assert route.call_count == 0
    assert budget.spent_usd == 0


@respx.mock
async def test_credentials_in_an_error_body_are_hidden(make_http: MakeHttp) -> None:
    respx.get(URL).mock(return_value=httpx.Response(401, text='{"error": "bad api_key=sk-secret123 rejected"}'))

    with pytest.raises(ProviderError) as caught:
        await make_http().request("GET", URL, endpoint="search")

    assert "sk-secret123" not in str(caught.value)
    assert "sk-secret123" not in (caught.value.call.error or "")


@respx.mock
async def test_request_starts_are_spaced_by_the_minimum_interval(make_http: MakeHttp) -> None:
    respx.get(URL).mock(return_value=httpx.Response(200, json={}))
    http = make_http(min_interval_s=0.05)

    started = time.perf_counter()
    await asyncio.gather(*(http.request("GET", URL, endpoint="search") for _ in range(3)))

    assert time.perf_counter() - started >= 0.09


@respx.mock
async def test_concurrency_is_limited(make_http: MakeHttp) -> None:
    in_flight = 0
    peak = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.01)
        in_flight -= 1
        return httpx.Response(200, json={})

    respx.get(URL).mock(side_effect=handler)
    http = make_http(max_concurrency=2)

    await asyncio.gather(*(http.request("GET", URL, endpoint="search") for _ in range(6)))

    assert peak == 2


@respx.mock
async def test_a_cancelled_call_gives_its_reservation_back(make_http: MakeHttp, budget: Budget) -> None:
    async def hang(_request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(10)
        return httpx.Response(200)

    respx.get(URL).mock(side_effect=hang)
    task = asyncio.create_task(make_http().request("GET", URL, endpoint="search"))
    await asyncio.sleep(0.01)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert budget.reserved_usd == 0
    assert budget.spent_usd == 0
