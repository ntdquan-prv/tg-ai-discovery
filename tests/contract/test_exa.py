import json
from datetime import UTC, date, datetime, timedelta
from typing import cast, get_args

import httpx
import pytest
import respx

from tests.support import HttpFactory, load_fixture
from tg_discovery.domain.enums import Flow
from tg_discovery.pipelines.common.budget import Budget
from tg_discovery.providers.base import BudgetExceededError, ProviderError, SearchProvider, SearchQuery
from tg_discovery.providers.search.exa import MAX_CONTENT_CHARS, PRICE, URL, ExaSearch, ExaSearchType

QUERY = SearchQuery(text="gay bars Berlin", flow=Flow.VENUE, country="DE", city="Berlin", language="de", max_results=10)
WORST_CASE_USD = 0.017  # auto, 10 results: 0.007 base + 10 pages of text at 0.001
Payload = dict[str, object]


def _provider(make_http: HttpFactory, *, search_type: ExaSearchType = "auto") -> ExaSearch:
    return ExaSearch("key-123", make_http("exa", PRICE), search_type=search_type)


def _ok(fixture: str = "search.json") -> httpx.Response:
    return httpx.Response(200, json=load_fixture("exa", fixture))


def _sent(route: respx.Route) -> Payload:
    return json.loads(route.calls.last.request.content)


@respx.mock
async def test_the_request_is_a_post_with_the_api_key_and_the_documented_body(make_http: HttpFactory) -> None:
    route = respx.post(URL).mock(return_value=_ok())

    await _provider(make_http).search(QUERY)

    request = route.calls.last.request
    assert request.method == "POST"
    assert request.headers["x-api-key"] == "key-123"
    assert request.headers["content-type"] == "application/json"
    assert _sent(route) == {
        "query": "gay bars Berlin",
        "type": "auto",
        "numResults": 10,
        "userLocation": "DE",
        "contents": {"text": {"maxCharacters": MAX_CONTENT_CHARS}},
    }


@respx.mock
async def test_the_country_is_left_out_when_the_query_has_none(make_http: HttpFactory) -> None:
    route = respx.post(URL).mock(return_value=_ok())

    await _provider(make_http).search(QUERY.model_copy(update={"country": ""}))

    assert "userLocation" not in _sent(route)


@pytest.mark.parametrize("days", [1, 7, 30, 400])
@respx.mock
async def test_freshness_days_become_a_start_published_date_at_midnight_utc(days: int, make_http: HttpFactory) -> None:
    route = respx.post(URL).mock(return_value=_ok())
    before = datetime.now(UTC).date()

    await _provider(make_http).search(QUERY.model_copy(update={"freshness_days": days}))

    after = datetime.now(UTC).date()  # the test may straddle midnight UTC
    assert _sent(route)["startPublishedDate"] in {
        f"{day - timedelta(days=days):%Y-%m-%d}T00:00:00.000Z" for day in (before, after)
    }


@respx.mock
async def test_no_date_filter_is_sent_when_none_was_asked_for(make_http: HttpFactory) -> None:
    route = respx.post(URL).mock(return_value=_ok())

    await _provider(make_http).search(QUERY)

    assert "startPublishedDate" not in _sent(route)


@pytest.mark.parametrize("search_type", get_args(ExaSearchType))
@respx.mock
async def test_the_search_type_is_a_constructor_option(search_type: ExaSearchType, make_http: HttpFactory) -> None:
    route = respx.post(URL).mock(return_value=_ok())

    await _provider(make_http, search_type=search_type).search(QUERY)

    assert _sent(route)["type"] == search_type


async def test_an_unknown_search_type_is_refused_at_construction(make_http: HttpFactory) -> None:
    with pytest.raises(ValueError, match="unknown Exa search type 'turbo'"):
        _provider(make_http, search_type=cast(ExaSearchType, "turbo"))


