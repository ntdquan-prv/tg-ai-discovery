"""Contract tests for the SerpApi events-pack adapter: request shape, parsing, cost and failures against fixtures."""

from collections.abc import Callable
from datetime import date

import httpx
import pytest
import respx

from tests.support import HttpFactory, load_fixture
from tg_discovery.domain.enums import Flow
from tg_discovery.pipelines.common.budget import Budget
from tg_discovery.providers.base import BudgetExceededError, ProviderError, SearchProvider, SearchQuery
from tg_discovery.providers.metering import MeteredHttp
from tg_discovery.providers.search.serpapi_events import MAX_CONTENT_CHARS, PRICE, SerpApiEvents
from tg_discovery.providers.serpapi import SEARCH_URL

API_KEY = "serp-test-key-123"
PAGE = "https://www.google.com/search?q=LGBTQ%2B+events+in+Bangkok&hl=en&gl=th&ie=UTF-8"

# Pyright checks the protocol and the constructor the registry calls (the key, then the HTTP layer).
_conforms: Callable[[str, MeteredHttp], SearchProvider] = SerpApiEvents


def _query(*, freshness_days: int | None = None, country: str = "TH", language: str = "en") -> SearchQuery:
    return SearchQuery(
        text="LGBTQ+ events in Bangkok",
        flow=Flow.EVENT,
        country=country,
        city="Bangkok",
        language=language,
        max_results=10,
        freshness_days=freshness_days,
    )


def _adapter(make_http: HttpFactory) -> SerpApiEvents:
    return SerpApiEvents(API_KEY, make_http(SerpApiEvents.name, SerpApiEvents.price))


def _serve(fixture: str = "events_bangkok.json") -> respx.Route:
    return respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, json=load_fixture("serpapi_events", fixture)))


async def _content(make_http: HttpFactory, *events: object) -> str:
    """Serve the given events in a results page and return the text of the one hit the adapter made of them."""
    body = {"search_metadata": {"google_url": PAGE}, "events_results": list(events)}
    respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, json=body))
    (hit,) = (await _adapter(make_http).search(_query())).hits
    return hit.content


@respx.mock
async def test_the_request_is_a_google_search_with_the_key_as_a_parameter(make_http: HttpFactory) -> None:
    route = _serve()

    result = await _adapter(make_http).search(_query())

    assert route.call_count == 1
    request = route.calls.last.request
    assert request.method == "GET"
    assert str(request.url).split("?")[0] == SEARCH_URL
    assert API_KEY not in request.headers.values()
    assert dict(request.url.params) == {
        "engine": "google",
        "q": "LGBTQ+ events in Bangkok",
        "hl": "en",
        "gl": "th",
        "api_key": API_KEY,
    }
    assert result.requested is None  # the pack has no result cap to send


@pytest.mark.parametrize("freshness_days", [None, 3, 60])
@respx.mock
async def test_there_is_no_date_filter_so_the_window_changes_nothing(
    make_http: HttpFactory, freshness_days: int | None
) -> None:
    route = _serve()

    await _adapter(make_http).search(_query(freshness_days=freshness_days))

    assert {"htichips", "location"}.isdisjoint(route.calls.last.request.url.params)


@pytest.mark.parametrize(
    ("country", "language", "sent"),
    [
        ("TH", "th", {"hl": "th", "gl": "th"}),
        ("BR", "pt-BR", {"hl": "pt-br", "gl": "br"}),
        ("Thailand", "english!", {}),
    ],
)
@respx.mock
async def test_language_and_country_are_sent_only_when_they_look_like_googles_codes(
    make_http: HttpFactory, country: str, language: str, sent: dict[str, str]
) -> None:
    route = _serve()

    await _adapter(make_http).search(_query(country=country, language=language))

    params = dict(route.calls.last.request.url.params)
    assert {key: params[key] for key in ("hl", "gl") if key in params} == sent


@respx.mock
async def test_the_events_pack_is_one_hit_on_the_results_page(make_http: HttpFactory) -> None:
    _serve()

    result = await _adapter(make_http).search(_query())

    assert result.provider == "serpapi_events"
    (hit,) = result.hits
    assert (hit.url, hit.title) == (PAGE, "Google events for LGBTQ+ events in Bangkok")
    assert hit.published_at is None
    assert "pride.example" not in result.model_dump_json()  # the organic results are not events


