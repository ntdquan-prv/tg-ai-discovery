from collections.abc import Callable
from datetime import date

import httpx
import pytest
import respx

from tests.support import HttpFactory, load_fixture
from tg_discovery.domain.geo import BoundingBox
from tg_discovery.domain.models import SourceRef, VenueDraft
from tg_discovery.pipelines.common.budget import Budget
from tg_discovery.providers.base import BudgetExceededError, ProviderError, VenueProvider, VenueQuery
from tg_discovery.providers.maps.brave_place import PRICE, SEARCH_URL, BravePlace
from tg_discovery.providers.metering import MeteredHttp

API_KEY = "test-key-123"
AREA = BoundingBox(south=52.338245, west=13.088345, north=52.675509, east=13.761161)
QUERY = VenueQuery(text="gay bar", country="DE", city="Berlin", area=AREA, language="de", max_results=20)

# Pyright checks the protocol and the constructor the registry calls (the HTTP layer, then the key).
_conforms: Callable[[MeteredHttp, str], VenueProvider] = BravePlace


def _adapter(make_http: HttpFactory) -> BravePlace:
    return BravePlace(make_http(BravePlace.name, BravePlace.price), api_key=API_KEY)


def _serve(fixture: str = "gay_bar_berlin.json") -> respx.Route:
    return respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, json=load_fixture("brave_place", fixture)))


def _place(**fields: object) -> dict[str, object]:
    return {"type": "location_result", "title": "Test Venue", **fields}


async def _fetch(make_http: HttpFactory, *places: object) -> list[VenueDraft]:
    """Serve the given results through the mocked endpoint and return what the adapter made of them."""
    respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, json={"type": "locations", "results": list(places)}))
    return (await _adapter(make_http).find_venues(QUERY)).venues


@respx.mock
async def test_the_request_is_a_get_with_the_key_in_a_header_and_the_box_as_centre_and_radius(
    make_http: HttpFactory,
) -> None:
    route = _serve()

    await _adapter(make_http).find_venues(QUERY)

    assert route.call_count == 1
    request = route.calls.last.request
    assert request.method == "GET"
    assert str(request.url).split("?")[0] == SEARCH_URL
    assert request.headers["x-subscription-token"] == API_KEY
    assert request.headers["accept"] == "application/json"
    assert API_KEY not in str(request.url)
    lat, lng = AREA.center
    params = dict(request.url.params)
    assert params == {
        "q": "gay bar",
        "latitude": str(round(lat, 6)),
        "longitude": str(round(lng, 6)),
        "radius": str(round(AREA.radius_km * 1000)),
        "count": "20",
        "country": "DE",
        "search_lang": "de",
        "safesearch": "off",
    }
    # The radius is in metres and reaches from the centre to a corner of the box (about 29 km here).
    assert 25_000 < int(params["radius"]) < 35_000


@pytest.mark.parametrize(("max_results", "count"), [(1, 1), (20, 20), (100, 100), (200, 100)])
@respx.mock
async def test_the_count_follows_max_results_up_to_the_api_maximum_and_is_reported_as_requested(
    make_http: HttpFactory, max_results: int, count: int
) -> None:
    route = _serve()

    result = await _adapter(make_http).find_venues(QUERY.model_copy(update={"max_results": max_results}))

    assert route.calls.last.request.url.params["count"] == str(count)
    assert result.requested == count


@pytest.mark.parametrize(
    ("country", "language", "sent_country", "sent_language"),
    [
        ("DE", "de", "DE", "de"),
        ("de", "DE", "DE", "de"),
        ("TH", "th", "ALL", "th"),
        ("US", "xx", "US", None),
        ("ZZ", "zh", "ALL", None),
    ],
)
@respx.mock
async def test_country_and_language_are_only_sent_when_the_reference_lists_them(
    make_http: HttpFactory, country: str, language: str, sent_country: str, sent_language: str | None
) -> None:
    route = _serve()

    await _adapter(make_http).find_venues(QUERY.model_copy(update={"country": country, "language": language}))

    params = route.calls.last.request.url.params
    assert params["country"] == sent_country
    assert params.get("search_lang") == sent_language


@respx.mock
async def test_a_blank_search_text_leaves_out_q_to_get_places_in_general(make_http: HttpFactory) -> None:
    route = _serve()

    await _adapter(make_http).find_venues(QUERY.model_copy(update={"text": "  "}))

    assert "q" not in route.calls.last.request.url.params