@pytest.mark.parametrize(("asked", "sent"), [(1, 1), (10, 10), (100, 100), (150, 100)])
@respx.mock
async def test_the_result_cap_is_limited_to_100_and_reported(asked: int, sent: int, make_http: HttpFactory) -> None:
    route = respx.post(URL).mock(return_value=_ok())

    result = await _provider(make_http).search(QUERY.model_copy(update={"max_results": asked}))

    assert _sent(route)["numResults"] == sent
    assert result.requested == sent


@respx.mock
async def test_results_become_hits_with_the_best_text_exa_returned_and_utc_dates(make_http: HttpFactory) -> None:
    respx.post(URL).mock(return_value=_ok())

    result = await _provider(make_http).search(QUERY)

    assert result.provider == "exa"
    assert [
        (hit.url, hit.title, hit.content, hit.published_at.isoformat() if hit.published_at else None)
        for hit in result.hits
    ] == [
        (
            "https://www.berlin-queer-guide.example/bars?ref=home",
            "Gay Bars in Berlin: The Complete 2026 Guide",
            "Berlin has one of the largest queer scenes in Europe. Most bars cluster around Motzstraße in Schöneberg "
            "and in Neukölln.\n\n"
            "Rainbow Corner Bar - Motzstraße 12, 10777 Berlin. Open Mon-Thu 20:00-03:00, Fri-Sun until late. Drag show "
            "every Saturday at 23:00.",
            "2026-09-28T00:00:00+00:00",
        ),
        (
            "https://events.berlin-pride.example/calendar",
            "Pride calendar 2026",
            "Rainbow Night, 14 November 2026, 21:00, Rainbow Corner Bar, Motzstraße 12.\n\n"
            "Winter Pride Party, 5 December 2026, 22:00, Club Prisma.",  # highlights, joined: there is no text
            "2026-10-02T00:00:00+00:00",  # a date without a time
        ),
        (
            "https://nightlife-wiki.example/Berlin_queer_nightlife",
            "Queer nightlife in Berlin",
            "An overview of the queer bars, clubs and saunas of Berlin and how the scene developed after 1990.",
            None,  # the summary is the only text, and there is no date
        ),
        ("https://rainbow-corner.example", "Rainbow Corner Bar - Neukölln", "", None),  # "last Tuesday" is not a date
        (
            "https://cafe-list.example/berlin/gay-friendly",
            "Gay-friendly cafes in Berlin",
            "Ten cafes where everyone is welcome.",  # the text is empty, so the highlights are used
            "2026-09-30T18:45:10+00:00",  # sent as +02:00
        ),
        ("https://sauna.example/berlin", "Sauna Berlin", "Open daily 12:00-24:00.", None),  # trimmed, fragment gone
    ]


@pytest.mark.parametrize(
    "item",
    [
        {"url": "https://a.example/long", "text": "t" * 9_000},
        {"url": "https://a.example/long", "highlights": ["h" * 5_000, "i" * 5_000]},
        {"url": "https://a.example/long", "summary": "s" * 9_000},
    ],
)
@respx.mock
async def test_content_is_capped_per_hit(item: Payload, make_http: HttpFactory) -> None:
    respx.post(URL).mock(return_value=httpx.Response(200, json={"results": [item]}))

    result = await _provider(make_http).search(QUERY)

    assert [len(hit.content) for hit in result.hits] == [MAX_CONTENT_CHARS]


UNEXPECTED_SHAPES: list[Payload] = [
    {},
    {"results": None},
    {"results": "oops"},
    {"results": [1, None, "x", [], {"title": "no url"}, {"url": "", "text": "empty url"}]},
]


@pytest.mark.parametrize("payload", UNEXPECTED_SHAPES)
@respx.mock
async def test_unexpected_shapes_give_no_hits_instead_of_errors(payload: Payload, make_http: HttpFactory) -> None:
    respx.post(URL).mock(return_value=httpx.Response(200, json=payload))

    result = await _provider(make_http).search(QUERY)

    assert result.hits == []


