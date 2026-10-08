from collections.abc import Callable
from dataclasses import dataclass
from datetime import date

import httpx
import pytest
import respx

from tests.support import HttpFactory, load_fixture
from tg_discovery.domain.enums import Flow
from tg_discovery.pipelines.common.budget import Budget
from tg_discovery.providers.base import BudgetExceededError, PriceSpec, ProviderError, SearchProvider, SearchQuery
from tg_discovery.providers.metering import MeteredHttp
from tg_discovery.providers.search.brave import (
    LLM_CONTEXT_PRICE,
    LLM_CONTEXT_URL,
    MAX_CONTENT_CHARS,
    WEB_PRICE,
    WEB_URL,
    BraveLlmContext,
    BraveWebSearch,
)

QUERY = SearchQuery(text="gay bars Berlin", flow=Flow.VENUE, country="DE", city="Berlin", language="de", max_results=10)
REQUEST_USD = 0.005


@dataclass(frozen=True)
class Endpoint:
    """One Brave endpoint with the fixtures that go with it."""

    name: str
    url: str
    price: PriceSpec
    build: Callable[[str, MeteredHttp], SearchProvider]
    results: str
    empty: str


WEB = Endpoint("brave_web", WEB_URL, WEB_PRICE, BraveWebSearch, "web_search.json", "web_search_empty.json")
CONTEXT = Endpoint(
    "brave_llm_context",
    LLM_CONTEXT_URL,
    LLM_CONTEXT_PRICE,
    BraveLlmContext,
    "llm_context.json",
    "llm_context_empty.json",
)
BOTH = [pytest.param(WEB, id="web"), pytest.param(CONTEXT, id="llm_context")]
Payload = dict[str, object]


def _provider(endpoint: Endpoint, make_http: HttpFactory) -> SearchProvider:
    return endpoint.build("key-123", make_http(endpoint.name, endpoint.price))


def _ok(endpoint: Endpoint, fixture: str | None = None) -> httpx.Response:
    return httpx.Response(200, json=load_fixture("brave", fixture or endpoint.results))


def _params(route: respx.Route) -> dict[str, str]:
    return dict(route.calls.last.request.url.params)


@pytest.mark.parametrize("endpoint", BOTH)
@respx.mock
async def test_the_request_is_a_get_with_the_subscription_token(endpoint: Endpoint, make_http: HttpFactory) -> None:
    route = respx.get(endpoint.url).mock(return_value=_ok(endpoint))

    await _provider(endpoint, make_http).search(QUERY)

    request = route.calls.last.request
    assert request.method == "GET"
    assert request.headers["x-subscription-token"] == "key-123"
    assert request.headers["accept"] == "application/json"
    assert request.url.params["q"] == "gay bars Berlin"


@respx.mock
async def test_the_web_search_asks_for_plain_text_with_extra_snippets(make_http: HttpFactory) -> None:
    route = respx.get(WEB_URL).mock(return_value=_ok(WEB))

    await _provider(WEB, make_http).search(QUERY)

    assert _params(route) == {
        "q": "gay bars Berlin",
        "count": "10",
        "extra_snippets": "true",
        "text_decorations": "false",
        "country": "DE",
        "search_lang": "de",
    }


@respx.mock
async def test_the_llm_context_request_sizes_the_context_for_one_hit_per_url(make_http: HttpFactory) -> None:
    route = respx.get(LLM_CONTEXT_URL).mock(return_value=_ok(CONTEXT))

    await _provider(CONTEXT, make_http).search(QUERY)

    assert _params(route) == {
        "q": "gay bars Berlin",
        "count": "10",
        "maximum_number_of_urls": "10",
        "maximum_number_of_tokens": "32768",
        "maximum_number_of_tokens_per_url": "2048",
        "country": "DE",
        "search_lang": "de",
    }


@pytest.mark.parametrize("endpoint", BOTH)
@pytest.mark.parametrize(
    ("days", "window"),
    [(1, "pd"), (2, "pw"), (7, "pw"), (8, "pm"), (31, "pm"), (32, "py"), (365, "py"), (366, None)],
)
@respx.mock
async def test_freshness_days_round_up_to_the_next_brave_window(
    endpoint: Endpoint, days: int, window: str | None, make_http: HttpFactory
) -> None:
    route = respx.get(endpoint.url).mock(return_value=_ok(endpoint))

    await _provider(endpoint, make_http).search(QUERY.model_copy(update={"freshness_days": days}))

    assert _params(route).get("freshness") == window


@pytest.mark.parametrize("endpoint", BOTH)
@respx.mock
async def test_no_freshness_filter_is_sent_when_none_was_asked_for(endpoint: Endpoint, make_http: HttpFactory) -> None:
    route = respx.get(endpoint.url).mock(return_value=_ok(endpoint))

    await _provider(endpoint, make_http).search(QUERY)

    assert "freshness" not in _params(route)


