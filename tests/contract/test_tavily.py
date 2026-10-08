"""Contract tests for the Tavily adapter: request shape, parsing, cost and failures against hand-written fixtures."""

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
from tg_discovery.providers.search.tavily import MAX_CONTENT_CHARS, PRICE, SearchDepth, TavilySearch, Topic

URL = "https://api.tavily.com/search"

_conforms: Callable[[str, MeteredHttp], SearchProvider] = TavilySearch


def _query(
    *, max_results: int = 10, freshness_days: int | None = None, country: str = "TH", language: str = "en"
) -> SearchQuery:
    return SearchQuery(
        text="gay bars in Bangkok",
        flow=Flow.VENUE,
        country=country,
        city="Bangkok",
        language=language,
        max_results=max_results,
        freshness_days=freshness_days,
    )


def _adapter(
    make_http: HttpFactory,
    *,
    search_depth: SearchDepth = "basic",
    topic: Topic = "general",
    include_raw_content: bool = False,
) -> TavilySearch:
    return TavilySearch(
        "tvly-test-key",
        make_http("tavily", PRICE),
        search_depth=search_depth,
        topic=topic,
        include_raw_content=include_raw_content,
    )


def _reply(name: str) -> httpx.Response:
    return httpx.Response(200, json=load_fixture("tavily", name))


def _sent(route: respx.Route) -> dict[str, Any]:
    return json.loads(route.calls.last.request.content)


def _one_result(**fields: object) -> httpx.Response:
    result = {"url": "https://guide.example.com/a", **fields}
    return httpx.Response(200, json={"results": [result], "usage": {"credits": 1}})


