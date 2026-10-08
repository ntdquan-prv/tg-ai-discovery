from datetime import date

import pytest

from tg_discovery.domain.fingerprint import (
    core_title,
    event_fingerprint,
    events_match,
    normalize_name,
    similarity,
    venue_fingerprint,
    venue_keys,
)
from tg_discovery.domain.models import EventDraft, EventOccurrence, EventVenue, VenueDraft

BKK = "TH:bangkok"


def _venue(name: str = "Heaven", **fields: object) -> VenueDraft:
    return VenueDraft.model_validate({"name": name, **fields})


def _event(name: str, *dates: tuple[date, date | None], venue: str | None = None, **fields: object) -> EventDraft:
    occurrences = [EventOccurrence(start_date=start, end_date=end) for start, end in dates]
    return EventDraft.model_validate(
        {"name": name, "occurrences": occurrences, "venue": EventVenue(name=venue), **fields}
    )


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("The Heaven Gay Bar", "heaven"),
        ("HEAVEN", "heaven"),
        ("Café Müller", "muller"),
        ("Tom & Jerry's Club", "tom jerry s"),
        ("Gay Bar", "gay bar"),  # nothing but generic words: keep them rather than return an empty key
        ("ゲイバー新宿", "ゲイバー新宿"),
        ("ガーデン", "ガーデン"),  # the dakuten of a kana must survive accent stripping
    ],
)
def test_normalize_name(name: str, expected: str) -> None:
    assert normalize_name(name) == expected


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Pride Night 2026 Vol. 3", "pride night"),
        ("Circuit Fest #12", "circuit fest"),
        ("3rd Annual Bear Party", "bear party"),
        ("Saturday Night Fever", "saturday night fever"),
    ],
)
def test_core_title_drops_years_and_edition_numbers(title: str, expected: str) -> None:
    assert core_title(title) == expected


def test_similarity_tolerates_word_order() -> None:
    assert similarity("heaven saturdays", "saturdays heaven") == 1.0
    assert similarity("", "heaven") == 0.0


def test_venue_keys_list_the_strongest_key_first() -> None:
    venue = _venue(
        website="https://www.heavenbar.com/home?utm_source=x",
        lat=13.7563,
        lng=100.5018,
        external_ids={"google_place_id": "ChIJabc", "osm": "node/1", "fsq": "f1"},
    )

    assert venue_keys(venue, BKK) == [
        "gpid:ChIJabc",
        "osm:node/1",
        "fsq:f1",
        f"{BKK}|dom:heavenbar.com",
        f"{BKK}|ng:heaven|w4rqqbr",
    ]
    assert venue_fingerprint(venue, BKK) == "gpid:ChIJabc"


def test_a_social_media_page_is_not_a_website_identity() -> None:
    venue = _venue(website="https://www.facebook.com/heaven", lat=13.7563, lng=100.5018)

    assert venue_keys(venue, BKK) == [f"{BKK}|ng:heaven|w4rqqbr"]


def test_a_text_only_venue_falls_back_to_name_within_its_city() -> None:
    assert venue_keys(_venue("The Heaven Bar"), BKK) == [f"{BKK}|n:heaven"]
    assert venue_keys(_venue("Heaven"), "TH:phuket") != venue_keys(_venue("Heaven"), BKK)


def test_the_name_key_is_not_added_when_something_stronger_exists() -> None:
    assert venue_keys(_venue(website="https://heavenbar.com"), BKK) == [f"{BKK}|dom:heavenbar.com"]


def test_event_fingerprint_ignores_dates_and_year() -> None:
    a = _event("Pride Night 2026", (date(2026, 11, 14), None), venue="Heaven")
    b = _event("Pride Night 2027", (date(2027, 11, 13), None), venue="Heaven")

    assert event_fingerprint(a, BKK) == event_fingerprint(b, BKK) == f"{BKK}|pride night|v:heaven"


def test_event_fingerprint_uses_the_city_when_the_venue_is_unknown() -> None:
    assert event_fingerprint(_event("Pride Night"), BKK) == f"{BKK}|pride night|city"


def test_events_with_similar_titles_match_on_the_same_venue() -> None:
    a = _event("Heaven Saturdays", (date(2026, 11, 14), None), venue="Heaven")
    b = _event("Heaven Saturday", (date(2026, 11, 21), None), venue="Heaven Bar")

    assert events_match(a, b)


def test_events_with_similar_titles_match_on_overlapping_dates() -> None:
    a = _event("Bear Weekend", (date(2026, 11, 13), date(2026, 11, 15)))
    b = _event("Bear Weekend", (date(2026, 11, 14), None), venue="Somewhere")

    assert events_match(a, b)


def test_events_do_not_match_without_a_shared_venue_or_date() -> None:
    a = _event("Bear Weekend", (date(2026, 11, 14), None), venue="Heaven")
    b = _event("Bear Weekend", (date(2026, 12, 14), None), venue="Spartacus")

    assert not events_match(a, b)


def test_events_with_different_titles_do_not_match() -> None:
    a = _event("Pride Night", (date(2026, 11, 14), None), venue="Heaven")
    b = _event("Pride Night Party", (date(2026, 11, 14), None), venue="Heaven")

    assert not events_match(a, b)
