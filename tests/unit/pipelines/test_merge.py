from datetime import timedelta

from tests.pipeline_builders import CITY, TODAY, batch, event, venue
from tg_discovery.domain.enums import EventStatus, LgbtqLevel, OperatingStatus
from tg_discovery.domain.fingerprint import event_fingerprint
from tg_discovery.domain.models import MAX_EVENT_IMAGES, EventDraft
from tg_discovery.pipelines.common.merge import (
    event_content_hash,
    group_events,
    group_venues,
    merge_event,
    merge_venue,
    venue_content_hash,
)
from tg_discovery.pipelines.discovery import prepare_events, prepare_venues

SCOPE = CITY.scope


def test_venues_that_share_an_id_are_one_venue() -> None:
    maps = ("serpapi_maps", venue("Heaven", external_ids={"google_place_id": "g1"}, website=None, lat=10.0, lng=20.0))
    again = (
        "exa",
        venue("Heaven Bar Lounge", external_ids={"google_place_id": "g1"}, website=None, lat=None, lng=None),
    )
    other = ("osm_overpass", venue("Elsewhere", website="https://elsewhere.example", lat=10.05, lng=20.05))

    groups = group_venues([maps, again, other], SCOPE)

    assert sorted(len(group) for group in groups) == [1, 2]


def test_a_draft_without_a_position_joins_the_venue_of_the_same_name() -> None:
    located = ("serpapi_maps", venue("Julius'", website=None, lat=10.0, lng=20.0))
    text_only = ("exa", venue("Julius'", website=None, lat=None, lng=None))
    different = ("exa", venue("Eagle", website=None, lat=None, lng=None))

    groups = group_venues([located, text_only, different], SCOPE)

    assert sorted(len(group) for group in groups) == [1, 2]


def test_two_located_venues_of_one_name_in_different_places_stay_apart() -> None:
    one = ("serpapi_maps", venue("Flaming Saddles", website=None, lat=10.00, lng=20.00))
    two = ("serpapi_maps", venue("Flaming Saddles", website=None, lat=10.08, lng=20.08))

    assert len(group_venues([one, two], SCOPE)) == 2


def test_a_merged_venue_keeps_the_fullest_draft_and_fills_its_gaps() -> None:
    osm = (
        "osm_overpass",
        venue(
            "Stonewall Inn",
            website="https://stonewall.example",
            address=None,
            phone="+1 555",
            venue_type=None,
            lat=None,
            lng=None,
        ),
    )
    maps = (
        "serpapi_maps",
        venue("Stonewall Inn", address="53 Christopher St", website=None, lat=10.01, lng=20.01).model_copy(
            update={"price_text": "$$"}
        ),
    )
    exa = ("exa", venue("Stonewall Inn", website=None, address=None, lat=None, lng=None, description="A historic bar."))

    merged = merge_venue([exa, osm, maps])

    assert merged.address == "53 Christopher St"
    assert merged.website == "https://stonewall.example"
    assert merged.phone == "+1 555"
    assert merged.venue_type == "Bars"
    assert (merged.lat, merged.lng) == (10.01, 20.01)
    assert merged.description == "A historic bar."
    assert merged.price_text == "$$"
    assert len(merged.sources) == 1  # the builders give every draft the same page


def test_the_merge_takes_the_strongest_level_the_quotes_and_the_worst_news() -> None:
    sure = ("osm_overpass", venue(level=LgbtqLevel.PRIMARY))
    unsure = ("exa", venue(level=LgbtqLevel.UNCLEAR, quote=False, status=OperatingStatus.PERMANENTLY_CLOSED))

    merged = merge_venue([unsure, sure])

    assert merged.lgbtq is not None
    assert merged.lgbtq.level is LgbtqLevel.PRIMARY
    assert len(merged.lgbtq.evidence) == 1
    assert merged.operating_status is OperatingStatus.PERMANENTLY_CLOSED


def test_a_no_backed_by_a_quote_outweighs_a_guess() -> None:
    denied = ("exa", venue(level=LgbtqLevel.NO))
    guess = ("osm_overpass", venue(level=LgbtqLevel.PRIMARY, quote=False))

    merged = merge_venue([guess, denied])

    assert merged.lgbtq is not None
    assert merged.lgbtq.level is LgbtqLevel.NO


def test_events_with_the_same_title_and_venue_are_one_event_whatever_the_date() -> None:
    first = ("exa", event("Drag Brunch", days_ahead=5))
    second = ("exa", event("Drag Brunch", days_ahead=12))
    other = ("exa", event("Pride March", venue_name="City Hall", days_ahead=5))

    groups = group_events([first, second, other], SCOPE)

    assert sorted(len(group) for group in groups) == [1, 2]


