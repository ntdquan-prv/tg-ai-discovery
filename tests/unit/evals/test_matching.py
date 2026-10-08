from datetime import date

import pytest

from evals.matching import cluster_events, cluster_venues, resolve_entities, same_event, same_venue
from tests.evals_builders import CITY, city, event_row, golden_event, golden_venue, venue_row
from tg_discovery.domain.models import EventDraft, EventOccurrence, EventVenue, VenueDraft


def _v(name: str, **fields: object) -> VenueDraft:
    return VenueDraft.model_validate({"name": name, **fields})


def _e(name: str, *starts: date, venue: str | None = None) -> EventDraft:
    return EventDraft(
        name=name, occurrences=[EventOccurrence(start_date=s) for s in starts], venue=EventVenue(name=venue)
    )


@pytest.mark.parametrize(
    ("a", "b", "expected"),
    [
        (_v("Heaven Bar", lat=10.0, lng=20.0), _v("HEAVEN", lat=10.001, lng=20.0), True),
        (_v("Heaven", lat=10.0, lng=20.0), _v("Heaven", lat=10.05, lng=20.0), False),  # 5 km apart
        (_v("Heaven", lat=10.0, lng=20.0), _v("Spartacus", lat=10.0, lng=20.0), False),
        (_v("Heaven Bar"), _v("Heaven"), True),
        (_v("Heaven"), _v("Heaven 2"), False),
        (_v("Sauna 24", lat=10.0, lng=20.0), _v("Sauna 25", lat=10.0, lng=20.0), False),  # numbers tell places apart
        (_v("Studio 54 Bar", lat=10.0, lng=20.0), _v("Studio 54", lat=10.0, lng=20.0), True),
        (_v("Heaven"), _v("Utopia", website="https://heaven.test"), False),
        (
            _v("Heaven", website="https://heaven.test/a"),
            _v("The Heaven Group", website="https://www.heaven.test"),
            True,
        ),
        (
            _v("Heaven", website="https://facebook.com/heaven"),
            _v("Utopia", website="https://facebook.com/utopia"),
            False,
        ),
        (
            _v("Heaven", website="https://heaven.test", lat=10.0, lng=20.0),
            _v("Heaven", website="https://heaven.test", lat=10.03, lng=20.0),
            False,  # same chain, another branch 3 km away
        ),
        (_v("A", external_ids={"osm": "node/1"}), _v("Totally Different", external_ids={"osm": "node/1"}), True),
        (
            _v("A", external_ids={"osm": "node/1"}),
            _v("A", external_ids={"osm": "node/2"}),
            True,
        ),  # same name, no coords
    ],
)
def test_same_venue(a: VenueDraft, b: VenueDraft, expected: bool) -> None:
    assert same_venue(a, b) is expected
    assert same_venue(b, a) is expected


def test_the_same_event_is_matched_across_titles_with_years_and_dates() -> None:
    a = _e("Pride Night 2026", date(2026, 11, 14), venue="Heaven")
    b = _e("Pride Night", date(2026, 11, 21), venue="Heaven Bar")

    assert same_event(a, b)


def test_an_undated_event_matches_on_a_near_identical_title() -> None:
    assert same_event(_e("Bear Weekend Party"), _e("Bear Weekend Party", date(2026, 11, 14), venue="Spartacus"))
    assert not same_event(_e("Bear Weekend Party"), _e("Bear Weekend Parade", date(2026, 11, 14)))


def test_providers_drafts_of_one_venue_form_one_cluster() -> None:
    drafts = [_v("Heaven Bar"), _v("Heaven"), _v("Spartacus"), _v("Heaven Nightclub")]

    groups = cluster_venues(drafts, [False] * 4, CITY)

    assert groups == [[0, 1, 3], [2]]


def test_two_golden_records_never_merge_even_through_a_bridging_draft() -> None:
    drafts = [_v("Heaven"), _v("Heaven Club"), _v("Heaven Bar")]  # the last one is close to both golden names

    groups = cluster_venues(drafts, [True, True, False], CITY)

    assert sorted(len(group) for group in groups) == [1, 2]
    assert [0] not in groups or [1] not in groups
    assert not any(0 in group and 1 in group for group in groups)


def test_recurring_events_cluster_by_title_and_venue() -> None:
    drafts = [
        _e("Heaven Saturdays", date(2026, 11, 14), venue="Heaven"),
        _e("Heaven Saturdays", date(2026, 11, 21), venue="Heaven"),
        _e("Pride Parade", date(2026, 11, 14), venue="City Hall"),
    ]

    assert cluster_events(drafts, [False] * 3, CITY) == [[0, 1], [2]]


def test_entities_tie_provider_drafts_to_golden_records() -> None:
    rows = [
        venue_row("p1", "Heaven Bar", lat=10.0, lng=20.0),
        venue_row("p2", "Heaven", lat=10.0005, lng=20.0),
        venue_row("p1", "Novel Place"),
        venue_row("p2", "Novel Place"),
        venue_row("p2", "Another Novel"),
    ]
    golden = [golden_venue("g1", "Heaven", lat=10.0, lng=20.0), golden_venue("g2", "Never Found")]

    entities = resolve_entities(rows, [city()], {CITY: golden}, {})

    by_golden = {e.golden_id: e for e in entities if e.golden_id}
    assert by_golden["g1"].providers == {"p1", "p2"}
    assert by_golden["g2"].providers == frozenset()
    novel = [e for e in entities if e.is_novel]
    assert sorted(sorted(e.providers) for e in novel) == [["p1", "p2"], ["p2"]]
    assert all(e.representative is not None for e in novel)
    assert len({e.id for e in entities}) == len(entities)


def test_event_entities_use_the_golden_schedule() -> None:
    rows = [
        event_row("p1", "Heaven Saturdays 2026", date(2026, 11, 14), venue="Heaven"),
        event_row("p1", "Something New", date(2026, 12, 1)),
    ]
    golden = [golden_event("e1", "Heaven Saturdays", date(2026, 11, 14), venue="Heaven")]

    entities = resolve_entities(rows, [city()], {}, {CITY: golden})

    assert {e.golden_id for e in entities if e.providers} == {"e1", None}
