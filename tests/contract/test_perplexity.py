"""Contract tests for the Perplexity Search adapter: request shape, parsing, cost and failures against fixtures."""

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
from tg_discovery.providers.search.perplexity import MAX_CONTENT_CHARS, PRICES, PerplexitySearch, SearchType

URL = "https://api.perplexity.ai/search"
TEXT = "gay bars in Bangkok"

_conforms: Callable[[str, MeteredHttp], SearchProvider] = PerplexitySearch


def _query(*, max_results: int = 10, freshness_days: int | None = None, country: str = "TH") -> SearchQuery:
    return SearchQuery(
        text=TEXT,
        flow=Flow.VENUE,
        country=country,
        city="Bangkok",
        max_results=max_results,
        freshness_days=freshness_days,
    )


def _adapter(
    make_http: HttpFactory, *, search_type: SearchType = "web", http_type: SearchType | None = None
) -> PerplexitySearch:
    """An adapter of ``search_type`` over an HTTP layer priced for ``http_type`` (the same unless said otherwise)."""
    http = make_http("perplexity_search", PRICES[http_type or search_type])
    return PerplexitySearch("pplx-test-key", http, search_type=search_type)


def _reply(name: str) -> httpx.Response:
    return httpx.Response(200, json=load_fixture("perplexity", name))


def _one_result(**fields: object) -> httpx.Response:
    return httpx.Response(200, json={"results": [{"url": "https://guide.example.com/a", **fields}], "id": "x"})


def _sent(route: respx.Route) -> dict[str, Any]:
    return json.loads(route.calls.last.request.content)


