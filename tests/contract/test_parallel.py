"""Contract tests for the Parallel adapter: request shape, parsing, cost and failures against hand-written fixtures."""

import json
from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Any

import httpx
import pytest
import respx

from tests.support import HttpFactory, load_fixture
from tg_discovery.domain.enums import Flow
from tg_discovery.pipelines.common.budget import Budget
from tg_discovery.providers.base import BudgetExceededError, ProviderError, SearchProvider, SearchQuery
from tg_discovery.providers.metering import MeteredHttp
from tg_discovery.providers.search.parallel import MAX_CONTENT_CHARS, PRICES, Mode, ParallelSearch

URL = "https://api.parallel.ai/v1/search"
TEXT = "gay bars in Bangkok"

_conforms: Callable[[str, MeteredHttp], SearchProvider] = ParallelSearch


def _query(
    *, text: str = TEXT, max_results: int = 10, freshness_days: int | None = None, country: str = "TH"
) -> SearchQuery:
    return SearchQuery(
        text=text,
        flow=Flow.VENUE,
        country=country,
        city="Bangkok",
        max_results=max_results,
        freshness_days=freshness_days,
    )


def _adapter(make_http: HttpFactory, *, mode: Mode = "fast", http_mode: Mode | None = None) -> ParallelSearch:
    """An adapter in ``mode`` over an HTTP layer priced for ``http_mode`` (the same mode unless said otherwise)."""
    return ParallelSearch("parallel-test-key", make_http("parallel", PRICES[http_mode or mode]), mode=mode)


def _reply(name: str) -> httpx.Response:
    return httpx.Response(200, json=load_fixture("parallel", name))


def _page(count: int) -> httpx.Response:
    """A response with ``count`` results, for the cost of results beyond the ten a request covers."""
    results = [
        {"url": f"https://guide.example.com/page-{n}", "title": f"Page {n}", "excerpts": [f"Excerpt {n}"]}
        for n in range(count)
    ]
    return httpx.Response(200, json={"search_id": "search_x", "results": results, "session_id": "session_x"})


def _one_result(**fields: object) -> httpx.Response:
    return httpx.Response(200, json={"results": [{"url": "https://guide.example.com/a", **fields}]})


def _sent(route: respx.Route) -> dict[str, Any]:
    return json.loads(route.calls.last.request.content)