@pytest.mark.parametrize("endpoint", BOTH)
@pytest.mark.parametrize(
    ("country", "language", "sent_country", "sent_language"),
    [
        ("DE", "de", "DE", "de"),
        ("TH", "th", "ALL", "th"),  # Brave has no Thailand: searched worldwide, the language is still supported
        ("ID", "id", "ID", None),  # Indonesian is not in Brave's language list
        ("BR", "pt", "BR", "pt-br"),
        ("PT", "pt", "PT", "pt-pt"),
        ("TW", "zh", "TW", "zh-hant"),
        ("CN", "zh", "CN", "zh-hans"),
        ("NO", "no", "NO", "nb"),
        ("JP", "ja", "JP", "ja"),
        ("GB", "en-GB", "GB", "en-gb"),
        ("us", "EN", "US", "en"),
    ],
)
@respx.mock
async def test_country_and_language_are_sent_only_in_values_brave_accepts(
    endpoint: Endpoint,
    country: str,
    language: str,
    sent_country: str,
    sent_language: str | None,
    make_http: HttpFactory,
) -> None:
    route = respx.get(endpoint.url).mock(return_value=_ok(endpoint))

    await _provider(endpoint, make_http).search(QUERY.model_copy(update={"country": country, "language": language}))

    params = _params(route)
    assert params["country"] == sent_country
    assert params.get("search_lang") == sent_language


@pytest.mark.parametrize(
    ("endpoint", "asked", "sent"),
    [(WEB, 5, 5), (WEB, 20, 20), (WEB, 50, 20), (CONTEXT, 20, 20), (CONTEXT, 50, 50), (CONTEXT, 100, 50)],
)
@respx.mock
async def test_the_result_cap_is_limited_to_what_brave_allows_and_reported(
    endpoint: Endpoint, asked: int, sent: int, make_http: HttpFactory
) -> None:
    route = respx.get(endpoint.url).mock(return_value=_ok(endpoint))

    result = await _provider(endpoint, make_http).search(QUERY.model_copy(update={"max_results": asked}))

    assert _params(route)["count"] == str(sent)
    assert result.requested == sent


@respx.mock
async def test_web_results_become_hits_with_snippets_and_utc_dates(make_http: HttpFactory) -> None:
    respx.get(WEB_URL).mock(return_value=_ok(WEB))

    result = await _provider(WEB, make_http).search(QUERY)

    assert result.provider == "brave_web"
    assert [
        (hit.url, hit.title, hit.content, hit.published_at.isoformat() if hit.published_at else None)
        for hit in result.hits
    ] == [
        (
            "https://www.berlin-queer-guide.example/bars?ref=home",
            "Gay Bars in Berlin: The Complete 2026 Guide",
            "Our guide to the best gay and queer bars in Berlin, from Schöneberg to Neukölln, with opening hours and "
            "addresses.\n\n"
            "Opening hours: Mon-Thu 20:00-03:00, Fri-Sun open end. Address: Motzstraße 12, 10777 Berlin.\n\n"
            "Every Saturday: Rainbow Night with two floors and a cabaret show at 23:00.",
            "2026-09-20T08:15:00+00:00",
        ),
        (
            "https://nightlife-wiki.example/Berlin_queer_nightlife",
            "Queer nightlife in Berlin",
            "An overview of the queer bars, clubs and saunas of Berlin and how the scene developed after 1990.",
            None,  # no page_age: the human-readable "2 years ago" is not turned into a date
        ),
        ("https://events.berlin-pride.example/calendar", None, "", "2026-10-01T00:00:00+00:00"),
        (
            "https://rainbow-corner.example",
            "Rainbow Corner Bar - Neukölln",
            "Cocktails, DJs and drag shows in the heart of Neukölln.",
            None,  # "3 weeks ago" is not an ISO date
        ),
        (
            "https://cafe-list.example/berlin/gay-friendly",
            "Gay-friendly cafes in Berlin",
            "Ten cafes where everyone is welcome.",  # the extra snippet repeats the description
            "2026-09-30T18:45:10+00:00",  # sent as +02:00
        ),
    ]


