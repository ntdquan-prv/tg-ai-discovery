"""Contract tests for the SerpApi Google Maps adapter: request shape, parsing, cost and failures against fixtures."""

from collections.abc import Callable
from datetime import date

import httpx
import pytest
import respx

from tests.support import HttpFactory, load_fixture
from tg_discovery.domain.enums import LgbtqLevel, OperatingStatus
from tg_discovery.domain.geo import BoundingBox
from tg_discovery.domain.models import SourceRef, VenueDraft, scrub_google_derived
from tg_discovery.pipelines.common.budget import Budget
from tg_discovery.providers.base import BudgetExceededError, ProviderError, VenueProvider, VenueQuery
from tg_discovery.providers.maps.serpapi_maps import MAX_MAP_HEIGHT_M, PRICE, SerpApiMaps
from tg_discovery.providers.metering import MeteredHttp
from tg_discovery.providers.serpapi import SEARCH_URL

API_KEY = "serp-test-key-123"
AREA = BoundingBox(south=13.6, west=100.4, north=13.9, east=100.7)
QUERY = VenueQuery(text="gay bar", country="TH", city="Bangkok", area=AREA, language="en", max_results=20)

# Pyright checks the protocol and the constructor the registry calls (the HTTP layer, then the key).
_conforms: Callable[[MeteredHttp, str], VenueProvider] = SerpApiMaps


def _adapter(make_http: HttpFactory) -> SerpApiMaps:
    return SerpApiMaps(make_http(SerpApiMaps.name, SerpApiMaps.price), api_key=API_KEY)


def _serve(fixture: str = "gay_bar_bangkok.json") -> respx.Route:
    return respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, json=load_fixture("serpapi_maps", fixture)))


def _place(**fields: object) -> dict[str, object]:
    return {"title": "Test Venue", **fields}


async def _fetch(make_http: HttpFactory, *places: object) -> list[VenueDraft]:
    """Serve the given places through the mocked endpoint and return what the adapter made of them."""
    respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, json={"local_results": list(places)}))
    return (await _adapter(make_http).find_venues(QUERY)).venues


def _map_url(name: str, place_id: str) -> str:
    return f"https://www.google.com/maps/search/?api=1&query={name.replace(' ', '%20')}&query_place_id={place_id}"


@respx.mock
async def test_the_request_is_a_get_with_the_key_as_a_parameter_and_the_box_as_a_map_view(
    make_http: HttpFactory,
) -> None:
    route = _serve()

    await _adapter(make_http).find_venues(QUERY)

    assert route.call_count == 1
    request = route.calls.last.request
    assert request.method == "GET"
    assert str(request.url).split("?")[0] == SEARCH_URL
    assert API_KEY not in request.headers.values()
    # The box is 0.3 degrees tall, 33,396 metres, and the map is centred on it.
    assert dict(request.url.params) == {
        "engine": "google_maps",
        "type": "search",
        "q": "gay bar",
        "ll": "@13.750000,100.550000,33396m",
        "hl": "en",
        "gl": "th",
        "api_key": API_KEY,
    }


@respx.mock
async def test_a_box_taller_than_the_largest_map_is_clamped(make_http: HttpFactory) -> None:
    route = _serve()
    huge = BoundingBox(south=-80, west=-170, north=80, east=170)

    await _adapter(make_http).find_venues(QUERY.model_copy(update={"area": huge}))

    assert route.calls.last.request.url.params["ll"] == f"@0.000000,0.000000,{MAX_MAP_HEIGHT_M}m"


@pytest.mark.parametrize(("max_results", "count"), [(1, 1), (2, 2), (20, 20), (50, 20), (200, 20)])
@respx.mock
async def test_a_page_holds_at_most_twenty_places_and_the_cap_is_reported_as_requested(
    make_http: HttpFactory, max_results: int, count: int
) -> None:
    _serve()

    result = await _adapter(make_http).find_venues(QUERY.model_copy(update={"max_results": max_results}))

    assert result.requested == count
    assert len(result.venues) == min(count, 5)  # the fixture holds five places


@pytest.mark.parametrize(
    ("country", "language", "sent"),
    [
        ("TH", "th", {"hl": "th", "gl": "th"}),
        ("de", "DE", {"hl": "de", "gl": "de"}),
        ("BR", "pt-BR", {"hl": "pt-br", "gl": "br"}),
        ("Thailand", "english!", {}),
        ("T", "", {}),
    ],
)
@respx.mock
async def test_language_and_country_are_sent_only_when_they_look_like_googles_codes(
    make_http: HttpFactory, country: str, language: str, sent: dict[str, str]
) -> None:
    route = _serve()

    await _adapter(make_http).find_venues(QUERY.model_copy(update={"country": country, "language": language}))

    params = dict(route.calls.last.request.url.params)
    assert {key: params[key] for key in ("hl", "gl") if key in params} == sent