@respx.mock
async def test_fields_of_the_wrong_type_leave_the_hit_with_empty_content(make_http: HttpFactory) -> None:
    item: Payload = {
        "url": "https://a.example/x",
        "title": 7,
        "text": 5,
        "highlights": "oops",
        "publishedDate": 20260928,
    }
    respx.post(URL).mock(return_value=httpx.Response(200, json={"results": [item]}))

    result = await _provider(make_http).search(QUERY)

    assert [(hit.url, hit.title, hit.content, hit.published_at) for hit in result.hits] == [
        ("https://a.example/x", None, "", None)
    ]


@respx.mock
async def test_the_cost_is_taken_from_cost_dollars(make_http: HttpFactory, budget: Budget) -> None:
    respx.post(URL).mock(return_value=_ok())

    result = await _provider(make_http).search(QUERY)

    call = result.call
    assert (call.provider, call.endpoint, call.status, call.http_status, call.attempts) == (
        "exa",
        "search",
        "ok",
        200,
        1,
    )
    assert call.cost_usd == pytest.approx(0.014)  # costDollars.total, not the 0.007 list price
    assert call.units == 1.0  # one request; the dollars carry the extra results and contents
    assert budget.spent_by_provider() == pytest.approx({"exa": 0.014})
    assert budget.reserved_usd == 0


@pytest.mark.parametrize(
    ("search_type", "results", "reserved_usd"),
    [
        ("auto", 10, 0.017),  # 0.007 base + 10 pages of text
        ("instant", 10, 0.014),  # 0.004 base
        ("auto", 25, 0.047),  # + 15 results above ten + 25 pages
        ("deep-reasoning", 100, 0.205),  # 0.015 base + 90 results above ten + 100 pages
    ],
)
@respx.mock
async def test_the_reservation_is_the_worst_case_for_the_type_and_result_count(
    search_type: ExaSearchType, results: int, reserved_usd: float, make_http: HttpFactory, budget: Budget
) -> None:
    reserved: list[float] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        reserved.append(budget.reserved_usd)  # what is held while the request is in flight
        return _ok("search_empty.json")

    respx.post(URL).mock(side_effect=handler)

    await _provider(make_http, search_type=search_type).search(QUERY.model_copy(update={"max_results": results}))

    assert reserved == [pytest.approx(reserved_usd)]
    assert budget.reserved_usd == 0


COST_NOT_REPORTED: list[Payload] = [
    {"results": []},
    {"results": [], "costDollars": None},
    {"results": [], "costDollars": {"search": {"neural": 0.007}}},
    {"results": [], "costDollars": {"total": "free"}},
    {"results": [], "costDollars": {"total": True}},
]


@pytest.mark.parametrize("payload", COST_NOT_REPORTED)
@respx.mock
async def test_a_response_that_does_not_report_its_cost_is_billed_at_the_worst_case(
    payload: Payload, make_http: HttpFactory, budget: Budget
) -> None:
    respx.post(URL).mock(return_value=httpx.Response(200, json=payload))

    result = await _provider(make_http).search(QUERY)

    assert result.call.cost_usd == pytest.approx(WORST_CASE_USD)
    assert budget.spent_usd == pytest.approx(WORST_CASE_USD)


@respx.mock
async def test_an_empty_result_is_billed_as_reported(make_http: HttpFactory, budget: Budget) -> None:
    respx.post(URL).mock(return_value=_ok("search_empty.json"))

    result = await _provider(make_http).search(QUERY)

    assert result.hits == []
    assert result.call.cost_usd == pytest.approx(0.007)
    assert budget.spent_usd == pytest.approx(0.007)


@pytest.mark.parametrize(
    ("status", "tag"),
    [(400, "INVALID_REQUEST_BODY"), (401, "INVALID_API_KEY"), (402, "NO_MORE_CREDITS"), (403, "FEATURE_DISABLED")],
)
@respx.mock
async def test_client_errors_are_not_retried(status: int, tag: str, make_http: HttpFactory, budget: Budget) -> None:
    body = {"requestId": "r1", "error": "rejected", "tag": tag}
    route = respx.post(URL).mock(return_value=httpx.Response(status, json=body))

    with pytest.raises(ProviderError) as caught:
        await _provider(make_http).search(QUERY)

    assert route.call_count == 1
    call = caught.value.call
    assert (call.status, call.http_status, call.attempts, call.cost_usd) == ("error", status, 1, 0)
    assert budget.spent_usd == 0
    assert budget.reserved_usd == 0