@respx.mock
async def test_llm_context_chunks_become_hits_with_the_page_date_from_the_sources(make_http: HttpFactory) -> None:
    respx.get(LLM_CONTEXT_URL).mock(return_value=_ok(CONTEXT))

    result = await _provider(CONTEXT, make_http).search(QUERY)

    assert result.provider == "brave_llm_context"
    assert [
        (hit.url, hit.title, hit.content, hit.published_at.isoformat() if hit.published_at else None)
        for hit in result.hits
    ] == [
        (
            "https://www.berlin-queer-guide.example/bars",
            "Gay Bars in Berlin: The Complete 2026 Guide",
            "Gay Bars in Berlin: The Complete 2026 Guide\n"
            "Berlin has one of the largest queer scenes in Europe. Most bars cluster around Motzstraße in Schöneberg "
            "and in Neukölln.\n\n"
            "Rainbow Corner Bar - Motzstraße 12, 10777 Berlin. Open Mon-Thu 20:00-03:00, Fri-Sun until late. Drag show "
            "every Saturday at 23:00.\n\n"
            # Structured data inside a snippet is kept verbatim for the extractor.
            '{"@type": "Event", "name": "Rainbow Night", "startDate": "2026-11-14T21:00", '
            '"location": {"name": "Rainbow Corner Bar"}}',
            "2026-09-20T08:15:00+00:00",  # the ISO timestamp in the fourth position wins
        ),
        (
            "https://nightlife-wiki.example/Berlin_queer_nightlife",
            "Queer nightlife in Berlin",  # the grounding entry has no title: the sources entry supplies it
            "Berlin queer nightlife developed rapidly after 1990. Today the scene includes bars, clubs, saunas and "
            "regular parties.",
            None,  # age is an empty list
        ),
        (
            "https://events.berlin-pride.example/calendar",
            "Pride calendar",
            "",
            "2026-10-05T00:00:00+00:00",  # only three age positions: the date is the best that parses
        ),
        (
            "https://rainbow-corner.example",
            "Rainbow Corner Bar",
            "Cocktails, DJs and drag shows in the heart of Neukölln.",  # blank snippets are dropped
            None,  # the sources entry has no age
        ),
    ]


LONG_CONTENT: list[tuple[Endpoint, Payload]] = [
    (WEB, {"web": {"results": [{"description": "x" * 9_000, "url": "https://a.example/long"}]}}),
    (CONTEXT, {"grounding": {"generic": [{"url": "https://a.example/long", "snippets": ["b" * 5_000, "c" * 5_000]}]}}),
]
UNEXPECTED_SHAPES: list[tuple[Endpoint, Payload]] = [
    (WEB, {}),
    (WEB, {"web": "oops"}),
    (WEB, {"web": {"results": "oops"}}),
    (WEB, {"web": {"results": [1, None, "x", [], {"title": "no url"}]}}),
    (CONTEXT, {}),
    (CONTEXT, {"grounding": "oops", "sources": []}),
    (CONTEXT, {"grounding": {"generic": [1, None, "x", [], {"title": "no url"}]}}),
]


@pytest.mark.parametrize(("endpoint", "payload"), LONG_CONTENT)
@respx.mock
async def test_content_is_capped_per_hit(endpoint: Endpoint, payload: Payload, make_http: HttpFactory) -> None:
    respx.get(endpoint.url).mock(return_value=httpx.Response(200, json=payload))

    result = await _provider(endpoint, make_http).search(QUERY)

    assert [len(hit.content) for hit in result.hits] == [MAX_CONTENT_CHARS]


@pytest.mark.parametrize(("endpoint", "payload"), UNEXPECTED_SHAPES)
@respx.mock
async def test_unexpected_shapes_give_no_hits_instead_of_errors(
    endpoint: Endpoint, payload: Payload, make_http: HttpFactory
) -> None:
    respx.get(endpoint.url).mock(return_value=httpx.Response(200, json=payload))

    result = await _provider(endpoint, make_http).search(QUERY)

    assert result.hits == []


@respx.mock
async def test_llm_context_snippets_that_are_not_a_list_leave_the_content_empty(make_http: HttpFactory) -> None:
    payload: Payload = {"grounding": {"generic": [{"url": "https://a.example/x", "snippets": "oops"}]}, "sources": []}
    respx.get(LLM_CONTEXT_URL).mock(return_value=httpx.Response(200, json=payload))

    result = await _provider(CONTEXT, make_http).search(QUERY)

    assert [(hit.url, hit.content, hit.published_at) for hit in result.hits] == [("https://a.example/x", "", None)]


@pytest.mark.parametrize("endpoint", BOTH)
@respx.mock
async def test_a_call_is_billed_as_one_request(endpoint: Endpoint, make_http: HttpFactory, budget: Budget) -> None:
    respx.get(endpoint.url).mock(return_value=_ok(endpoint))

    result = await _provider(endpoint, make_http).search(QUERY)

    call = result.call
    assert (call.provider, call.status, call.http_status, call.attempts) == (endpoint.name, "ok", 200, 1)
    assert call.units == 1.0
    assert call.cost_usd == pytest.approx(REQUEST_USD)
    assert budget.spent_by_provider() == pytest.approx({endpoint.name: REQUEST_USD})
    assert budget.reserved_usd == 0