@respx.mock
async def test_each_event_is_written_out_as_labelled_lines_for_the_extractor(make_http: HttpFactory) -> None:
    _serve()

    (hit,) = (await _adapter(make_http).search(_query())).hits

    assert hit.content == (
        "Event: Bangkok Pride Parade 2026\n"
        "Type: Pride march\n"
        "When: Nov 29, 3:00 PM\n"
        "Where: Ratchaprasong Intersection, Bangkok, Thailand\n"
        "\n"
        "Event: Silom Drag Marathon\n"
        "Type: Drag performance\n"
        "When: Dec 6, 9:00 PM\n"
        "Where: Silom Soi 4, Bangkok, Thailand\n"
        "\n"
        "Event: Chiang Mai Queer Film Week\n"
        "When: Dec 12"
    )


@respx.mock
async def test_thumbnails_are_not_passed_on(make_http: HttpFactory) -> None:
    _serve()

    result = await _adapter(make_http).search(_query())

    dumped = result.model_dump_json()
    for leftover in ("thumb-event", "thumbnail", "serpapi.com/searches"):
        assert leftover not in dumped


@respx.mock
async def test_without_the_pages_address_the_hit_points_at_the_search_itself(make_http: HttpFactory) -> None:
    body: dict[str, object] = {"search_metadata": {}, "events_results": [{"title": "Party", "date": "Dec 1"}]}
    respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, json=body))

    (hit,) = (await _adapter(make_http).search(_query())).hits

    assert hit.url == "https://www.google.com/search?q=LGBTQ%2B+events+in+Bangkok"


@pytest.mark.parametrize(
    ("event", "content"),
    [
        ({"title": "T", "date": "Dec 1", "time": "8:00 PM"}, "Event: T\nWhen: Dec 1, 8:00 PM"),
        ({"title": "T", "time": "8:00 PM"}, "Event: T\nWhen: 8:00 PM"),
        ({"title": "T", "address": ["Hall", "Bangkok"], "type": "Drag"}, "Event: T\nType: Drag\nWhere: Hall, Bangkok"),
        # The retired events API gave the date as an object; the same words are used if it ever comes back that way.
        (
            {"title": "T", "date": {"start_date": "Nov 29", "when": "Sat, Nov 29, 3 PM"}},
            "Event: T\nWhen: Sat, Nov 29, 3 PM",
        ),
        ({"title": "T", "date": {"start_date": "Nov 29"}}, "Event: T\nWhen: Nov 29"),
        ({"title": "T", "date": 5, "time": 7, "address": "here", "type": 3}, "Event: T"),
        ({"title": "  Padded  "}, "Event: Padded"),
    ],
)
@respx.mock
async def test_the_lines_are_built_only_from_what_the_event_states(
    make_http: HttpFactory, event: dict[str, object], content: str
) -> None:
    assert await _content(make_http, event) == content


@respx.mock
async def test_entries_that_are_not_events_are_left_out_and_without_events_there_is_no_hit(
    make_http: HttpFactory,
) -> None:
    body = {"events_results": ["junk", 5, None, {"date": "Dec 1"}, {"title": 7}, {"title": "  "}]}
    respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, json=body))

    assert (await _adapter(make_http).search(_query())).hits == []


@respx.mock
async def test_a_long_page_is_cut_to_what_the_extractor_reads(make_http: HttpFactory) -> None:
    events = [{"title": f"Event number {number} " + "x" * 200, "date": "Dec 1"} for number in range(60)]

    assert len(await _content(make_http, *events)) == MAX_CONTENT_CHARS


@respx.mock
async def test_one_search_is_billed_at_the_plan_price(make_http: HttpFactory, budget: Budget) -> None:
    _serve()

    result = await _adapter(make_http).search(_query())

    assert (result.call.provider, result.call.endpoint, result.call.status) == (
        "serpapi_events",
        "google_search",
        "ok",
    )
    assert (result.call.units, result.call.attempts, result.call.http_status) == (1.0, 1, 200)
    assert result.call.cost_usd == pytest.approx(0.010)
    assert budget.spent_usd == pytest.approx(0.010)