def test_events_with_similar_titles_at_the_same_venue_merge() -> None:
    one = ("exa", event("Silom Drag Marathon 2026"))
    two = ("serpapi_events", event("Silom Drag Marathon", official=None))

    assert len(group_events([one, two], SCOPE)) == 1


def test_a_merged_event_gathers_every_date_and_fills_the_links() -> None:
    soon = ("exa", event("Drag Brunch", days_ahead=5, official=None, tickets="https://tickets.example/b"))
    later = ("serpapi_events", event("Drag Brunch", days_ahead=12, organizer="Brunch Co", price="Free"))

    merged = merge_event([later, soon])

    assert [item.start_date for item in merged.occurrences] == [TODAY + timedelta(days=5), TODAY + timedelta(days=12)]
    assert merged.links.official == "https://rainbow-bar.example/drag"
    assert merged.links.tickets == "https://tickets.example/b"
    assert (merged.organizer, merged.price_text) == ("Brunch Co", "Free")


def test_a_merged_event_keeps_the_pictures_of_every_draft_once() -> None:
    first = ("exa", event("Drag Brunch", images=["https://cdn.example/a.jpg", "https://cdn.example/b.jpg"]))
    second = (
        "exa",
        event("Drag Brunch", images=["https://cdn.example/b.jpg?utm_source=x", "https://cdn.example/c.jpg"]),
    )

    merged = merge_event([first, second])

    assert sorted(image.url for image in merged.images) == [
        "https://cdn.example/a.jpg",
        "https://cdn.example/b.jpg",
        "https://cdn.example/c.jpg",
    ]


def test_a_merged_event_keeps_at_most_twenty_pictures() -> None:
    drafts = [
        ("exa", event("Drag Brunch", images=[f"https://cdn.example/{draft}-{number}.jpg" for number in range(15)]))
        for draft in range(3)
    ]

    assert len(merge_event(drafts).images) == MAX_EVENT_IMAGES


def test_changed_pictures_alone_do_not_count_as_a_change() -> None:
    plain = event("Drag Brunch")
    pictured = event("Drag Brunch", images=["https://cdn.example/a.jpg"])

    assert event_content_hash(plain) == event_content_hash(pictured)


def test_one_source_saying_cancelled_cancels_the_event() -> None:
    merged = merge_event([("exa", event(status=EventStatus.CANCELLED)), ("serpapi_events", event())])

    assert merged.status is EventStatus.CANCELLED


def test_the_content_hash_ignores_rephrased_descriptions_but_not_moved_addresses() -> None:
    base = venue(description="A friendly bar.")

    assert venue_content_hash(base) == venue_content_hash(venue(description="Friendly neighbourhood bar."))
    assert venue_content_hash(base) != venue_content_hash(venue(address="2 Other Street"))
    assert event_content_hash(event(description="a")) == event_content_hash(event(description="b"))
    assert event_content_hash(event(days_ahead=5)) != event_content_hash(event(days_ahead=6))


def test_preparing_a_city_merges_scores_and_drops_the_junk() -> None:
    entries = [
        ("serpapi_maps", venue("Heaven", website=None, external_ids={"google_place_id": "g1"})),
        ("osm_overpass", venue("Heaven", external_ids={"osm": "node/1"}, lat=10.0, lng=20.0)),
        ("exa", venue("Dead Club", status=OperatingStatus.PERMANENTLY_CLOSED, website="https://dead.example")),
        ("exa", venue("Corner Cafe", level=LgbtqLevel.NO, website="https://cafe.example", lat=10.05, lng=20.05)),
    ]

    prepared = prepare_venues(batch(venues=entries))

    # Venues are not limited to LGBTQ+ places: one that is not LGBTQ+ is kept, with its level as a label.
    assert [item.draft.name for item in prepared.candidates] == ["Heaven", "Corner Cafe"]
    assert prepared.candidates[0].providers == ["osm_overpass", "serpapi_maps"]
    corner = prepared.candidates[1].draft
    assert corner.lgbtq is not None
    assert corner.lgbtq.level is LgbtqLevel.NO
    assert dict(prepared.rejected) == {"closed": 1}


def test_preparing_events_drops_the_ones_already_over() -> None:
    entries = [
        ("exa", event("Over", days_ahead=-4)),
        ("exa", event("Coming", days_ahead=4)),
        ("serpapi_events", event("Coming", days_ahead=4, official=None)),
    ]

    prepared = prepare_events(batch(events=entries), today=TODAY, window_days=60)

    assert [item.draft.name for item in prepared.candidates] == ["Coming"]
    candidate = prepared.candidates[0]
    assert isinstance(candidate.draft, EventDraft)
    assert candidate.fingerprint == event_fingerprint(candidate.draft, SCOPE)
    assert dict(prepared.rejected) == {"expired": 1}