@pytest.fixture
def fixed_today(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("tg_discovery.providers.search.tavily._utc_today", lambda: date(2026, 10, 8))


def test_the_adapter_describes_itself_to_the_registry() -> None:
    assert TavilySearch.name == "tavily"
    assert TavilySearch.flows == {Flow.VENUE, Flow.EVENT}
    assert TavilySearch.google_derived is False
    assert TavilySearch.price is PRICE
    assert TavilySearch.max_concurrency == 4
    assert 60 / TavilySearch.min_interval_s <= 100  # a development key allows 100 requests a minute


def test_the_price_is_the_pay_as_you_go_credit_price() -> None:
    assert (PRICE.unit, PRICE.usd_per_unit, PRICE.verified_on) == ("credit", 0.008, date(2026, 10, 8))
    assert PRICE.source_url == "https://docs.tavily.com/documentation/api-credits"


@respx.mock
async def test_the_request_carries_the_key_and_the_query(make_http: HttpFactory) -> None:
    route = respx.post(URL).mock(return_value=_reply("search_basic.json"))

    result = await _adapter(make_http).search(_query())

    request = route.calls.last.request
    assert request.method == "POST"
    assert str(request.url) == URL
    assert request.headers["authorization"] == "Bearer tvly-test-key"
    assert request.headers["content-type"] == "application/json"
    assert _sent(route) == {
        "query": "gay bars in Bangkok",
        "search_depth": "basic",
        "topic": "general",
        "max_results": 10,
        "language": "en",
        "country": "thailand",
        "include_published_date": True,
        "include_usage": True,
    }
    assert result.requested == 10


@pytest.mark.parametrize(("asked", "sent"), [(1, 1), (10, 10), (20, 20), (21, 20), (100, 20)])
@respx.mock
async def test_the_result_cap_is_the_vendors_limit_and_is_reported(
    make_http: HttpFactory, asked: int, sent: int
) -> None:
    route = respx.post(URL).mock(return_value=_reply("search_basic.json"))

    result = await _adapter(make_http).search(_query(max_results=asked))

    assert _sent(route)["max_results"] == sent
    assert result.requested == sent


@pytest.mark.parametrize(("days", "start_date"), [(1, "2026-10-07"), (14, "2026-09-24"), (365, "2025-10-08")])
@pytest.mark.usefixtures("fixed_today")
@respx.mock
async def test_freshness_days_become_an_exact_start_date(make_http: HttpFactory, days: int, start_date: str) -> None:
    route = respx.post(URL).mock(return_value=_reply("search_basic.json"))

    await _adapter(make_http).search(_query(freshness_days=days))

    body = _sent(route)
    assert body["start_date"] == start_date
    assert "time_range" not in body


@respx.mock
async def test_without_freshness_no_date_filter_is_sent(make_http: HttpFactory) -> None:
    route = respx.post(URL).mock(return_value=_reply("search_basic.json"))

    await _adapter(make_http).search(_query())

    assert {"start_date", "end_date", "time_range"}.isdisjoint(_sent(route))


@respx.mock
async def test_the_news_topic_is_sent_without_a_country(make_http: HttpFactory) -> None:
    route = respx.post(URL).mock(return_value=_reply("search_basic.json"))

    await _adapter(make_http, topic="news").search(_query())

    body = _sent(route)
    assert body["topic"] == "news"
    assert "country" not in body


@pytest.mark.parametrize(
    ("code", "name"), [("TH", "thailand"), ("US", "united states"), ("GB", "united kingdom"), ("CZ", "czech republic")]
)
@respx.mock
async def test_the_country_is_sent_by_the_name_tavily_accepts(make_http: HttpFactory, code: str, name: str) -> None:
    route = respx.post(URL).mock(return_value=_reply("search_basic.json"))

    await _adapter(make_http).search(_query(country=code))

    assert _sent(route)["country"] == name


@pytest.mark.parametrize("code", ["XK", "HK", "CD", "ZZ"])
@respx.mock
async def test_a_country_outside_the_vendors_list_gets_no_bias(make_http: HttpFactory, code: str) -> None:
    route = respx.post(URL).mock(return_value=_reply("search_basic.json"))

    await _adapter(make_http).search(_query(country=code))

    assert "country" not in _sent(route)


@pytest.mark.parametrize(("tag", "code"), [("en", "en"), ("th", "th"), ("pt-BR", "pt"), ("zh_CN", "zh"), ("DE", "de")])
@respx.mock
async def test_language_tags_are_reduced_to_a_language_code(make_http: HttpFactory, tag: str, code: str) -> None:
    route = respx.post(URL).mock(return_value=_reply("search_basic.json"))

    await _adapter(make_http).search(_query(language=tag))

    body = _sent(route)
    assert body["language"] == code
    assert "filter_by_language" not in body  # a bias, not a filter


@respx.mock
async def test_raw_content_is_not_requested_by_default(make_http: HttpFactory) -> None:
    route = respx.post(URL).mock(return_value=_reply("search_basic.json"))

    await _adapter(make_http).search(_query())

    assert "include_raw_content" not in _sent(route)


@respx.mock
async def test_raw_content_is_requested_as_markdown_and_follows_the_chunks(make_http: HttpFactory) -> None:
    route = respx.post(URL).mock(return_value=_reply("search_raw_content.json"))

    result = await _adapter(make_http, include_raw_content=True).search(_query())

    assert _sent(route)["include_raw_content"] == "markdown"
    [hit] = result.hits
    chunks, _, page = hit.content.partition("\n\n# ")
    assert chunks == (
        "A drag brunch runs every Sunday from 12:00. [...] "
        "The community centre opens its doors for a film night on the 18th."
    )
    assert page.startswith("Queer events in Lisbon\n\nA drag brunch runs every Sunday from 12:00 at Example Terrace.")
    assert page.endswith("Write to events@example.org.")


@respx.mock
async def test_a_realistic_response_becomes_hits_in_the_vendors_order(make_http: HttpFactory) -> None:
    respx.post(URL).mock(return_value=_reply("search_basic.json"))

    result = await _adapter(make_http).search(_query())

    assert result.provider == "tavily"
    assert [hit.url for hit in result.hits] == [
        "https://guide.example.com/bangkok/gay-bars",  # tracking parameters removed
        "https://events.example.org/bangkok/pride-programme",
        "https://forum.example.net/threads/bangkok-gay-scene",
        "https://venue.example.com/about",
    ]  # the fifth result has no URL and is skipped
    first, second, third, fourth = result.hits
    assert first.title == "The best gay bars in Bangkok: a 2026 guide"
    assert first.content.startswith("Silom Soi 2 and Soi 4 are the centre of Bangkok's gay nightlife")
    assert first.content.count("[...]") == 2
    assert first.published_at == datetime(2026, 10, 2, 8, 15, tzinfo=UTC)
    assert (second.title, second.published_at) == ("Bangkok Pride programme", None)
    assert (third.title, third.content) == (None, "")  # empty strings count as missing
    assert third.published_at == datetime(2026, 9, 15, 21, 40, 12, tzinfo=UTC)
    assert (fourth.title, fourth.content, fourth.published_at) == (
        None,
        "A small cocktail bar with a mixed crowd.",
        None,
    )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Tue, 11 Mar 2025 17:00:00 GMT", datetime(2025, 3, 11, 17, 0, tzinfo=UTC)),
        ("Tue, 11 Mar 2025 19:00:00 +0200", datetime(2025, 3, 11, 17, 0, tzinfo=UTC)),
        ("2025-03-11T17:00:00Z", datetime(2025, 3, 11, 17, 0, tzinfo=UTC)),
        ("2025-03-11T19:00:00+02:00", datetime(2025, 3, 11, 17, 0, tzinfo=UTC)),
        ("2025-03-11T17:00:00", datetime(2025, 3, 11, 17, 0, tzinfo=UTC)),
        ("2025-03-11", datetime(2025, 3, 11, tzinfo=UTC)),
        ("last Tuesday", None),
        ("", None),
        (None, None),
    ],
)
@respx.mock
async def test_published_dates_are_read_as_utc(
    make_http: HttpFactory, raw: str | None, expected: datetime | None
) -> None:
    respx.post(URL).mock(return_value=_one_result(published_date=raw))

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
    respx.post(URL).mock(return_value=_one_result(content="a" * 20_000))

    [hit] = (await _adapter(make_http).search(_query())).hits

    assert hit.content == "a" * MAX_CONTENT_CHARS