@respx.mock
async def test_results_become_drafts_and_the_other_buckets_are_ignored(make_http: HttpFactory) -> None:
    _serve()

    result = await _adapter(make_http).find_venues(QUERY)

    assert result.provider == "brave_place"
    assert [venue.name for venue in result.venues] == [
        "Bar Lila",
        "Sauna Nordlicht",
        "Café Regenbogen",
        "Haus ohne Adresse",
        "Club Aurora",
    ]


@respx.mock
async def test_a_result_with_everything_gives_a_complete_draft(make_http: HttpFactory) -> None:
    _serve()

    result = await _adapter(make_http).find_venues(QUERY)

    bar = result.venues[0]
    assert bar.venue_type == "Bar"
    assert bar.address == "Motzstraße 24, 10777 Berlin"
    assert (bar.city, bar.country) == ("Berlin", "DE")
    assert (bar.lat, bar.lng) == (52.4989121, 13.3534876)
    assert bar.website == "https://www.bar-lila.example"
    assert bar.phone == "+49 30 1234567"
    assert bar.opening_hours == "Daily 20:00-02:00"
    assert (bar.lgbtq, bar.socials, bar.external_ids, bar.description) == (None, {}, {}, None)
    assert bar.sources == [
        SourceRef(
            url="https://www.bar-lila.example", title="Bar Lila", provider="brave_place", retrieved_at=result.call.at
        )
    ]
    assert bar.missing_fields == ["description"]


@respx.mock
async def test_a_listing_page_is_a_source_but_not_the_venues_website(make_http: HttpFactory) -> None:
    _serve()

    result = await _adapter(make_http).find_venues(QUERY)

    sauna = result.venues[1]
    assert sauna.website is None
    assert [source.url for source in sauna.sources] == ["https://www.yelp.com/biz/sauna-nordlicht-berlin"]
    assert sauna.venue_type == "Sauna"
    assert sauna.address == "Hauptstraße 5, 10827 Berlin"
    assert sauna.phone is None
    assert sauna.opening_hours == (
        "Tue 14:00-23:00; Wed 14:00-23:00; Thu 14:00-23:00; Fri 14:00-23:00; Sat 12:00-16:00, 18:00-23:00; "
        "Sun 12:00-22:00"
    )


@respx.mock
async def test_results_with_little_data_still_become_drafts(make_http: HttpFactory) -> None:
    _serve()

    result = await _adapter(make_http).find_venues(QUERY)

    cafe, bare, club = result.venues[2], result.venues[3], result.venues[4]
    # No displayAddress: the street address is used. "Deutschland" is no country code. Today's hours are not a week.
    assert (cafe.venue_type, cafe.address, cafe.city, cafe.country) == ("Cafe", "Fuggerstraße 19", "Berlin", None)
    assert (cafe.website, cafe.opening_hours) == ("https://cafe-regenbogen.example", None)
    assert bare.name == "Haus ohne Adresse"
    assert (bare.address, bare.city, bare.lat, bare.lng, bare.website, bare.phone, bare.venue_type) == (None,) * 7
    assert bare.sources == []
    # Coordinates out of range are dropped, a web address without a scheme gets one.
    assert (club.venue_type, club.lat, club.lng, club.website) == (
        "Dance Club",
        None,
        None,
        "https://club-aurora.example",
    )


@respx.mock
async def test_ratings_reviews_photos_contact_emails_and_place_ids_are_not_carried(make_http: HttpFactory) -> None:
    _serve()

    result = await _adapter(make_http).find_venues(QUERY)

    dumped = result.model_dump_json()
    for leftover in ("7731", "brave-thumb", "brave-original", "loc4AAAA", "info@bar-lila.example", "price"):
        assert leftover not in dumped


@respx.mock
async def test_a_search_is_billed_at_the_list_price(make_http: HttpFactory, budget: Budget) -> None:
    _serve()

    result = await _adapter(make_http).find_venues(QUERY)

    assert (result.call.provider, result.call.endpoint, result.call.status) == ("brave_place", "place_search", "ok")
    assert (result.call.units, result.call.attempts, result.call.http_status) == (1.0, 1, 200)
    assert result.call.cost_usd == pytest.approx(0.005)
    assert budget.spent_usd == pytest.approx(0.005)


@pytest.mark.parametrize("body", [{"type": "locations", "results": []}, {"type": "locations"}])
@respx.mock
async def test_a_search_without_places_gives_no_venues(make_http: HttpFactory, body: dict[str, object]) -> None:
    respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, json=body))

    result = await _adapter(make_http).find_venues(QUERY)

    assert result.venues == []
    assert result.call.cost_usd == pytest.approx(0.005)