@respx.mock
async def test_a_blank_search_text_leaves_out_q(make_http: HttpFactory) -> None:
    route = _serve()

    await _adapter(make_http).find_venues(QUERY.model_copy(update={"text": "  "}))

    assert "q" not in route.calls.last.request.url.params


@respx.mock
async def test_results_become_drafts_in_the_order_google_gave_them(make_http: HttpFactory) -> None:
    _serve()

    result = await _adapter(make_http).find_venues(QUERY)

    assert result.provider == "serpapi_maps"
    assert [venue.name for venue in result.venues] == [
        "Bar Example Silom",
        "Club Example",
        "Sauna Example",
        "Lesbian Bar Example",
        "Mystery Place",
    ]


@respx.mock
async def test_a_place_with_everything_gives_a_complete_draft(make_http: HttpFactory) -> None:
    _serve()

    result = await _adapter(make_http).find_venues(QUERY)

    bar = result.venues[0]
    assert bar.venue_type == "Bar"
    assert bar.address == "Soi Si Lom 4, Bang Rak, Bangkok 10500"
    assert (bar.lat, bar.lng) == (13.7284, 100.5329)
    assert bar.website == "https://bar-example.example"
    assert bar.phone == "+66 2 000 0001"
    assert bar.operating_status is OperatingStatus.UNKNOWN
    assert bar.lgbtq is not None
    assert (bar.lgbtq.level, bar.lgbtq.evidence) == (LgbtqLevel.PRIMARY, [])
    assert bar.external_ids == {
        "google_place_id": "ChIJexample0000000001",
        "google_data_id": "0x30e29ee00000001:0x1000000000000001",
    }
    assert bar.sources == [
        SourceRef(
            url=_map_url("Bar Example Silom", "ChIJexample0000000001"),
            title="Bar Example Silom",
            provider="serpapi_maps",
            retrieved_at=result.call.at,
        )
    ]
    assert bar.missing_fields == ["opening_hours", "description"]


@respx.mock
async def test_places_with_less_data_still_become_drafts(make_http: HttpFactory) -> None:
    _serve()

    result = await _adapter(make_http).find_venues(QUERY)

    club, sauna, lesbian, bare = result.venues[1], result.venues[2], result.venues[3], result.venues[4]
    assert (club.venue_type, club.website, club.phone, club.lgbtq) == ("Dance Club", None, None, None)
    # A Facebook page is not a website; a closure written into open_state is a closure.
    assert (sauna.venue_type, sauna.website, sauna.operating_status) == (
        "Sauna",
        None,
        OperatingStatus.PERMANENTLY_CLOSED,
    )
    assert (lesbian.website, lesbian.operating_status) == (
        "https://lesbian-bar.example/en",
        OperatingStatus.TEMPORARILY_CLOSED,
    )
    assert lesbian.lgbtq is not None
    assert lesbian.lgbtq.level is LgbtqLevel.PRIMARY
    assert lesbian.external_ids == {"google_place_id": "ChIJexample0000000004"}
    # Without a place id there is no page to link to, and the draft says so rather than making one up.
    assert (bare.venue_type, bare.lat, bare.lng, bare.external_ids, bare.sources) == ("Cafe", None, None, {}, [])


@respx.mock
async def test_ratings_reviews_photos_and_hours_are_not_carried(make_http: HttpFactory) -> None:
    _serve()

    result = await _adapter(make_http).find_venues(QUERY)

    dumped = result.model_dump_json()
    for leftover in ("8123", "AF1Qip", "thumb-example", "8 PM", "reviews_link", "photos_link", "dine_in", "$$"):
        assert leftover not in dumped


@respx.mock
async def test_scrubbing_keeps_the_ids_and_the_level_and_drops_the_phone(make_http: HttpFactory) -> None:
    _serve()

    bar = (await _adapter(make_http).find_venues(QUERY)).venues[0]
    scrubbed = scrub_google_derived(bar)

    assert scrubbed.phone is None
    assert scrubbed.external_ids == bar.external_ids
    assert (scrubbed.name, scrubbed.address, scrubbed.lat, scrubbed.website) == (
        bar.name,
        bar.address,
        bar.lat,
        bar.website,
    )
    assert scrubbed.lgbtq is not None
    assert scrubbed.lgbtq.level is LgbtqLevel.PRIMARY