@pytest.fixture
def fixed_today(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("tg_discovery.providers.search.parallel._utc_today", lambda: date(2026, 10, 8))


def test_the_adapter_describes_itself_to_the_registry() -> None:
    assert ParallelSearch.name == "parallel"
    assert ParallelSearch.flows == {Flow.VENUE, Flow.EVENT}
    assert ParallelSearch.google_derived is False
    assert ParallelSearch.price is PRICES["fast"]  # the default mode
    assert ParallelSearch.max_concurrency == 4
    assert ParallelSearch.min_interval_s == 0  # 600 requests a minute are allowed


@pytest.mark.parametrize(("mode", "usd"), [("turbo", 0.001), ("fast", 0.001), ("basic", 0.005), ("advanced", 0.005)])
def test_an_instance_carries_the_price_of_its_mode(make_http: HttpFactory, mode: Mode, usd: float) -> None:
    price = _adapter(make_http, mode=mode).price

    assert price is PRICES[mode]
    assert (price.unit, price.usd_per_unit, price.verified_on) == ("request", usd, date(2026, 10, 8))
    assert price.source_url == "https://docs.parallel.ai/getting-started/pricing"


@respx.mock
async def test_the_request_carries_the_key_and_the_query(make_http: HttpFactory) -> None:
    route = respx.post(URL).mock(return_value=_reply("search_fast.json"))

    result = await _adapter(make_http).search(_query())

    request = route.calls.last.request
    assert request.method == "POST"
    assert str(request.url) == URL
    assert request.headers["x-api-key"] == "parallel-test-key"
    assert "authorization" not in request.headers
    assert request.headers["content-type"] == "application/json"
    assert _sent(route) == {
        "objective": TEXT,
        "search_queries": [TEXT],
        "mode": "fast",
        "advanced_settings": {
            "max_results": 10,
            "location": "th",
            "excerpt_settings": {"max_chars_per_result": MAX_CONTENT_CHARS},
        },
    }
    assert result.requested == 10


@pytest.mark.parametrize("mode", ["turbo", "fast", "basic", "advanced"])
@respx.mock
async def test_the_mode_is_sent_as_chosen(make_http: HttpFactory, mode: Mode) -> None:
    route = respx.post(URL).mock(return_value=_reply("search_fast.json"))

    await _adapter(make_http, mode=mode).search(_query())

    assert _sent(route)["mode"] == mode


@pytest.mark.parametrize(("asked", "sent"), [(1, 1), (10, 10), (20, 20), (21, 20), (100, 20)])
@respx.mock
async def test_the_result_cap_is_the_vendors_limit_and_is_reported(
    make_http: HttpFactory, asked: int, sent: int
) -> None:
    route = respx.post(URL).mock(return_value=_reply("search_fast.json"))

    result = await _adapter(make_http).search(_query(max_results=asked))

    assert _sent(route)["advanced_settings"]["max_results"] == sent
    assert result.requested == sent


@pytest.mark.parametrize(("days", "after_date"), [(1, "2026-10-07"), (14, "2026-09-24"), (365, "2025-10-08")])
@pytest.mark.usefixtures("fixed_today")
@respx.mock
async def test_freshness_days_become_an_exact_after_date(make_http: HttpFactory, days: int, after_date: str) -> None:
    route = respx.post(URL).mock(return_value=_reply("search_fast.json"))

    await _adapter(make_http).search(_query(freshness_days=days))

    assert _sent(route)["advanced_settings"]["source_policy"] == {"after_date": after_date}


@respx.mock
async def test_without_freshness_no_source_policy_is_sent(make_http: HttpFactory) -> None:
    route = respx.post(URL).mock(return_value=_reply("search_fast.json"))

    await _adapter(make_http).search(_query())

    assert "source_policy" not in _sent(route)["advanced_settings"]


@pytest.mark.parametrize(("code", "location"), [("TH", "th"), ("GB", "gb"), ("de", "de")])
@respx.mock
async def test_the_country_is_sent_as_a_lower_case_location(make_http: HttpFactory, code: str, location: str) -> None:
    route = respx.post(URL).mock(return_value=_reply("search_fast.json"))

    await _adapter(make_http).search(_query(country=code))

    assert _sent(route)["advanced_settings"]["location"] == location


@respx.mock
async def test_a_long_query_is_cut_to_the_documented_limits(make_http: HttpFactory) -> None:
    route = respx.post(URL).mock(return_value=_reply("search_fast.json"))

    await _adapter(make_http).search(_query(text="bar " * 100))

    body = _sent(route)
    assert body["search_queries"] == [("bar " * 100)[:200]]
    assert body["objective"] == "bar " * 100  # well under the 5,000 characters allowed


@respx.mock
async def test_a_realistic_response_becomes_hits_with_the_excerpts_joined(make_http: HttpFactory) -> None:
    respx.post(URL).mock(return_value=_reply("search_fast.json"))

    result = await _adapter(make_http).search(_query())

    assert result.provider == "parallel"
    assert [hit.url for hit in result.hits] == [
        "https://guide.example.com/bangkok/gay-bars",  # tracking parameters removed
        "https://events.example.org/bangkok/pride-programme",
        "https://forum.example.net/threads/bangkok-gay-scene",
        "https://venue.example.com/about",
    ]  # the fifth result has no URL and is skipped
    first, second, third, fourth = result.hits
    assert first.title == "The best gay bars in Bangkok: a 2026 guide"
    assert first.content == (
        "## Silom\n\nSilom Soi 2 and Soi 4 are the centre of Bangkok's gay nightlife, with a dozen bars and clubs a "
        "short walk apart.\n\n"
        "Example Bar opens at 21:00 and has a drag show every Friday. Most venues are free to enter before midnight."
    )
    assert first.published_at == datetime(2026, 9, 30, tzinfo=UTC)
    assert (second.title, second.published_at) == ("Bangkok Pride programme", None)
    assert second.content.startswith("The parade starts at 14:00 on Sunday")
    assert (third.title, third.content) == (None, "")  # no excerpts
    assert third.published_at == datetime(2026, 9, 15, tzinfo=UTC)
    assert (fourth.title, fourth.content, fourth.published_at) == (None, "", None)  # excerpts null


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2026-09-30", datetime(2026, 9, 30, tzinfo=UTC)),
        ("2026-09-30T08:15:00Z", datetime(2026, 9, 30, 8, 15, tzinfo=UTC)),
        ("2026-09-30T10:15:00+02:00", datetime(2026, 9, 30, 8, 15, tzinfo=UTC)),
        ("2026-09-30T08:15:00", datetime(2026, 9, 30, 8, 15, tzinfo=UTC)),
        ("30 September", None),
        ("", None),
        (None, None),
    ],
)
@respx.mock
async def test_publish_dates_are_read_as_utc(
    make_http: HttpFactory, raw: str | None, expected: datetime | None
) -> None:
    respx.post(URL).mock(return_value=_one_result(publish_date=raw))

    [hit] = (await _adapter(make_http).search(_query())).hits

    assert hit.published_at == expected
    assert hit.published_at is None or hit.published_at.tzinfo == UTC