@respx.mock
async def test_the_empty_fixture_gives_no_venues(make_http: HttpFactory) -> None:
    _serve("empty.json")

    assert (await _adapter(make_http).find_venues(QUERY)).venues == []


def _days(hours: dict[str, list[tuple[str, str]]]) -> dict[str, object]:
    """Weekly hours as Brave nests them: a list of days, each a list of slots."""
    return {
        "days": [
            [{"abbr_name": day, "full_name": day, "opens": opens, "closes": closes} for opens, closes in slots]
            for day, slots in hours.items()
        ]
    }


WEEK = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
OFFICE_HOURS = [("09:00", "17:00")]
SIX_OFFICE_DAYS = "; ".join(f"{day} 09:00-17:00" for day in WEEK[:6])


@pytest.mark.parametrize(
    ("hours", "expected"),
    [
        (_days(dict.fromkeys(WEEK, OFFICE_HOURS)), "Daily 09:00-17:00"),
        (
            _days({**dict.fromkeys(WEEK[:6], OFFICE_HOURS), "Sun": [("10:00", "14:00")]}),
            f"{SIX_OFFICE_DAYS}; Sun 10:00-14:00",
        ),
        (_days({"Mon": [("09:00", "12:00"), ("13:00", "17:00")]}), "Mon 09:00-12:00, 13:00-17:00"),
        # A flat list of slots works too, and a slot without a closing time is left out.
        (
            {
                "days": [
                    {"abbr_name": "Mon", "opens": "09:00", "closes": "17:00"},
                    {"abbr_name": "Tue", "opens": "10:00"},
                ]
            },
            "Mon 09:00-17:00",
        ),
        # Today's hours alone say nothing about the other days.
        ({"current_day": [{"abbr_name": "Thu", "opens": "09:00", "closes": "18:00"}]}, None),
        ({"days": []}, None),
        ({}, None),
        ("open", None),
        (None, None),
    ],
)
@respx.mock
async def test_weekly_hours_become_one_short_line(make_http: HttpFactory, hours: object, expected: str | None) -> None:
    (venue,) = await _fetch(make_http, _place(opening_hours=hours))

    assert venue.opening_hours == expected


@pytest.mark.parametrize(
    ("categories", "venue_type"),
    [
        (["Gay Bar", "Bar"], "Bar"),
        (["Cocktail Bars"], "Bar"),
        (["Pub"], "Bar"),
        (["Beach Bar"], "Bar"),
        (["Night Club"], "Dance Club"),
        (["Nightclubs", "Bars"], "Dance Club"),
        (["Coffee & Tea", "Cafe"], "Cafe"),
        (["Coffee Shop"], "Cafe"),
        (["Café"], "Cafe"),
        (["Italian Restaurant"], "Restaurant"),
        (["Sauna"], "Sauna"),
        (["Bathhouse"], "Sauna"),
        (["Gyms", "Fitness & Instruction"], "Gym"),
        (["Day Spas"], "Spa"),
        (["Hotel"], "Hotels"),
        (["Hostels"], "Hotels"),
        (["Beaches"], "Beach"),
        (["Book Store"], "Shop"),
        (["Barber Shop"], None),
        (["Barbecue"], None),
        (["Mediterranean", "Greek"], None),
        ([], None),
    ],
)
@respx.mock
async def test_clear_categories_give_a_travelgay_venue_type(
    make_http: HttpFactory, categories: list[str], venue_type: str | None
) -> None:
    (venue,) = await _fetch(make_http, _place(categories=categories))

    assert venue.venue_type == venue_type


@pytest.mark.parametrize(
    ("url", "website"),
    [
        ("https://bluebottle.example/?utm_source=brave&fbclid=1#top", "https://bluebottle.example"),
        ("bluebottle.example/menu", "https://bluebottle.example/menu"),
        ("https://www.yelp.com/biz/blue-bottle-sf", None),
        ("https://www.facebook.com/bluebottle", None),
        ("https://www.tripadvisor.co.uk/Restaurant_Review-g186338-d1", None),
        ("not a url", None),
        (None, None),
    ],
)
@respx.mock
async def test_only_the_venues_own_site_is_its_website(
    make_http: HttpFactory, url: str | None, website: str | None
) -> None:
    (venue,) = await _fetch(make_http, _place(url=url))

    assert venue.website == website