@pytest.mark.parametrize(
    ("status", "tag"), [(429, "RATE_LIMIT_EXCEEDED"), (500, "INTERNAL"), (503, "SERVICE_OVERLOADED")]
)
@respx.mock
async def test_rate_limits_and_server_errors_are_retried_once_then_reported(
    status: int, tag: str, make_http: HttpFactory, budget: Budget
) -> None:
    route = respx.post(URL).mock(
        return_value=httpx.Response(status, json={"requestId": "r1", "error": "x", "tag": tag})
    )

    with pytest.raises(ProviderError) as caught:
        await _provider(make_http).search(QUERY)

    assert route.call_count == 2
    call = caught.value.call
    assert (call.status, call.http_status, call.attempts, call.cost_usd) == ("error", status, 2, 0)
    assert budget.spent_usd == 0


@respx.mock
async def test_a_retry_that_succeeds_is_billed_once_at_the_reported_cost(
    make_http: HttpFactory, budget: Budget
) -> None:
    route = respx.post(URL).mock(side_effect=[httpx.Response(503, json={"tag": "SERVICE_OVERLOADED"}), _ok()])

    result = await _provider(make_http).search(QUERY)

    assert route.call_count == 2
    assert result.call.attempts == 2
    assert budget.spent_usd == pytest.approx(0.014)


@respx.mock
async def test_a_timeout_is_charged_at_the_worst_case_estimate(make_http: HttpFactory, budget: Budget) -> None:
    respx.post(URL).mock(side_effect=httpx.ReadTimeout("too slow"))

    with pytest.raises(ProviderError) as caught:
        await _provider(make_http).search(QUERY)

    assert caught.value.call.status == "timeout"
    assert caught.value.call.cost_usd == pytest.approx(WORST_CASE_USD)
    assert budget.spent_usd == pytest.approx(WORST_CASE_USD)


@pytest.mark.parametrize("body", ["<html>maintenance</html>", "[]"])
@respx.mock
async def test_a_body_that_is_not_a_json_object_is_an_error_that_keeps_the_billed_cost(
    body: str, make_http: HttpFactory, budget: Budget
) -> None:
    respx.post(URL).mock(return_value=httpx.Response(200, text=body))

    with pytest.raises(ProviderError, match="not a JSON object") as caught:
        await _provider(make_http).search(QUERY)

    call = caught.value.call
    assert (call.status, call.http_status, call.error) == ("error", 200, "the response body is not a JSON object")
    assert call.cost_usd == pytest.approx(WORST_CASE_USD)  # no costDollars to read, so the worst case stands
    assert budget.spent_usd == pytest.approx(WORST_CASE_USD)


@respx.mock
async def test_a_call_that_does_not_fit_the_budget_is_never_sent(make_http: HttpFactory, budget: Budget) -> None:
    route = respx.post(URL).mock(return_value=_ok())
    budget.reserve("other", budget.limit_usd - 0.01)  # leaves less than the 0.017 worst case

    with pytest.raises(BudgetExceededError):
        await _provider(make_http).search(QUERY)

    assert route.call_count == 0
    assert budget.spent_usd == 0


async def test_the_adapter_satisfies_the_search_provider_protocol(make_http: HttpFactory) -> None:
    provider: SearchProvider = _provider(make_http)

    assert provider.name == "exa"
    assert provider.flows == {Flow.VENUE, Flow.EVENT}
    assert provider.google_derived is False
    assert provider.price is PRICE
    assert provider.price.usd_per_unit == 0.007
    assert provider.price.verified_on == date(2026, 10, 8)


def test_the_registry_reads_the_limits_from_the_class() -> None:
    assert ExaSearch.max_concurrency == 4
    assert 0 < ExaSearch.min_interval_s <= 0.1  # at most 10 request starts per second, the /search limit
