import pytest

from tests.pipeline_builders import AREA, TODAY, event, venue
from tg_discovery.domain.enums import EventStatus, LgbtqLevel, OperatingStatus
from tg_discovery.domain.models import EventDraft, VenueDraft
from tg_discovery.pipelines.common.scoring import (
    EVENT_WEIGHTS,
    FAST_APPROVE_FROM,
    REJECT_BELOW,
    VENUE_WEIGHTS,
    score_event,
    score_venue,
)

TWO_SOURCES = ["exa", "osm_overpass"]


def test_the_weights_of_each_flow_add_up_to_one() -> None:
    assert sum(VENUE_WEIGHTS.values()) == pytest.approx(1.0)
    assert sum(EVENT_WEIGHTS.values()) == pytest.approx(1.0)


def test_a_venue_with_a_quote_a_position_and_two_sources_is_an_easy_approve() -> None:
    scored = score_venue(venue(status=OperatingStatus.OPEN), TWO_SOURCES, area=AREA)

    assert scored.decision == "needs_review"
    assert scored.reason is None
    assert scored.total >= FAST_APPROVE_FROM
    assert scored.fast_approve is True


def test_a_thin_single_source_venue_goes_to_review_without_the_easy_label() -> None:
    thin = venue(website=None, address=None, lat=None, lng=None, quote=False, venue_type=None)

    scored = score_venue(thin, ["exa"], area=AREA)

    assert REJECT_BELOW <= scored.total < FAST_APPROVE_FROM
    assert (scored.decision, scored.fast_approve) == ("needs_review", False)


def test_the_concerns_say_what_to_look_at_twice() -> None:
    scored = score_venue(venue(website=None, lat=None, lng=None, quote=False), ["exa"], area=AREA)

    assert scored.concerns == [
        "It is not known whether the venue is still open.",
        "Only one source lists it.",
        "No website to check it against.",
        "No coordinates, so its position was not checked.",
    ]


@pytest.mark.parametrize(
    ("draft", "reason"),
    [
        (venue(status=OperatingStatus.PERMANENTLY_CLOSED), "closed"),
        (venue(lat=40.0, lng=-74.0), "outside_area"),
    ],
)
def test_a_hard_gate_rejects_a_venue_whatever_else_it_scores(draft: VenueDraft, reason: str) -> None:
    scored = score_venue(draft, TWO_SOURCES, area=AREA)

    assert (scored.decision, scored.reason, scored.fast_approve) == ("reject", reason, False)


@pytest.mark.parametrize("level", [*LgbtqLevel, None])
def test_the_lgbtq_level_neither_scores_nor_rejects_a_venue(level: LgbtqLevel | None) -> None:
    gay_bar = score_venue(venue(status=OperatingStatus.OPEN), TWO_SOURCES, area=AREA)

    scored = score_venue(venue(status=OperatingStatus.OPEN, level=level), TWO_SOURCES, area=AREA)

    assert "lgbtq_relevance" not in scored.parts
    assert (scored.total, scored.decision, scored.concerns) == (gay_bar.total, "needs_review", gay_bar.concerns)


def test_junk_below_the_threshold_is_dropped() -> None:
    junk = venue(
        website=None,
        address=None,
        lat=None,
        lng=None,
        quote=False,
        status=OperatingStatus.TEMPORARILY_CLOSED,
        venue_type=None,
    )

    scored = score_venue(junk, ["exa"], area=AREA)

    assert scored.total < REJECT_BELOW
    assert (scored.decision, scored.reason) == ("reject", "low_score")


def test_a_venue_just_outside_the_box_is_still_in_the_city() -> None:
    near = venue(lat=10.105, lng=20.0)

    assert score_venue(near, TWO_SOURCES, area=AREA).decision == "needs_review"


def test_an_upcoming_event_with_a_page_and_two_sources_is_an_easy_approve() -> None:
    scored = score_event(event(organizer="Org", price="Free"), TWO_SOURCES, today=TODAY, window_days=60, area=AREA)

    assert scored.parts["time_validity"] == 1.0
    assert (scored.decision, scored.fast_approve) == ("needs_review", True)


def test_an_inferred_year_halves_the_credit_for_the_date() -> None:
    scored = score_event(event(inferred=True), ["exa"], today=TODAY, window_days=60, area=AREA)

    assert scored.parts["time_validity"] == 0.5
    assert "The year was not stated, so the date was inferred." in scored.concerns


def test_a_date_beyond_the_window_counts_for_less() -> None:
    scored = score_event(event(days_ahead=120), ["exa"], today=TODAY, window_days=60, area=AREA)

    assert scored.parts["time_validity"] == 0.4


def test_an_event_without_a_date_cannot_be_checked_and_says_so() -> None:
    scored = score_event(event(dated=False), ["exa"], today=TODAY, window_days=60, area=AREA)

    assert scored.parts["time_validity"] == 0.0
    assert "No date was found." in scored.concerns


@pytest.mark.parametrize(
    ("draft", "reason"),
    [
        (event(days_ahead=-3), "expired"),
        (event(status=EventStatus.CANCELLED), "cancelled"),
        (event(level=LgbtqLevel.NO), "not_lgbtq"),
    ],
)
def test_a_past_cancelled_or_unrelated_event_is_dropped(draft: EventDraft, reason: str) -> None:
    scored = score_event(draft, TWO_SOURCES, today=TODAY, window_days=60, area=AREA)

    assert (scored.decision, scored.reason) == ("reject", reason)


def test_an_event_that_is_still_on_today_is_not_expired() -> None:
    scored = score_event(event(days_ahead=0), ["exa"], today=TODAY, window_days=60, area=AREA)

    assert scored.decision == "needs_review"


def test_an_event_only_google_listed_has_nothing_to_verify_it_against() -> None:
    google = event(official=None, quote=False, provider="serpapi_events")

    scored = score_event(google, ["serpapi_events"], today=TODAY, window_days=60, area=AREA)

    assert "No official or ticket page to check it against." in scored.concerns
    assert scored.parts["verifiability"] == 0.3