@respx.mock
async def test_one_page_is_one_search_billed_at_the_plan_price(make_http: HttpFactory, budget: Budget) -> None:
    _serve()

    result = await _adapter(make_http).find_venues(QUERY)

    assert (result.call.provider, result.call.endpoint, result.call.status) == ("serpapi_maps", "google_maps", "ok")
    assert (result.call.units, result.call.attempts, result.call.http_status) == (1.0, 1, 200)
    assert result.call.cost_usd == pytest.approx(0.010)
    assert budget.spent_usd == pytest.approx(0.010)


@pytest.mark.parametrize("body", [{}, {"local_results": []}, {"local_results": "none"}, {"place_results": 5}])
@respx.mock
async def test_a_reply_without_places_gives_no_venues(make_http: HttpFactory, body: dict[str, object]) -> None:
    respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, json=body))

    result = await _adapter(make_http).find_venues(QUERY)

    assert result.venues == []
    assert result.call.cost_usd == pytest.approx(0.010)


@respx.mock
async def test_the_no_results_error_is_an_empty_answer_not_a_failure(make_http: HttpFactory) -> None:
    _serve("empty.json")

    result = await _adapter(make_http).find_venues(QUERY)

    assert result.venues == []
    assert result.call.status == "ok"


@respx.mock
async def test_a_search_that_google_resolves_to_one_place_comes_back_as_place_results(make_http: HttpFactory) -> None:
    body = {
        "place_results": _place(
            title="Only Place", place_id="ChIJonly", gps_coordinates={"latitude": 1, "longitude": 2}
        )
    }
    respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, json=body))

    venues = (await _adapter(make_http).find_venues(QUERY)).venues

    assert [(venue.name, venue.lat, venue.lng) for venue in venues] == [("Only Place", 1.0, 2.0)]


@pytest.mark.parametrize(
    ("types", "venue_type", "level"),
    [
        (["Gay bar"], "Bar", LgbtqLevel.PRIMARY),
        (["Lesbian bar", "Bar"], "Bar", LgbtqLevel.PRIMARY),
        (["LGBT bar"], "Bar", LgbtqLevel.PRIMARY),
        (["Queer cafe"], "Cafe", LgbtqLevel.PRIMARY),
        (["Night club"], "Dance Club", None),
        (["Bar"], "Bar", None),
        (["Sex shop"], "Shop", None),
        (["Massage spa", "Spa"], "Spa", None),
        (["Hotel"], "Hotels", None),
        (["Event venue"], None, None),
        ([], None, None),
    ],
)
@respx.mock
async def test_the_place_category_gives_the_venue_type_and_only_a_gay_category_gives_a_level(
    make_http: HttpFactory, types: list[str], venue_type: str | None, level: LgbtqLevel | None
) -> None:
    (venue,) = await _fetch(make_http, _place(types=types))

    assert venue.venue_type == venue_type
    assert (venue.lgbtq.level if venue.lgbtq else None) == level


@pytest.mark.parametrize(
    ("open_state", "status"),
    [
        ("Permanently closed", OperatingStatus.PERMANENTLY_CLOSED),
        ("⋅ Permanently closed", OperatingStatus.PERMANENTLY_CLOSED),
        ("Temporarily closed", OperatingStatus.TEMPORARILY_CLOSED),
        ("Open ⋅ Closes 2 AM", OperatingStatus.UNKNOWN),
        ("Closed ⋅ Opens 9 PM", OperatingStatus.UNKNOWN),
        (None, OperatingStatus.UNKNOWN),
        (7, OperatingStatus.UNKNOWN),
    ],
)
@respx.mock
async def test_only_a_closure_changes_the_operating_status(
    make_http: HttpFactory, open_state: object, status: OperatingStatus
) -> None:
    (venue,) = await _fetch(make_http, _place(open_state=open_state))

    assert venue.operating_status is status


@pytest.mark.parametrize(
    ("coordinates", "position"),
    [
        ({"latitude": 13.7, "longitude": 100.5}, (13.7, 100.5)),
        ({"latitude": 13, "longitude": 100}, (13.0, 100.0)),
        ({"latitude": 91.0, "longitude": 100.5}, (None, None)),
        ({"latitude": 13.7, "longitude": 252.5}, (None, None)),
        ({"latitude": "13.7", "longitude": 100.5}, (None, None)),
        ({"latitude": True, "longitude": 100.5}, (None, None)),
        ({"latitude": 13.7}, (None, None)),
        ([13.7, 100.5], (None, None)),
        (None, (None, None)),
    ],
)
@respx.mock
async def test_coordinates_are_kept_only_when_both_are_numbers_inside_the_range(
    make_http: HttpFactory, coordinates: object, position: tuple[float | None, float | None]
) -> None:
    (venue,) = await _fetch(make_http, _place(gps_coordinates=coordinates))

    assert (venue.lat, venue.lng) == position


