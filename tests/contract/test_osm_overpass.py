from collections.abc import Callable
from urllib.parse import parse_qs

import httpx
import pytest
import respx

from tests.support import HttpFactory, load_fixture
from tg_discovery.domain.enums import LgbtqLevel
from tg_discovery.domain.geo import BoundingBox
from tg_discovery.domain.models import Evidence, LgbtqAssessment, SourceRef, VenueDraft
from tg_discovery.pipelines.common.budget import Budget
from tg_discovery.providers.base import ProviderError, VenueProvider, VenueQuery
from tg_discovery.providers.metering import MeteredHttp
from tg_discovery.providers.opendata.osm_overpass import INTERPRETER_URL, ODBL_ATTRIBUTION, PRICE, OsmOverpass

USER_AGENT = "tg-discovery-tests/0.1 (https://example.test/contact)"
AREA = BoundingBox(south=52.338245, west=13.088345, north=52.675509, east=13.761161)
QUERY = VenueQuery(text="ignored", country="DE", city="Berlin", area=AREA)

# Pyright checks the protocol and the constructor the registry calls (the HTTP layer, then the identity).
_conforms: Callable[[MeteredHttp, str], VenueProvider] = OsmOverpass


def _adapter(make_http: HttpFactory) -> OsmOverpass:
    return OsmOverpass(make_http(OsmOverpass.name, OsmOverpass.price), user_agent=USER_AGENT)


def _node(tags: dict[str, str], ident: int = 1) -> dict[str, object]:
    return {"type": "node", "id": ident, "lat": 52.5, "lon": 13.4, "tags": {"name": "Test Venue", **tags}}


def _serve(fixture: str) -> respx.Route:
    return respx.post(INTERPRETER_URL).mock(
        return_value=httpx.Response(200, json=load_fixture("osm_overpass", fixture))
    )


async def _fetch(make_http: HttpFactory, *elements: object) -> list[VenueDraft]:
    """Serve the given elements through the mocked interpreter and return what the adapter made of them."""
    respx.post(INTERPRETER_URL).mock(return_value=httpx.Response(200, json={"elements": list(elements)}))
    return (await _adapter(make_http).find_venues(QUERY)).venues


@respx.mock
async def test_the_request_is_a_post_with_the_query_in_a_form_body(make_http: HttpFactory) -> None:
    route = _serve("empty.json")

    await _adapter(make_http).find_venues(QUERY)

    assert route.call_count == 1
    request = route.calls.last.request
    assert request.method == "POST"
    assert str(request.url) == INTERPRETER_URL
    assert request.headers["user-agent"] == USER_AGENT
    assert request.headers["content-type"] == "application/x-www-form-urlencoded"
    form = parse_qs(request.content.decode())
    assert list(form) == ["data"]
    # The box is south, west, north, east; the search text of the query plays no part.
    assert form["data"][0].splitlines() == [
        "[out:json][timeout:25];",
        'nwr["lgbtq"~"^(friendly|only|primary|welcome|yes)$"](52.338245,13.088345,52.675509,13.761161);',
        "out center tags;",
    ]


@respx.mock
async def test_a_node_becomes_a_draft_with_everything_the_tags_give(make_http: HttpFactory) -> None:
    _serve("berlin.json")

    result = await _adapter(make_http).find_venues(QUERY)

    bar = result.venues[0]
    assert bar.name == "Bar Lila"
    assert bar.venue_type == "Bars"
    assert bar.address == "Motzstraße 24, 10777 Berlin"
    assert (bar.city, bar.country) == ("Berlin", "DE")
    assert (bar.lat, bar.lng) == (52.4989121, 13.3534876)
    assert bar.website == "https://www.bar-lila.example"
    assert bar.phone == "+49 30 1234567"
    assert bar.opening_hours == "Mo-Th 20:00-02:00; Fr,Sa 20:00-05:00"
    # The Facebook value is a bare account name, which is no link.
    assert bar.socials == {"instagram": "https://www.instagram.com/bar_lila"}
    assert bar.external_ids == {"osm": "node/1001"}
    assert bar.lgbtq == LgbtqAssessment(
        level=LgbtqLevel.PRIMARY,
        evidence=[Evidence(quote="lgbtq=primary", url="https://www.openstreetmap.org/node/1001")],
    )
    assert bar.sources == [
        SourceRef(url="https://www.openstreetmap.org/node/1001", provider="osm_overpass", retrieved_at=result.call.at)
    ]
    assert bar.missing_fields == ["description"]