@pytest.fixture
def fixed_today(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("tg_discovery.providers.search.perplexity._utc_today", lambda: date(2026, 10, 8))


def test_the_adapter_describes_itself_to_the_registry() -> None:
    assert PerplexitySearch.name == "perplexity_search"
    assert PerplexitySearch.flows == {Flow.VENUE, Flow.EVENT}
    assert PerplexitySearch.google_derived is False
    assert PerplexitySearch.price is PRICES["web"]  # the default type
    assert PerplexitySearch.max_concurrency == 4
    assert PerplexitySearch.min_interval_s == 0  # 50 query units a second for every tier


@pytest.mark.parametrize(("search_type", "usd"), [("web", 0.005), ("fast", 0.001)])
def test_an_instance_carries_the_price_of_its_search_type(
    make_http: HttpFactory, search_type: SearchType, usd: float
) -> None:
    price = _adapter(make_http, search_type=search_type).price

    assert price is PRICES[search_type]
    assert (price.unit, price.usd_per_unit, price.verified_on) == ("request", usd, date(2026, 10, 8))
    assert price.source_url == "https://docs.perplexity.ai/docs/getting-started/pricing"


@respx.mock
async def test_the_request_carries_the_key_and_the_query(make_http: HttpFactory) -> None:
    route = respx.post(URL).mock(return_value=_reply("search_web.json"))

    result = await _adapter(make_http).search(_query())

    request = route.calls.last.request
    assert request.method == "POST"
    assert str(request.url) == URL
    assert request.headers["authorization"] == "Bearer pplx-test-key"
    assert request.headers["content-type"] == "application/json"
    assert _sent(route) == {
        "query": TEXT,
        "search_type": "web",
        "max_results": 10,
        "max_tokens_per_page": 2000,
        "max_tokens": 20000,
        "country": "TH",
    }
    assert result.requested == 10


@respx.mock
async def test_the_fast_search_type_is_sent_as_chosen(make_http: HttpFactory) -> None:
    route = respx.post(URL).mock(return_value=_reply("search_web.json"))

    await _adapter(make_http, search_type="fast").search(_query())

    assert _sent(route)["search_type"] == "fast"


@pytest.mark.parametrize(
    ("asked", "sent", "tokens"),
    [(1, 1, 2_000), (10, 10, 20_000), (20, 20, 40_000), (21, 20, 40_000), (100, 20, 40_000)],
)
@respx.mock
async def test_the_result_cap_is_the_vendors_limit_and_the_token_budget_follows_it(
    make_http: HttpFactory, asked: int, sent: int, tokens: int
) -> None:
    route = respx.post(URL).mock(return_value=_reply("search_web.json"))

    result = await _adapter(make_http).search(_query(max_results=asked))

    body = _sent(route)
    assert (body["max_results"], body["max_tokens"]) == (sent, tokens)
    assert result.requested == sent


@pytest.mark.parametrize(
    ("days", "cutoff"),
    [(1, "10/07/2026"), (14, "09/24/2026"), (30, "09/08/2026"), (270, "01/11/2026"), (365, "10/08/2025")],
)
@pytest.mark.usefixtures("fixed_today")
@respx.mock
async def test_freshness_days_become_an_exact_after_date_in_the_vendors_format(
    make_http: HttpFactory, days: int, cutoff: str
) -> None:
    route = respx.post(URL).mock(return_value=_reply("search_web.json"))

    await _adapter(make_http).search(_query(freshness_days=days))

    body = _sent(route)
    assert body["search_after_date_filter"] == cutoff
    assert "search_recency_filter" not in body


@respx.mock
async def test_without_freshness_no_date_filter_is_sent(make_http: HttpFactory) -> None:
    route = respx.post(URL).mock(return_value=_reply("search_web.json"))

    await _adapter(make_http).search(_query())

    body = _sent(route)
    assert {"search_after_date_filter", "search_recency_filter", "last_updated_after_filter"}.isdisjoint(body)


@respx.mock
async def test_the_language_is_not_sent_because_the_vendors_filter_is_hard(make_http: HttpFactory) -> None:
    route = respx.post(URL).mock(return_value=_reply("search_web.json"))

    await _adapter(make_http).search(_query())

    assert "search_language_filter" not in _sent(route)


@pytest.mark.parametrize("code", ["TH", "gb", "De"])
@respx.mock
async def test_the_country_is_sent_as_an_upper_case_iso_code(make_http: HttpFactory, code: str) -> None:
    route = respx.post(URL).mock(return_value=_reply("search_web.json"))

    await _adapter(make_http).search(_query(country=code))

    assert _sent(route)["country"] == code.upper()


@respx.mock
async def test_a_realistic_response_becomes_hits_in_the_vendors_order(make_http: HttpFactory) -> None:
    respx.post(URL).mock(return_value=_reply("search_web.json"))

    result = await _adapter(make_http).search(_query())

    assert result.provider == "perplexity_search"
    assert [hit.url for hit in result.hits] == [
        "https://guide.example.com/bangkok/gay-bars",  # tracking parameters removed
        "https://events.example.org/bangkok/pride-programme",
        "https://forum.example.net/threads/is-example-bar-open",
        "https://venue.example.com/about",
    ]  # the fifth result has no URL and is skipped
    first, second, third, fourth = result.hits
    assert first.title == "The best gay bars in Bangkok: a 2026 guide"
    assert first.content.startswith("Silom Soi 2 and Soi 4 are the centre of Bangkok's gay nightlife")
    assert first.published_at == datetime(2026, 9, 30, tzinfo=UTC)  # the publication date wins over last_updated
    assert second.published_at == datetime(2026, 10, 5, 11, 20, 47, tzinfo=UTC)  # no date, so last_updated
    assert (third.title, third.published_at) == ("Is Example Bar still open?", None)
    assert (fourth.title, fourth.content, fourth.published_at) == (
        None,
        "A small cocktail bar with a mixed crowd.",
        None,
    )


@pytest.mark.parametrize(
    ("published", "updated", "expected"),
    [
        ("2026-09-30", "2026-10-03", datetime(2026, 9, 30, tzinfo=UTC)),
        (None, "2026-10-03", datetime(2026, 10, 3, tzinfo=UTC)),
        ("not a date", "2026-10-03", datetime(2026, 10, 3, tzinfo=UTC)),
        ("", "2026-10-03T08:15:00Z", datetime(2026, 10, 3, 8, 15, tzinfo=UTC)),
        ("2026-09-30T10:15:00+02:00", None, datetime(2026, 9, 30, 8, 15, tzinfo=UTC)),
        ("2026-09-30T08:15:00", None, datetime(2026, 9, 30, 8, 15, tzinfo=UTC)),
        (None, None, None),
        ("", "", None),
        ("soon", "later", None),
    ],
)
@respx.mock
async def test_dates_are_read_as_utc_with_last_updated_as_the_fallback(
    make_http: HttpFactory, published: str | None, updated: str | None, expected: datetime | None
) -> None:
    respx.post(URL).mock(return_value=_one_result(date=published, last_updated=updated))

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
    respx.post(URL).mock(return_value=_one_result(snippet="a" * 20_000))

    [hit] = (await _adapter(make_http).search(_query())).hits

    assert hit.content == "a" * MAX_CONTENT_CHARS


@respx.mock
async def test_a_request_is_billed_at_the_price_of_its_type(make_http: HttpFactory, budget: Budget) -> None:
    respx.post(URL).mock(return_value=_reply("search_web.json"))

    result = await _adapter(make_http).search(_query())

    call = result.call
    assert (call.provider, call.endpoint, call.status, call.http_status, call.attempts) == (
        "perplexity_search",
        "search",
        "ok",
        200,
        1,
    )
    assert call.units == 1
    assert call.cost_usd == pytest.approx(0.005)
    assert budget.spent_by_provider() == pytest.approx({"perplexity_search": 0.005})
    assert budget.reserved_usd == 0


@respx.mock
async def test_a_fast_request_costs_a_fifth(make_http: HttpFactory, budget: Budget) -> None:
    respx.post(URL).mock(return_value=_reply("search_web.json"))

    result = await _adapter(make_http, search_type="fast").search(_query())

    assert result.call.cost_usd == pytest.approx(0.001)
    assert budget.spent_usd == pytest.approx(0.001)


@respx.mock
async def test_the_adapter_states_its_own_cost_whatever_price_its_http_layer_was_built_with(
    make_http: HttpFactory, budget: Budget
) -> None:
    respx.post(URL).mock(return_value=_reply("search_web.json"))

    result = await _adapter(make_http, search_type="fast", http_type="web").search(_query())

    assert result.call.cost_usd == pytest.approx(0.001)
    assert budget.spent_usd == pytest.approx(0.001)


@pytest.mark.parametrize("body", [{"results": []}, {}, {"results": None}])
@respx.mock
async def test_an_empty_result_is_still_billed(make_http: HttpFactory, budget: Budget, body: dict[str, Any]) -> None:
    respx.post(URL).mock(return_value=httpx.Response(200, json={**body, "id": "x"}))

    result = await _adapter(make_http).search(_query())

    assert result.hits == []
    assert result.call.cost_usd == pytest.approx(0.005)
    assert budget.spent_usd == pytest.approx(0.005)


@respx.mock
async def test_the_empty_fixture_has_no_hits(make_http: HttpFactory) -> None:
    respx.post(URL).mock(return_value=_reply("search_empty.json"))

    result = await _adapter(make_http).search(_query())

    assert result.hits == []
    assert result.call.units == 1


@pytest.mark.parametrize("status", [401, 403, 422])
@respx.mock
async def test_a_rejected_key_or_request_is_not_retried(make_http: HttpFactory, budget: Budget, status: int) -> None:
    route = respx.post(URL).mock(return_value=httpx.Response(status, json={"detail": "rejected"}))

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
async def test_a_rate_limit_followed_by_an_answer_is_billed_once(make_http: HttpFactory, budget: Budget) -> None:
    route = respx.post(URL).mock(side_effect=[httpx.Response(429), _reply("search_web.json")])

    result = await _adapter(make_http).search(_query())

    assert route.call_count == 2
    assert result.call.attempts == 2
    assert len(result.hits) == 4
    assert budget.spent_usd == pytest.approx(0.005)


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
    assert call.cost_usd == pytest.approx(0.005)  # the vendor answered, so the call stays billed
    assert budget.spent_usd == pytest.approx(0.005)


@respx.mock
async def test_a_call_that_does_not_fit_the_budget_sends_nothing(make_http: HttpFactory, budget: Budget) -> None:
    route = respx.post(URL).mock(return_value=_reply("search_web.json"))
    budget.reserve("elsewhere", budget.remaining_usd - 0.003)  # a web search needs 0.005

    with pytest.raises(BudgetExceededError):
        await _adapter(make_http).search(_query())

    assert route.call_count == 0
    assert budget.spent_usd == 0


@respx.mock
async def test_the_price_of_the_adapter_is_what_must_fit_whatever_its_http_layer_was_built_with(
    make_http: HttpFactory, budget: Budget
) -> None:
    route = respx.post(URL).mock(return_value=_reply("search_web.json"))
    budget.reserve("elsewhere", budget.remaining_usd - 0.003)  # the fast price fits, the web price does not

    with pytest.raises(BudgetExceededError):
        await _adapter(make_http, search_type="web", http_type="fast").search(_query())

    assert route.call_count == 0


@respx.mock
async def test_a_fast_search_fits_where_a_web_search_does_not(make_http: HttpFactory, budget: Budget) -> None:
    route = respx.post(URL).mock(return_value=_reply("search_web.json"))
    budget.reserve("elsewhere", budget.remaining_usd - 0.003)

    await _adapter(make_http, search_type="fast").search(_query())

    assert route.call_count == 1