@respx.mock
async def test_a_result_with_only_a_url_is_kept_with_empty_fields(make_http: HttpFactory) -> None:
    respx.post(URL).mock(return_value=_one_result())

    [hit] = (await _adapter(make_http).search(_query())).hits

    assert (hit.url, hit.title, hit.content, hit.published_at) == ("https://guide.example.com/a", None, "", None)


@respx.mock
async def test_content_is_capped(make_http: HttpFactory) -> None:
    respx.post(URL).mock(return_value=_one_result(excerpts=["a" * 6_000, "b" * 6_000]))

    [hit] = (await _adapter(make_http).search(_query())).hits

    assert len(hit.content) == MAX_CONTENT_CHARS
    assert hit.content.startswith("a" * 6_000 + "\n\nbbb")


@respx.mock
async def test_a_request_is_billed_at_the_price_of_its_mode(make_http: HttpFactory, budget: Budget) -> None:
    respx.post(URL).mock(return_value=_reply("search_fast.json"))

    result = await _adapter(make_http).search(_query())

    call = result.call
    assert (call.provider, call.endpoint, call.status, call.http_status, call.attempts) == (
        "parallel",
        "search",
        "ok",
        200,
        1,
    )
    assert call.units == 1
    assert call.cost_usd == pytest.approx(0.001)
    assert budget.spent_by_provider() == pytest.approx({"parallel": 0.001})
    assert budget.reserved_usd == 0


@pytest.mark.parametrize(("mode", "usd"), [("turbo", 0.001), ("fast", 0.001), ("basic", 0.005), ("advanced", 0.005)])
@respx.mock
async def test_every_mode_is_billed_at_its_own_price(make_http: HttpFactory, mode: Mode, usd: float) -> None:
    respx.post(URL).mock(return_value=_page(10))

    result = await _adapter(make_http, mode=mode).search(_query())

    assert result.call.cost_usd == pytest.approx(usd)


@pytest.mark.parametrize(
    ("mode", "returned", "usd"),
    [("fast", 10, 0.001), ("fast", 12, 0.003), ("fast", 20, 0.011), ("advanced", 12, 0.007), ("advanced", 20, 0.015)],
)
@respx.mock
async def test_results_beyond_the_ten_a_request_covers_cost_a_thousandth_each(
    make_http: HttpFactory, budget: Budget, mode: Mode, returned: int, usd: float
) -> None:
    respx.post(URL).mock(return_value=_page(returned))

    result = await _adapter(make_http, mode=mode).search(_query(max_results=20))

    assert len(result.hits) == returned
    assert result.call.units == 1  # one request; the extra results only add dollars
    assert result.call.cost_usd == pytest.approx(usd)
    assert budget.spent_usd == pytest.approx(usd)


@respx.mock
async def test_fewer_results_than_asked_for_are_billed_for_what_came_back(make_http: HttpFactory) -> None:
    respx.post(URL).mock(return_value=_page(4))

    result = await _adapter(make_http).search(_query(max_results=20))

    assert result.call.cost_usd == pytest.approx(0.001)


@respx.mock
async def test_a_body_without_a_result_list_is_billed_at_the_worst_case(make_http: HttpFactory) -> None:
    respx.post(URL).mock(return_value=httpx.Response(200, json={"search_id": "search_x"}))

    result = await _adapter(make_http).search(_query(max_results=20))

    assert result.hits == []
    assert result.call.cost_usd == pytest.approx(0.011)