@respx.mock
async def test_ways_and_relations_are_placed_at_their_centre(make_http: HttpFactory) -> None:
    _serve("berlin.json")

    venues = (await _adapter(make_http).find_venues(QUERY)).venues

    club, hotel, sauna = venues[1], venues[2], venues[3]
    assert (club.name, club.venue_type, club.external_ids) == ("Club Aurora", "Dance Club", {"osm": "way/2002"})
    assert (club.lat, club.lng) == (52.5011, 13.4208)
    assert club.address == "Köpenicker Straße 70, Berlin"
    assert club.country is None
    assert club.website == "https://club-aurora.example"
    assert club.phone == "+49 30 7654321"
    assert club.lgbtq is not None
    assert (club.lgbtq.level, club.lgbtq.evidence[0].quote) == (LgbtqLevel.PRIMARY, "lgbtq=yes")
    assert (hotel.name, hotel.venue_type, hotel.external_ids) == ("Hotel Zur Sonne", None, {"osm": "relation/3003"})
    assert (hotel.lat, hotel.lng) == (52.5163, 13.3777)
    assert hotel.lgbtq is not None
    assert hotel.lgbtq.level is LgbtqLevel.FRIENDLY
    assert (sauna.venue_type, sauna.address) == ("Saunas", "Hauptstraße 5, Berlin")
    assert sauna.lgbtq is not None
    assert (sauna.lgbtq.level, sauna.lgbtq.evidence[0].quote) == (LgbtqLevel.PRIMARY, "lgbtq=only")


@respx.mock
async def test_nameless_elements_are_skipped_and_namesakes_are_all_kept(make_http: HttpFactory) -> None:
    _serve("berlin.json")

    venues = (await _adapter(make_http).find_venues(QUERY)).venues

    assert [venue.name for venue in venues] == [
        "Bar Lila",
        "Club Aurora",
        "Hotel Zur Sonne",
        "Sauna Nordlicht",
        "Bar Lila",
        "Queer Archiv",
    ]
    assert [venue.external_ids["osm"] for venue in venues] == [
        "node/1001",
        "way/2002",
        "relation/3003",
        "node/1004",
        "node/1006",
        "relation/3007",
    ]


@respx.mock
async def test_elements_with_little_data_leave_the_other_fields_empty(make_http: HttpFactory) -> None:
    _serve("berlin.json")

    venues = (await _adapter(make_http).find_venues(QUERY)).venues

    namesake, archive = venues[4], venues[5]
    assert (namesake.address, namesake.city, namesake.website, namesake.phone, namesake.opening_hours) == (None,) * 5
    assert namesake.socials == {}
    assert namesake.missing_fields == ["address", "website", "phone", "opening_hours", "description"]
    assert namesake.lgbtq is not None
    assert namesake.lgbtq.level is LgbtqLevel.FRIENDLY
    # A relation without a centre has no position, and a mailto: address is not a website.
    assert (archive.lat, archive.lng, archive.website, archive.venue_type) == (None, None, None, None)
    assert archive.lgbtq is not None
    assert (archive.lgbtq.level, archive.lgbtq.evidence[0].quote) == (LgbtqLevel.FRIENDLY, "lgbtq=friendly")


@pytest.mark.parametrize(
    ("value", "level"),
    [
        ("primary", LgbtqLevel.PRIMARY),
        ("yes", LgbtqLevel.PRIMARY),
        ("only", LgbtqLevel.PRIMARY),
        ("welcome", LgbtqLevel.FRIENDLY),
        ("friendly", LgbtqLevel.FRIENDLY),
        ("Primary", LgbtqLevel.PRIMARY),
        ("maybe", None),
    ],
)
@respx.mock
async def test_the_lgbtq_tag_gives_the_level_and_the_evidence(
    make_http: HttpFactory, value: str, level: LgbtqLevel | None
) -> None:
    (venue,) = await _fetch(make_http, _node({"lgbtq": value}, ident=42))

    if level is None:
        assert venue.lgbtq is None
        return
    assert venue.lgbtq == LgbtqAssessment(
        level=level, evidence=[Evidence(quote=f"lgbtq={value.lower()}", url="https://www.openstreetmap.org/node/42")]
    )