@respx.mock
async def test_the_cap_cuts_the_raw_content_and_keeps_the_chunks(make_http: HttpFactory) -> None:
    respx.post(URL).mock(return_value=_one_result(content="chunk one [...] chunk two", raw_content="b" * 20_000))

    [hit] = (await _adapter(make_http, include_raw_content=True).search(_query())).hits

    assert len(hit.content) == MAX_CONTENT_CHARS
    assert hit.content.startswith("chunk one [...] chunk two\n\nbbb")


@respx.mock
async def test_the_credits_the_response_reports_are_billed(make_http: HttpFactory, budget: Budget) -> None:
    respx.post(URL).mock(return_value=_reply("search_basic.json"))

    result = await _adapter(make_http).search(_query())

    call = result.call
    assert (call.provider, call.endpoint, call.status, call.http_status, call.attempts) == (
        "tavily",
        "search",
        "ok",
        200,
        1,
    )
    assert call.units == 1
    assert call.cost_usd == pytest.approx(0.008)
    assert budget.spent_by_provider() == pytest.approx({"tavily": 0.008})
    assert budget.reserved_usd == 0


@respx.mock
async def test_an_advanced_search_is_sent_as_such_and_costs_two_credits(make_http: HttpFactory, budget: Budget) -> None:
    route = respx.post(URL).mock(return_value=_reply("search_advanced.json"))

    result = await _adapter(make_http, search_depth="advanced").search(_query())

    assert _sent(route)["search_depth"] == "advanced"
    assert result.call.units == 2
    assert result.call.cost_usd == pytest.approx(0.016)
    assert budget.spent_usd == pytest.approx(0.016)


@pytest.mark.parametrize(("depth", "used"), [("basic", 1), ("fast", 1), ("ultra-fast", 1), ("advanced", 2)])
@respx.mock
async def test_a_response_without_usage_is_billed_at_the_credits_of_the_depth(
    make_http: HttpFactory, depth: SearchDepth, used: int
) -> None:
    respx.post(URL).mock(return_value=httpx.Response(200, json={"results": []}))

    result = await _adapter(make_http, search_depth=depth).search(_query())

    assert result.call.units == used
    assert result.call.cost_usd == pytest.approx(used * 0.008)


@pytest.mark.parametrize("body", [{"results": []}, {}, {"results": None}])
@respx.mock
async def test_an_empty_result_is_still_billed(make_http: HttpFactory, budget: Budget, body: dict[str, Any]) -> None:
    respx.post(URL).mock(return_value=httpx.Response(200, json={**body, "usage": {"credits": 1}}))

    result = await _adapter(make_http).search(_query())

    assert result.hits == []
    assert result.call.cost_usd == pytest.approx(0.008)
    assert budget.spent_usd == pytest.approx(0.008)


@respx.mock
async def test_the_empty_fixture_has_no_hits(make_http: HttpFactory) -> None:
    respx.post(URL).mock(return_value=_reply("search_empty.json"))

    result = await _adapter(make_http).search(_query())

    assert result.hits == []
    assert result.call.units == 1


@pytest.mark.parametrize("status", [401, 403, 432, 433])
@respx.mock
async def test_a_rejected_key_or_an_exhausted_plan_is_not_retried(
    make_http: HttpFactory, budget: Budget, status: int
) -> None:
    route = respx.post(URL).mock(return_value=httpx.Response(status, json={"detail": {"error": "not allowed"}}))

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
    route = respx.post(URL).mock(side_effect=[httpx.Response(503), _reply("search_basic.json")])

    result = await _adapter(make_http).search(_query())

    assert route.call_count == 2
    assert result.call.attempts == 2
    assert len(result.hits) == 4
    assert budget.spent_usd == pytest.approx(0.008)


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
    assert call.cost_usd == pytest.approx(0.008)  # the vendor answered, so the call stays billed
    assert budget.spent_usd == pytest.approx(0.008)


@respx.mock
async def test_a_call_that_does_not_fit_the_budget_sends_nothing(make_http: HttpFactory, budget: Budget) -> None:
    route = respx.post(URL).mock(return_value=_reply("search_basic.json"))
    budget.reserve("elsewhere", budget.remaining_usd - 0.005)  # a basic search needs 0.008

    with pytest.raises(BudgetExceededError):
        await _adapter(make_http).search(_query())

    assert route.call_count == 0
    assert budget.spent_usd == 0


@respx.mock
async def test_the_worst_case_of_the_depth_is_what_must_fit(make_http: HttpFactory, budget: Budget) -> None:
    route = respx.post(URL).mock(return_value=_reply("search_advanced.json"))
    budget.reserve("elsewhere", budget.remaining_usd - 0.012)  # one credit fits, two do not

    with pytest.raises(BudgetExceededError):
        await _adapter(make_http, search_depth="advanced").search(_query())

    assert route.call_count == 0


@respx.mock
async def test_a_basic_search_fits_where_an_advanced_one_does_not(make_http: HttpFactory, budget: Budget) -> None:
    route = respx.post(URL).mock(return_value=_reply("search_basic.json"))
    budget.reserve("elsewhere", budget.remaining_usd - 0.012)

    await _adapter(make_http).search(_query())

    assert route.call_count == 1
