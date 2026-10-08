from typing import cast

import httpx
import pytest
import respx

from tests.support import HttpFactory, load_fixture
from tg_discovery.domain.geo import BoundingBox
from tg_discovery.pipelines.common.budget import Budget
from tg_discovery.providers.base import ProviderError
from tg_discovery.providers.opendata.nominatim import PRICE, SEARCH_URL, GeocodedCity, Nominatim

USER_AGENT = "tg-discovery-tests/0.1 (https://example.test/contact)"


def _adapter(make_http: HttpFactory) -> Nominatim:
    return Nominatim(make_http(Nominatim.name, Nominatim.price), user_agent=USER_AGENT)


def _serve(fixture: str) -> respx.Route:
    return respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, json=load_fixture("nominatim", fixture)))


def _berlin() -> dict[str, object]:
    return cast(list[dict[str, object]], load_fixture("nominatim", "berlin.json"))[0]


@respx.mock
async def test_the_request_is_one_structured_search_for_a_single_match_in_the_country(make_http: HttpFactory) -> None:
    route = _serve("berlin.json")

    await _adapter(make_http).geocode_city("Berlin", "DE")

    assert route.call_count == 1
    request = route.calls.last.request
    assert request.method == "GET"
    assert str(request.url).split("?")[0] == SEARCH_URL
    # A structured query cannot be combined with q, and countrycodes is the hard filter on the country.
    assert dict(request.url.params) == {"city": "Berlin", "countrycodes": "de", "format": "jsonv2", "limit": "1"}
    assert request.headers["user-agent"] == USER_AGENT


@respx.mock
async def test_the_bounding_box_is_read_as_south_north_west_east(make_http: HttpFactory) -> None:
    _serve("berlin.json")

    city = await _adapter(make_http).geocode_city("Berlin", "DE")

    assert city is not None
    assert city == GeocodedCity(
        display_name="Berlin, Deutschland",
        center_lat=52.5198535,
        center_lng=13.4385964,
        bbox=BoundingBox(south=52.3382448, west=13.088345, north=52.6755087, east=13.7611609),
        osm_type="relation",
        osm_id=62422,
    )
    assert city.bbox.contains(city.center_lat, city.center_lng)


@respx.mock
async def test_a_place_that_nominatim_does_not_know_gives_none(make_http: HttpFactory) -> None:
    _serve("empty.json")

    assert await _adapter(make_http).geocode_city("Atlantis", "DE") is None


@respx.mock
async def test_input_is_trimmed_and_the_country_code_is_sent_in_lower_case(make_http: HttpFactory) -> None:
    route = _serve("berlin.json")

    await _adapter(make_http).geocode_city("  Zürich ", " ch")

    assert dict(route.calls.last.request.url.params)["city"] == "Zürich"
    assert dict(route.calls.last.request.url.params)["countrycodes"] == "ch"


@respx.mock
async def test_a_string_osm_id_and_a_missing_display_name_are_tolerated(make_http: HttpFactory) -> None:
    item = {**_berlin(), "osm_id": "107775", "osm_type": "node"}
    del item["display_name"]
    respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, json=[item]))

    city = await _adapter(make_http).geocode_city("Berlin", "DE")

    assert city is not None
    assert (city.osm_type, city.osm_id, city.display_name) == ("node", 107775, "Berlin")


@pytest.mark.parametrize(
    "change",
    [
        {"lat": None},
        {"lon": "east"},
        {"boundingbox": ["52.3", "52.6", "13.0"]},
        {"boundingbox": ["52.3", "52.6", "13.0", None]},
        {"boundingbox": ["52.3", "52.6", "13.0", "400"]},
        {"osm_type": "area"},
        {"osm_id": "abc"},
        {"osm_id": 12.5},
        {"lat": "95.0"},
    ],
)
@respx.mock
async def test_a_match_without_a_usable_position_or_id_is_a_failed_call(
    make_http: HttpFactory, change: dict[str, object]
) -> None:
    respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, json=[{**_berlin(), **change}]))

    with pytest.raises(ProviderError, match="no usable position") as caught:
        await _adapter(make_http).geocode_city("Berlin", "DE")

    assert caught.value.call.status == "error"


