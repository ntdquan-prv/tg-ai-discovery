from datetime import date

from tg_discovery.domain.enums import Flow, VenueType
from tg_discovery.pipelines.common.queries import build_event_plan, build_venue_plan

TODAY = date(2026, 10, 8)


def test_an_event_plan_is_dated_and_asks_for_recent_pages() -> None:
    plan = build_event_plan(city="Bangkok", country_name="Thailand", today=TODAY)

    assert [q.id for q in plan] == [f"e{n:02d}" for n in range(1, 13)]
    assert {q.flow for q in plan} == {Flow.EVENT}
    assert {q.language for q in plan} == {"en"}
    assert {q.freshness_days for q in plan} == {60}
    assert "queer events Bangkok October 2026" in {q.text for q in plan}
    assert "Pride Bangkok 2026 events" in {q.text for q in plan}


def test_the_event_window_sets_the_freshness_filter() -> None:
    plan = build_event_plan(city="Bangkok", country_name="Thailand", today=TODAY, freshness_days=30)

    assert {q.freshness_days for q in plan} == {30}


def test_a_venue_run_asks_three_queries_for_each_type_and_every_type_when_none_is_chosen() -> None:
    every = build_venue_plan(city="Bangkok", country_name="Thailand")

    assert len(every) == 3 * len(VenueType) == 30
    assert {q.flow for q in every} == {Flow.VENUE}
    assert {q.freshness_days for q in every} == {None}
    assert len({q.id for q in every}) == 30
    assert [q.text for q in every[9:12]] == [
        "best bars in Bangkok",
        "gay bars in Bangkok",
        "cocktail bars and pubs in Bangkok",
    ]
    assert not any("{" in q.text for q in every)
    assert build_venue_plan(city="Bangkok", country_name="Thailand", venue_types=[]) == every


def test_a_venue_run_asks_only_about_the_chosen_types_in_a_fixed_order() -> None:
    plan = build_venue_plan(
        city="Bangkok", country_name="Thailand", venue_types=[VenueType.SAUNAS, VenueType.CAFE, VenueType.SAUNAS]
    )

    assert [q.id for q in plan] == ["cafe-1", "cafe-2", "cafe-3", "saunas-1", "saunas-2", "saunas-3"]
    assert plan[4].text == "gay saunas in Bangkok"