@pytest.mark.parametrize("fixture", ["no_events.json", "empty.json"])
@respx.mock
async def test_a_query_that_brings_up_no_events_pack_gives_no_hits_but_was_still_a_search(
    make_http: HttpFactory, budget: Budget, fixture: str
) -> None:
    _serve(fixture)

    result = await _adapter(make_http).search(_query())

    assert result.hits == []
    assert (result.call.status, result.call.cost_usd) == ("ok", pytest.approx(0.010))
    assert budget.spent_usd == pytest.approx(0.010)


@pytest.mark.parametrize("body", [{}, {"events_results": []}, {"events_results": "none"}])
@respx.mock
async def test_a_reply_without_events_gives_no_hits(make_http: HttpFactory, body: dict[str, object]) -> None:
    respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, json=body))

    assert (await _adapter(make_http).search(_query())).hits == []


@respx.mock
async def test_a_body_that_is_not_a_json_object_is_a_failed_call_that_was_still_billed(
    make_http: HttpFactory, budget: Budget
) -> None:
    respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, json=["not", "an", "object"]))

    with pytest.raises(ProviderError, match="not a JSON object") as caught:
        await _adapter(make_http).search(_query())

    assert caught.value.call.status == "error"
    assert budget.spent_usd == pytest.approx(0.010)


@respx.mock
async def test_any_other_error_in_a_200_reply_fails_the_call_and_never_shows_the_key(make_http: HttpFactory) -> None:
    body = {"error": f"Invalid API key {API_KEY}. Your API key should be here: https://serpapi.com/manage-api-key"}
    respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, json=body))

    with pytest.raises(ProviderError, match="SerpApi said: Invalid API key") as caught:
        await _adapter(make_http).search(_query())

    assert API_KEY not in str(caught.value)
    assert API_KEY not in (caught.value.call.error or "")


@pytest.mark.parametrize("status", [400, 401, 403, 404])
@respx.mock
async def test_client_errors_are_not_retried_and_not_billed(
    make_http: HttpFactory, budget: Budget, status: int
) -> None:
    route = respx.get(SEARCH_URL).mock(return_value=httpx.Response(status, json={"error": "rejected"}))

    with pytest.raises(ProviderError) as caught:
        await _adapter(make_http).search(_query())

    assert route.call_count == 1
    assert (caught.value.call.attempts, caught.value.call.http_status) == (1, status)
    assert (caught.value.call.status, caught.value.call.cost_usd) == ("error", 0.0)
    assert budget.spent_usd == 0.0
    assert API_KEY not in str(caught.value)


@pytest.mark.parametrize("status", [429, 500, 503])
@respx.mock
async def test_rate_limiting_and_server_errors_are_retried_once_and_not_billed(
    make_http: HttpFactory, budget: Budget, status: int
) -> None:
    route = respx.get(SEARCH_URL).mock(return_value=httpx.Response(status, json={"error": "busy"}))

    with pytest.raises(ProviderError) as caught:
        await _adapter(make_http).search(_query())

    assert route.call_count == 2
    assert (caught.value.call.attempts, caught.value.call.http_status) == (2, status)
    assert budget.spent_usd == 0.0


@respx.mock(assert_all_called=False)
async def test_a_search_that_does_not_fit_the_budget_is_never_sent() -> None:
    route = respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, json={}))
    async with httpx.AsyncClient() as client:
        http = MeteredHttp(SerpApiEvents.name, SerpApiEvents.price, client, Budget(0.009))

        with pytest.raises(BudgetExceededError):
            await SerpApiEvents(API_KEY, http).search(_query())

    assert route.call_count == 0


def test_the_adapter_describes_itself_to_the_registry() -> None:
    assert SerpApiEvents.name == "serpapi_events"
    assert SerpApiEvents.flows == {Flow.EVENT}
    assert SerpApiEvents.google_derived is True
    assert SerpApiEvents.price is PRICE
    assert SerpApiEvents.max_concurrency == 2
    assert SerpApiEvents.min_interval_s > 0
    assert SerpApiEvents.timeout_s > 30  # a search through SerpApi regularly takes longer than the default 30 s
    assert (PRICE.unit, PRICE.usd_per_unit, PRICE.verified_on) == ("search", 0.010, date(2026, 10, 8))
    assert PRICE.source_url == "https://serpapi.com/pricing"