@pytest.mark.parametrize("body", ["<html>maintenance</html>", "{}"])
@respx.mock
async def test_a_body_that_is_not_a_json_array_is_a_failed_call(make_http: HttpFactory, body: str) -> None:
    respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, text=body))

    with pytest.raises(ProviderError, match=r"not JSON|not a JSON array"):
        await _adapter(make_http).geocode_city("Berlin", "DE")


@respx.mock
async def test_a_lookup_costs_nothing(make_http: HttpFactory, budget: Budget) -> None:
    _serve("berlin.json")

    await _adapter(make_http).geocode_city("Berlin", "DE")

    assert budget.spent_usd == 0.0


@pytest.mark.parametrize("status", [400, 401, 403])
@respx.mock
async def test_client_errors_and_blocks_are_not_retried(make_http: HttpFactory, status: int) -> None:
    route = respx.get(SEARCH_URL).mock(return_value=httpx.Response(status, text="blocked"))

    with pytest.raises(ProviderError) as caught:
        await _adapter(make_http).geocode_city("Berlin", "DE")

    assert route.call_count == 1
    assert (caught.value.call.attempts, caught.value.call.http_status) == (1, status)
    assert (caught.value.call.endpoint, caught.value.call.cost_usd) == ("search", 0.0)


@pytest.mark.parametrize("status", [429, 500, 503])
@respx.mock
async def test_rate_limiting_and_server_errors_are_retried_once(make_http: HttpFactory, status: int) -> None:
    route = respx.get(SEARCH_URL).mock(return_value=httpx.Response(status, text="busy"))

    with pytest.raises(ProviderError) as caught:
        await _adapter(make_http).geocode_city("Berlin", "DE")

    assert route.call_count == 2
    assert (caught.value.call.attempts, caught.value.call.http_status) == (2, status)


@respx.mock
async def test_a_retry_that_succeeds_returns_the_city(make_http: HttpFactory) -> None:
    busy = httpx.Response(503, text="busy")
    berlin = httpx.Response(200, json=load_fixture("nominatim", "berlin.json"))
    route = respx.get(SEARCH_URL).mock(side_effect=[busy, berlin])

    city = await _adapter(make_http).geocode_city("Berlin", "DE")

    assert route.call_count == 2
    assert city is not None
    assert city.osm_id == 62422


@pytest.mark.parametrize(
    ("city", "country"),
    [("", "DE"), ("   ", "DE"), ("Berlin", ""), ("Berlin", "D"), ("Berlin", "DEU"), ("Berlin", "1D"), ("Berlin", "Dé")],
)
@respx.mock
async def test_a_blank_city_or_a_country_that_is_not_alpha_2_is_refused_before_any_request(
    make_http: HttpFactory, city: str, country: str
) -> None:
    with pytest.raises(ValueError, match="ISO 3166-1 alpha-2"):
        await _adapter(make_http).geocode_city(city, country)


@pytest.mark.parametrize("user_agent", ["", "   "])
async def test_a_blank_user_agent_is_refused(make_http: HttpFactory, user_agent: str) -> None:
    with pytest.raises(ValueError, match="User-Agent"):
        Nominatim(make_http(Nominatim.name, Nominatim.price), user_agent=user_agent)


def test_the_class_attributes_follow_the_usage_policy() -> None:
    assert Nominatim.google_derived is False
    assert Nominatim.max_concurrency == 1
    assert Nominatim.min_interval_s >= 1.0
    assert Nominatim.price is PRICE
    assert PRICE.usd_per_unit == 0.0
    assert PRICE.verified_on is not None