@respx.mock
async def test_the_adapter_states_its_own_cost_whatever_price_its_http_layer_was_built_with(
    make_http: HttpFactory, budget: Budget
) -> None:
    respx.post(URL).mock(return_value=_page(10))

    result = await _adapter(make_http, mode="fast", http_mode="advanced").search(_query())

    assert result.call.cost_usd == pytest.approx(0.001)
    assert budget.spent_usd == pytest.approx(0.001)


@respx.mock
async def test_an_empty_result_is_still_billed(make_http: HttpFactory, budget: Budget) -> None:
    respx.post(URL).mock(return_value=_reply("search_empty.json"))

    result = await _adapter(make_http).search(_query())

    assert result.hits == []
    assert result.call.cost_usd == pytest.approx(0.001)
    assert budget.spent_usd == pytest.approx(0.001)


@pytest.mark.parametrize("status", [401, 403, 422])
@respx.mock
async def test_a_rejected_key_or_request_is_not_retried(make_http: HttpFactory, budget: Budget, status: int) -> None:
    route = respx.post(URL).mock(
        return_value=httpx.Response(status, json={"type": "error", "error": {"ref_id": "x", "message": "no"}})
    )

    with pytest.raises(ProviderError, match=f"HTTP {status}") as caught:
        await _adapter(make_http).search(_query())

    assert route.call_count == 1
    call = caught.value.call
    assert (call.status, call.http_status, call.attempts, call.cost_usd) == ("error", status, 1, 0)
    assert budget.spent_usd == 0
    assert budget.reserved_usd == 0


@pytest.mark.parametrize("status", [429, 500, 503])
@respx.mock
async def test_rate_limits_and_server_errors_are_retried_once_then_reported(
    make_http: HttpFactory, budget: Budget, status: int
) -> None:
    route = respx.post(URL).mock(return_value=httpx.Response(status, text="try again later"))

    with pytest.raises(ProviderError, match=f"HTTP {status}") as caught:
        await _adapter(make_http).search(_query())

    assert route.call_count == 2
    call = caught.value.call
    assert (call.status, call.http_status, call.attempts, call.cost_usd) == ("error", status, 2, 0)
    assert budget.spent_usd == 0


@respx.mock
async def test_a_server_error_followed_by_an_answer_is_billed_once(make_http: HttpFactory, budget: Budget) -> None:
    route = respx.post(URL).mock(side_effect=[httpx.Response(503), _reply("search_fast.json")])

    result = await _adapter(make_http).search(_query())

    assert route.call_count == 2
    assert result.call.attempts == 2
    assert len(result.hits) == 4
    assert budget.spent_usd == pytest.approx(0.001)


@pytest.mark.parametrize("content", ["<html>down for maintenance</html>", '{"results": "oops"}', "[]"])
@respx.mock
async def test_a_body_that_is_not_the_expected_json_is_a_provider_error(
    make_http: HttpFactory, budget: Budget, content: str
) -> None:
    respx.post(URL).mock(return_value=httpx.Response(200, text=content))

    with pytest.raises(ProviderError, match="not the expected JSON") as caught:
        await _adapter(make_http).search(_query())

    call = caught.value.call
    assert (call.status, call.http_status) == ("error", 200)
    assert call.cost_usd == pytest.approx(0.001)  # the vendor answered, so the call stays billed
    assert budget.spent_usd == pytest.approx(0.001)


@respx.mock
async def test_a_call_that_does_not_fit_the_budget_sends_nothing(make_http: HttpFactory, budget: Budget) -> None:
    route = respx.post(URL).mock(return_value=_reply("search_fast.json"))
    budget.reserve("elsewhere", budget.remaining_usd - 0.0005)  # a fast search needs 0.001

    with pytest.raises(BudgetExceededError):
        await _adapter(make_http).search(_query())

    assert route.call_count == 0
    assert budget.spent_usd == 0


@respx.mock
async def test_the_worst_case_with_extra_results_is_what_must_fit(make_http: HttpFactory, budget: Budget) -> None:
    route = respx.post(URL).mock(return_value=_page(10))
    budget.reserve("elsewhere", budget.remaining_usd - 0.005)  # the base price fits, 20 results at 0.011 do not

    with pytest.raises(BudgetExceededError):
        await _adapter(make_http).search(_query(max_results=20))
    await _adapter(make_http).search(_query(max_results=10))

    assert route.call_count == 1