@pytest.mark.parametrize(
    ("tags", "venue_type"),
    [
        ({"amenity": "bar"}, "Bars"),
        ({"amenity": "pub"}, "Bars"),
        ({"amenity": "nightclub"}, "Dance Club"),
        ({"amenity": "bar;nightclub"}, "Dance Club"),
        ({"amenity": "cafe"}, "Cafe"),
        ({"amenity": "restaurant"}, "Restaurant"),
        ({"leisure": "sauna"}, "Saunas"),
        ({"leisure": "fitness_centre"}, "Gyms"),
        ({"amenity": "spa"}, "Massage"),
        ({"shop": "beauty", "beauty": "spa"}, "Massage"),
        ({"shop": "massage"}, "Massage"),
        ({"shop": "erotic"}, "Adults"),
        ({"amenity": "bar;stripclub"}, "Adults"),
        ({"tourism": "museum"}, "Attractions"),
        ({"natural": "beach"}, "Attractions"),
        ({"tourism": "hostel"}, None),  # places to stay are not a TravelGay venue type
        ({"shop": "clothes"}, None),
        ({"amenity": "community_centre"}, None),
        ({}, None),
    ],
)
@respx.mock
async def test_clear_tags_give_a_travelgay_venue_type(
    make_http: HttpFactory, tags: dict[str, str], venue_type: str | None
) -> None:
    (venue,) = await _fetch(make_http, _node(tags))

    assert venue.venue_type == venue_type


@pytest.mark.parametrize(
    ("tags", "address"),
    [
        (
            {"addr:street": "Motzstraße", "addr:housenumber": "24", "addr:postcode": "10777", "addr:city": "Berlin"},
            "Motzstraße 24, 10777 Berlin",
        ),
        ({"addr:street": "Motzstraße", "addr:housenumber": "24"}, "Motzstraße 24"),
        ({"addr:place": "Dorfplatz", "addr:housenumber": "1", "addr:village": "Kleindorf"}, "Dorfplatz 1, Kleindorf"),
        ({"addr:full": "Hauptstraße 5, Berlin", "addr:housenumber": "5"}, "Hauptstraße 5, Berlin"),
        ({"addr:housenumber": "24", "addr:city": "Berlin"}, None),
        ({}, None),
    ],
)
@respx.mock
async def test_the_address_comes_from_the_addr_tags(
    make_http: HttpFactory, tags: dict[str, str], address: str | None
) -> None:
    (venue,) = await _fetch(make_http, _node(tags))

    assert venue.address == address


@pytest.mark.parametrize(
    ("tags", "website"),
    [
        ({"website": "https://a.example/menu?utm_source=osm&fbclid=1#top"}, "https://a.example/menu"),
        ({"website": "a.example"}, "https://a.example"),
        ({"website": "https://a.example; https://b.example"}, "https://a.example"),
        ({"website": "mailto:info@a.example", "contact:website": "https://b.example"}, "https://b.example"),
        ({"website": "mailto:info@a.example"}, None),
        ({"website": "not a url"}, None),
        ({}, None),
    ],
)
@respx.mock
async def test_the_website_is_cleaned_and_dropped_when_it_is_no_web_address(
    make_http: HttpFactory, tags: dict[str, str], website: str | None
) -> None:
    (venue,) = await _fetch(make_http, _node(tags))

    assert venue.website == website


@respx.mock
async def test_social_links_are_kept_but_bare_account_names_are_not(make_http: HttpFactory) -> None:
    tags = {
        "contact:facebook": "https://www.facebook.com/barlila?fbclid=abc",
        "contact:instagram": "instagram.com/bar_lila",
        "contact:twitter": "@barlila",
        "contact:tiktok": "bar.lila",
    }

    (venue,) = await _fetch(make_http, _node(tags))

    assert venue.socials == {
        "facebook": "https://www.facebook.com/barlila",
        "instagram": "https://instagram.com/bar_lila",
    }


@respx.mock
async def test_malformed_elements_are_skipped_or_lose_only_the_field_that_is_wrong(make_http: HttpFactory) -> None:
    venues = await _fetch(
        make_http,
        "junk",
        {"type": "node", "tags": {"name": "No Id"}},
        {"type": "area", "id": 1, "tags": {"name": "Not A Node Way Or Relation"}},
        {"type": "node", "id": True, "tags": {"name": "Boolean Id"}},
        {"type": ["node"], "id": 2, "tags": {"name": "Type Is A List"}},
        {"type": "node", "id": 3, "lat": 95.0, "lon": 13.4, "tags": {"name": "Off The Map"}},
        {"type": "node", "id": 4, "lat": 52.5, "tags": {"name": "Half A Position"}},
        {"type": "node", "id": 5, "lat": "x", "lon": "y", "tags": {"name": "Bad Numbers"}},
        {"type": "node", "id": 6, "lat": 52.5, "lon": 13.4, "tags": "not a mapping"},
        {
            "type": "node",
            "id": 7,
            "lat": 52.5,
            "lon": 13.4,
            "tags": {"name": "  Padded  ", "lgbtq": "primary", "addr:country": "Germany", "opening_hours": 5},
        },
    )

    assert [venue.name for venue in venues] == ["Off The Map", "Half A Position", "Bad Numbers", "Padded"]
    assert [(venue.lat, venue.lng) for venue in venues] == [(None, None)] * 3 + [(52.5, 13.4)]
    assert (venues[3].country, venues[3].opening_hours) == (None, None)