@pytest.mark.parametrize(
    ("url", "website"),
    [
        ("https://bluebottle.example/?utm_source=google&fbclid=1#top", "https://bluebottle.example"),
        ("bluebottle.example/menu", "https://bluebottle.example/menu"),
        ("https://www.facebook.com/bluebottle", None),
        ("https://www.tripadvisor.co.uk/Restaurant_Review-g186338-d1", None),
        ("not a url", None),
        (None, None),
    ],
)
@respx.mock
async def test_only_the_places_own_site_is_its_website(
    make_http: HttpFactory, url: str | None, website: str | None
) -> None:
    (venue,) = await _fetch(make_http, _place(website=url))

    assert venue.website == website


@respx.mock
async def test_results_that_are_not_objects_or_have_no_title_are_skipped(make_http: HttpFactory) -> None:
    venues = await _fetch(make_http, "junk", 5, None, {"title": "  "}, {"title": 7}, _place(title="  Padded  "))

    assert [venue.name for venue in venues] == ["Padded"]


@respx.mock
async def test_a_body_that_is_not_a_json_object_is_a_failed_call_that_was_still_billed(
    make_http: HttpFactory, budget: Budget
) -> None:
    respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, text="<html>maintenance</html>"))

    with pytest.raises(ProviderError, match="not JSON") as caught:
        await _adapter(make_http).find_venues(QUERY)

    assert caught.value.call.status == "error"
    assert budget.spent_usd == pytest.approx(0.010)


@respx.mock
async def test_any_other_error_in_a_200_reply_fails_the_call_and_never_shows_the_key(make_http: HttpFactory) -> None:
    body = {"error": f"Invalid API key {API_KEY}. Your API key should be here: https://serpapi.com/manage-api-key"}
    respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, json=body))

    with pytest.raises(ProviderError, match="SerpApi said: Invalid API key") as caught:
        await _adapter(make_http).find_venues(QUERY)

    assert API_KEY not in str(caught.value)
    assert API_KEY not in (caught.value.call.error or "")
    assert caught.value.call.status == "error"


@pytest.mark.parametrize("status", [400, 401, 403, 404])
@respx.mock
async def test_client_errors_are_not_retried_and_not_billed(
    make_http: HttpFactory, budget: Budget, status: int
) -> None:
    route = respx.get(SEARCH_URL).mock(return_value=httpx.Response(status, json={"error": "rejected"}))

    with pytest.raises(ProviderError) as caught:
        await _adapter(make_http).find_venues(QUERY)

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
        await _adapter(make_http).find_venues(QUERY)

    assert route.call_count == 2
    assert (caught.value.call.attempts, caught.value.call.http_status) == (2, status)
    assert budget.spent_usd == 0.0


@respx.mock
async def test_a_retry_that_succeeds_is_billed_once(make_http: HttpFactory, budget: Budget) -> None:
    busy = httpx.Response(429, json={"error": "slow down"})
    served = httpx.Response(200, json=load_fixture("serpapi_maps", "gay_bar_bangkok.json"))
    route = respx.get(SEARCH_URL).mock(side_effect=[busy, served])

    result = await _adapter(make_http).find_venues(QUERY)

    assert route.call_count == 2
    assert result.call.attempts == 2
    assert len(result.venues) == 5
    assert budget.spent_usd == pytest.approx(0.010)


@respx.mock(assert_all_called=False)
async def test_a_search_that_does_not_fit_the_budget_is_never_sent() -> None:
    route = respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, json={}))
    async with httpx.AsyncClient() as client:
        http = MeteredHttp(SerpApiMaps.name, SerpApiMaps.price, client, Budget(0.009))

        with pytest.raises(BudgetExceededError):
            await SerpApiMaps(http, api_key=API_KEY).find_venues(QUERY)

    assert route.call_count == 0


def test_the_class_attributes_and_the_price_follow_what_was_read_on_the_vendors_pages() -> None:
    assert SerpApiMaps.name == "serpapi_maps"
    assert SerpApiMaps.queryless is False
    assert SerpApiMaps.google_derived is True
    assert SerpApiMaps.price is PRICE
    assert SerpApiMaps.max_concurrency == 2
    assert SerpApiMaps.min_interval_s > 0
    assert SerpApiMaps.timeout_s > 30  # a search through SerpApi regularly takes longer than the default 30 s
    assert (PRICE.unit, PRICE.usd_per_unit, PRICE.verified_on) == ("search", 0.010, date(2026, 10, 8))
    assert PRICE.source_url == "https://serpapi.com/pricing"