@pytest.mark.parametrize("endpoint", BOTH)
@respx.mock
async def test_an_empty_result_is_still_a_billed_success(
    endpoint: Endpoint, make_http: HttpFactory, budget: Budget
) -> None:
    respx.get(endpoint.url).mock(return_value=_ok(endpoint, endpoint.empty))

    result = await _provider(endpoint, make_http).search(QUERY)

    assert result.hits == []
    assert result.call.cost_usd == pytest.approx(REQUEST_USD)
    assert budget.spent_usd == pytest.approx(REQUEST_USD)


@pytest.mark.parametrize("endpoint", BOTH)
@pytest.mark.parametrize("status", [401, 403])
@respx.mock
async def test_authentication_errors_are_not_retried(
    endpoint: Endpoint, status: int, make_http: HttpFactory, budget: Budget
) -> None:
    route = respx.get(endpoint.url).mock(return_value=httpx.Response(status, json={"error": {"code": "AUTH"}}))

    with pytest.raises(ProviderError) as caught:
        await _provider(endpoint, make_http).search(QUERY)

    assert route.call_count == 1
    call = caught.value.call
    assert (call.status, call.http_status, call.attempts, call.cost_usd) == ("error", status, 1, 0)
    assert budget.spent_usd == 0
    assert budget.reserved_usd == 0


@pytest.mark.parametrize("endpoint", BOTH)
@pytest.mark.parametrize("status", [429, 500, 503])
@respx.mock
async def test_rate_limits_and_server_errors_are_retried_once_then_reported(
    endpoint: Endpoint, status: int, make_http: HttpFactory, budget: Budget
) -> None:
    route = respx.get(endpoint.url).mock(return_value=httpx.Response(status, text="try later"))

    with pytest.raises(ProviderError) as caught:
        await _provider(endpoint, make_http).search(QUERY)

    assert route.call_count == 2
    call = caught.value.call
    assert (call.status, call.http_status, call.attempts, call.cost_usd) == ("error", status, 2, 0)
    assert budget.spent_usd == 0


@pytest.mark.parametrize("endpoint", BOTH)
@respx.mock
async def test_a_retry_that_succeeds_is_billed_once(endpoint: Endpoint, make_http: HttpFactory, budget: Budget) -> None:
    route = respx.get(endpoint.url).mock(side_effect=[httpx.Response(429, text="slow down"), _ok(endpoint)])

    result = await _provider(endpoint, make_http).search(QUERY)

    assert route.call_count == 2
    assert result.call.attempts == 2
    assert len(result.hits) > 0
    assert budget.spent_usd == pytest.approx(REQUEST_USD)


@pytest.mark.parametrize("endpoint", BOTH)
@pytest.mark.parametrize("body", ["<html>maintenance</html>", "[]"])
@respx.mock
async def test_a_body_that_is_not_a_json_object_is_an_error_that_keeps_the_billed_cost(
    endpoint: Endpoint, body: str, make_http: HttpFactory, budget: Budget
) -> None:
    respx.get(endpoint.url).mock(return_value=httpx.Response(200, text=body))

    with pytest.raises(ProviderError, match="not a JSON object") as caught:
        await _provider(endpoint, make_http).search(QUERY)

    call = caught.value.call
    assert (call.status, call.http_status, call.error) == ("error", 200, "the response body is not a JSON object")
    assert call.cost_usd == pytest.approx(REQUEST_USD)
    assert budget.spent_usd == pytest.approx(REQUEST_USD)


@pytest.mark.parametrize("endpoint", BOTH)
@respx.mock
async def test_a_call_that_does_not_fit_the_budget_is_never_sent(
    endpoint: Endpoint, make_http: HttpFactory, budget: Budget
) -> None:
    route = respx.get(endpoint.url).mock(return_value=_ok(endpoint))
    budget.reserve("other", budget.limit_usd - 0.001)  # leaves less than the price of one request

    with pytest.raises(BudgetExceededError):
        await _provider(endpoint, make_http).search(QUERY)

    assert route.call_count == 0
    assert budget.spent_usd == 0


@pytest.mark.parametrize("endpoint", BOTH)
async def test_the_adapter_satisfies_the_search_provider_protocol(endpoint: Endpoint, make_http: HttpFactory) -> None:
    provider: SearchProvider = _provider(endpoint, make_http)

    assert provider.name == endpoint.name
    assert provider.flows == {Flow.VENUE, Flow.EVENT}
    assert provider.google_derived is False
    assert provider.price is endpoint.price
    assert provider.price.usd_per_unit == REQUEST_USD
    assert provider.price.verified_on == date(2026, 10, 8)


def test_the_registry_reads_the_limits_from_the_class() -> None:
    for adapter in (BraveWebSearch, BraveLlmContext):
        assert adapter.max_concurrency == 4
        assert 0 < adapter.min_interval_s <= 1 / 20  # at most 20 request starts per second, under the plan's 50