@respx.mock
async def test_the_call_is_free_and_the_result_has_no_cap(make_http: HttpFactory, budget: Budget) -> None:
    _serve("berlin.json")

    result = await _adapter(make_http).find_venues(QUERY)

    assert result.provider == "osm_overpass"
    assert result.requested is None
    assert (result.call.provider, result.call.endpoint, result.call.status) == ("osm_overpass", "interpreter", "ok")
    assert (result.call.units, result.call.cost_usd, result.call.attempts) == (1.0, 0.0, 1)
    assert budget.spent_usd == 0.0


@respx.mock
async def test_an_area_without_tagged_objects_gives_no_venues(make_http: HttpFactory) -> None:
    _serve("empty.json")

    result = await _adapter(make_http).find_venues(QUERY)

    assert result.venues == []
    assert result.call.status == "ok"


@respx.mock
async def test_a_timeout_reported_in_the_body_is_a_failed_call_not_an_empty_area(make_http: HttpFactory) -> None:
    _serve("runtime_error.json")

    with pytest.raises(ProviderError, match="Query timed out") as caught:
        await _adapter(make_http).find_venues(QUERY)

    assert (caught.value.call.status, caught.value.call.http_status) == ("error", 200)
    assert caught.value.call.error is not None
    assert caught.value.call.error.startswith("runtime error: Query timed out")


@pytest.mark.parametrize("body", ["<html>maintenance</html>", "[]"])
@respx.mock
async def test_a_body_that_is_not_a_json_object_is_a_failed_call(make_http: HttpFactory, body: str) -> None:
    respx.post(INTERPRETER_URL).mock(return_value=httpx.Response(200, text=body))

    with pytest.raises(ProviderError, match=r"not JSON|not a JSON object") as caught:
        await _adapter(make_http).find_venues(QUERY)

    assert caught.value.call.status == "error"


@pytest.mark.parametrize("status", [400, 401, 403, 406])
@respx.mock
async def test_client_errors_are_not_retried(make_http: HttpFactory, budget: Budget, status: int) -> None:
    route = respx.post(INTERPRETER_URL).mock(return_value=httpx.Response(status, text="no"))

    with pytest.raises(ProviderError) as caught:
        await _adapter(make_http).find_venues(QUERY)

    assert route.call_count == 1
    assert (caught.value.call.attempts, caught.value.call.http_status) == (1, status)
    assert (caught.value.call.status, caught.value.call.cost_usd) == ("error", 0.0)
    assert budget.spent_usd == 0.0


@pytest.mark.parametrize("status", [429, 500, 502, 504])
@respx.mock
async def test_rate_limiting_and_server_errors_are_retried_once(make_http: HttpFactory, status: int) -> None:
    route = respx.post(INTERPRETER_URL).mock(return_value=httpx.Response(status, text="busy"))

    with pytest.raises(ProviderError) as caught:
        await _adapter(make_http).find_venues(QUERY)

    assert route.call_count == 2
    assert (caught.value.call.attempts, caught.value.call.http_status) == (2, status)


@respx.mock
async def test_a_retry_that_succeeds_returns_the_venues(make_http: HttpFactory) -> None:
    busy, berlin = (
        httpx.Response(504, text="busy"),
        httpx.Response(200, json=load_fixture("osm_overpass", "berlin.json")),
    )
    route = respx.post(INTERPRETER_URL).mock(side_effect=[busy, berlin])

    result = await _adapter(make_http).find_venues(QUERY)

    assert route.call_count == 2
    assert result.call.attempts == 2
    assert len(result.venues) == 6


@pytest.mark.parametrize("user_agent", ["", "   "])
async def test_a_blank_user_agent_is_refused(make_http: HttpFactory, user_agent: str) -> None:
    with pytest.raises(ValueError, match="User-Agent"):
        OsmOverpass(make_http(OsmOverpass.name, OsmOverpass.price), user_agent=user_agent)


def test_the_class_attributes_follow_the_usage_policy() -> None:
    assert OsmOverpass.queryless is True
    assert OsmOverpass.google_derived is False
    assert OsmOverpass.max_concurrency == 1
    assert OsmOverpass.min_interval_s > 0
    assert OsmOverpass.price is PRICE
    assert PRICE.usd_per_unit == 0.0
    assert PRICE.verified_on is not None
    assert "ODbL" in ODBL_ATTRIBUTION
    assert "openstreetmap.org/copyright" in ODBL_ATTRIBUTION
