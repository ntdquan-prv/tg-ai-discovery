from datetime import UTC, date, datetime

from tg_discovery.domain.enums import LgbtqLevel
from tg_discovery.domain.models import (
    DESCRIPTION_MAX_CHARS,
    QUOTE_MAX_CHARS,
    EventDraft,
    EventImage,
    EventLinks,
    EventOccurrence,
    EventVenue,
    Evidence,
    LgbtqAssessment,
    SourceRef,
    VenueDraft,
    scrub_google_derived,
)

NOW = datetime(2026, 10, 8, tzinfo=UTC)


def test_blank_strings_become_none_and_text_is_trimmed() -> None:
    venue = VenueDraft.model_validate({"name": "  Heaven  ", "address": "   ", "phone": ""})

    assert venue.name == "Heaven"
    assert venue.address is None
    assert venue.phone is None


def test_coordinates_must_come_in_pairs() -> None:
    assert VenueDraft(name="Heaven", lat=13.7).has_coordinates is False
    assert VenueDraft(name="Heaven", lat=13.7).lat is None
    assert VenueDraft(name="Heaven", lat=13.7, lng=100.5).has_coordinates


def test_quotes_and_descriptions_are_capped() -> None:
    evidence = Evidence(quote="x" * 500, url="https://heaven.com")
    venue = VenueDraft(name="Heaven", description="y" * 900)

    assert len(evidence.quote) == QUOTE_MAX_CHARS
    assert venue.description is not None
    assert len(venue.description) == DESCRIPTION_MAX_CHARS


def test_venue_missing_fields_and_required_check() -> None:
    bare = VenueDraft(name="Heaven")
    complete = VenueDraft(name="Heaven", address="1 Soi 4", lat=13.7, lng=100.5, website="https://heaven.com")

    assert bare.missing_fields == [
        "address", "coordinates", "website", "venue_type", "phone", "opening_hours", "description",
    ]  # fmt: skip
    assert not bare.has_required_fields
    assert complete.has_required_fields
    assert complete.missing_fields == ["venue_type", "phone", "opening_hours", "description"]


def test_missing_fields_are_part_of_the_serialised_draft() -> None:
    assert VenueDraft(name="Heaven").model_dump()["missing_fields"][0] == "address"


def test_a_serialised_draft_loads_back() -> None:
    venue = VenueDraft(
        name="Heaven", lat=13.7, lng=100.5, sources=[SourceRef(url="https://x.com", provider="p", retrieved_at=NOW)]
    )

    assert VenueDraft.model_validate_json(venue.model_dump_json()) == venue


def test_event_required_fields() -> None:
    complete = EventDraft(
        name="Pride Night",
        occurrences=[EventOccurrence(start_date=date(2026, 11, 14))],
        venue=EventVenue(name="Heaven"),
        links=EventLinks(official="https://heaven.com/pride"),
    )
    no_date = complete.model_copy(update={"occurrences": []})

    assert complete.has_required_fields
    assert not no_date.has_required_fields
    assert "date" in no_date.missing_fields
    assert complete.missing_fields == ["organizer", "price_text", "start_time", "description"]


def test_event_date_helpers() -> None:
    event = EventDraft(
        name="Bear Weekend",
        occurrences=[
            EventOccurrence(start_date=date(2026, 11, 13), end_date=date(2026, 11, 15)),
            EventOccurrence(start_date=date(2026, 12, 4)),
        ],
    )

    assert event.first_start == date(2026, 11, 13)
    assert event.last_end == date(2026, 12, 4)
    assert EventDraft(name="x").first_start is None


def test_scrub_google_derived_keeps_facts_and_drops_the_rest() -> None:
    venue = VenueDraft(
        name="Heaven",
        address="1 Soi 4",
        lat=13.7,
        lng=100.5,
        website="https://heaven.com",
        phone="+66 2 123 4567",
        opening_hours="Daily 20:00-02:00",
        description="A club.",
        price_text="$$",
        socials={"instagram": "https://instagram.com/heaven"},
        external_ids={"google_place_id": "ChIJabc"},
        lgbtq=LgbtqAssessment(level=LgbtqLevel.PRIMARY, evidence=[Evidence(quote="review text", url="https://x.com")]),
    )

    scrubbed = scrub_google_derived(venue)

    assert (scrubbed.name, scrubbed.address, scrubbed.lat, scrubbed.website) == (
        "Heaven",
        "1 Soi 4",
        13.7,
        "https://heaven.com",
    )
    assert scrubbed.external_ids == {"google_place_id": "ChIJabc"}
    assert scrubbed.phone is None
    assert scrubbed.opening_hours is None
    assert scrubbed.description is None
    assert scrubbed.price_text is None
    assert scrubbed.socials == {}
    assert scrubbed.lgbtq is not None
    assert scrubbed.lgbtq.level is LgbtqLevel.PRIMARY
    assert scrubbed.lgbtq.evidence == []
    assert venue.phone is not None  # the original is untouched

    kept = scrub_google_derived(venue, keep_details=True)  # the owner's KEEP_GOOGLE_DETAILS switch

    assert (kept.phone, kept.opening_hours, kept.description, kept.price_text) == (
        "+66 2 123 4567",
        "Daily 20:00-02:00",
        "A club.",
        "$$",
    )
    assert kept.socials == {}  # socials and quotes are dropped either way
    assert kept.lgbtq is not None
    assert kept.lgbtq.evidence == []


def test_scrub_google_derived_event() -> None:
    event = EventDraft(
        name="Pride Night",
        description="Fun.",
        price_text="THB 500",
        organizer="Someone",
        images=[EventImage(url="https://encrypted-tbn0.gstatic.example/p.jpg")],
    )

    scrubbed = scrub_google_derived(event)

    assert (scrubbed.name, scrubbed.description, scrubbed.price_text, scrubbed.organizer) == (
        "Pride Night",
        None,
        None,
        None,
    )
    assert scrubbed.images == []
    assert len(event.images) == 1  # the original is untouched

    kept = scrub_google_derived(event, keep_details=True)

    assert (kept.description, kept.price_text, kept.organizer, kept.images) == ("Fun.", "THB 500", None, [])


def test_an_event_has_no_images_unless_some_were_found() -> None:
    assert EventDraft(name="Pride Night").images == []


def test_an_image_alt_text_is_capped_and_blank_becomes_none() -> None:
    long = EventImage(url="https://cdn.example/a.jpg", alt="x" * 500)
    blank = EventImage(url="https://cdn.example/b.jpg", alt="   ")

    assert long.alt is not None
    assert len(long.alt) == 200
    assert blank.alt is None