@pytest.mark.parametrize(
    ("fields", "source_url"),
    [
        ({"url": "https://a.example", "provider_url": "https://www.yelp.com/biz/a"}, "https://a.example"),
        (
            {"url": "https://www.yelp.com/biz/a", "provider_url": "https://www.yelp.com/biz/b"},
            "https://www.yelp.com/biz/a",
        ),
        ({"provider_url": "https://www.yelp.com/biz/b"}, "https://www.yelp.com/biz/b"),
    ],
)
@respx.mock
async def test_the_source_is_the_page_that_brave_names(
    make_http: HttpFactory, fields: dict[str, object], source_url: str
) -> None:
    (venue,) = await _fetch(make_http, _place(**fields))

    assert [source.url for source in venue.sources] == [source_url]
    assert [(source.provider, source.title) for source in venue.sources] == [("brave_place", "Test Venue")]


@pytest.mark.parametrize("fields", [{}, {"url": "not a url", "provider_url": "not a url either"}])
@respx.mock
async def test_a_result_without_a_page_has_no_source_rather_than_a_made_up_one(
    make_http: HttpFactory, fields: dict[str, object]
) -> None:
    (venue,) = await _fetch(make_http, _place(**fields))

    assert venue.sources == []


@pytest.mark.parametrize(
    ("coordinates", "position"),
    [
        ([52.5, 13.4], (52.5, 13.4)),
        ([52, 13], (52.0, 13.0)),
        ([13.4, 252.5], (None, None)),
        ([91.0, 13.4], (None, None)),
        ([52.5], (None, None)),
        ([True, 13.4], (None, None)),
        ("52.5,13.4", (None, None)),
        (None, (None, None)),
    ],
)
@respx.mock
async def test_coordinates_are_latitude_then_longitude(
    make_http: HttpFactory, coordinates: object, position: tuple[float | None, float | None]
) -> None:
    (venue,) = await _fetch(make_http, _place(coordinates=coordinates))

    assert (venue.lat, venue.lng) == position


@pytest.mark.parametrize(
    ("country", "code"), [("DE", "DE"), ("de", "DE"), ("Deutschland", None), (5, None), (None, None)]
)
@respx.mock
async def test_the_country_is_kept_only_as_an_alpha_2_code(
    make_http: HttpFactory, country: object, code: str | None
) -> None:
    (venue,) = await _fetch(make_http, _place(postal_address={"country": country}))

    assert venue.country == code


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
    assert caught.value.call.cost_usd == pytest.approx(0.005)
    assert budget.spent_usd == pytest.approx(0.005)


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
@respx.mock
async def test_client_errors_are_not_retried_and_not_billed(
    make_http: HttpFactory, budget: Budget, status: int
) -> None:
    route = respx.get(SEARCH_URL).mock(return_value=httpx.Response(status, text="rejected"))

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
    route = respx.get(SEARCH_URL).mock(return_value=httpx.Response(status, text="busy"))

    with pytest.raises(ProviderError) as caught:
        await _adapter(make_http).find_venues(QUERY)

    assert route.call_count == 2
    assert (caught.value.call.attempts, caught.value.call.http_status) == (2, status)
    assert budget.spent_usd == 0.0


@respx.mock
async def test_a_retry_that_succeeds_is_billed_once(make_http: HttpFactory, budget: Budget) -> None:
    busy = httpx.Response(429, text="slow down")
    served = httpx.Response(200, json=load_fixture("brave_place", "gay_bar_berlin.json"))
    route = respx.get(SEARCH_URL).mock(side_effect=[busy, served])

    result = await _adapter(make_http).find_venues(QUERY)

    assert route.call_count == 2
    assert result.call.attempts == 2
    assert len(result.venues) == 5
    assert budget.spent_usd == pytest.approx(0.005)


@respx.mock(assert_all_called=False)
async def test_a_search_that_does_not_fit_the_budget_is_never_sent() -> None:
    route = respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, json={"results": []}))
    async with httpx.AsyncClient() as client:
        http = MeteredHttp(BravePlace.name, BravePlace.price, client, Budget(0.004))

        with pytest.raises(BudgetExceededError):
            await BravePlace(http, api_key=API_KEY).find_venues(QUERY)

    assert route.call_count == 0


def test_the_class_attributes_and_the_price_follow_what_was_read_on_the_vendors_pages() -> None:
    assert BravePlace.queryless is False
    assert BravePlace.google_derived is False
    assert BravePlace.max_concurrency == 1
    assert BravePlace.min_interval_s >= 1.0
    assert BravePlace.price is PRICE
    assert PRICE.usd_per_unit == 0.005
    assert PRICE.verified_on == date(2026, 10, 8)
    assert PRICE.source_url.startswith("https://brave.com/search/api")
