from datetime import UTC, date, datetime, timedelta
from typing import TypedDict

import pytest

from evals.metrics import Analysis, compute_all, compute_metrics
from evals.models import CanaryObservation, DraftRow, UrlCheck
from tests.evals_builders import (
    NOW,
    TODAY,
    call_log,
    city,
    event_row,
    golden_event,
    golden_venue,
    info,
    label,
    make_data,
    venue_row,
)
from tg_discovery.domain.enums import Flow

GOLDEN = [
    golden_venue("g1", "Alpha"),
    golden_venue("g2", "Bravo"),
    golden_venue("g3", "Charlie"),
    golden_venue("g4", "Delta"),
]


class Point(TypedDict):
    lat: float
    lng: float


INSIDE = Point(lat=10.0, lng=20.0)
OUTSIDE = Point(lat=50.0, lng=20.0)


def _venue_run() -> tuple[Analysis, list[DraftRow]]:
    rows = [
        # p1 finds Alpha and Bravo, one novel place twice, and one novel place far outside the city
        venue_row("p1", "Alpha Bar", city_name="Testville", website="https://alpha.test", address="1 St", **INSIDE),
        venue_row("p1", "Bravo", city_name="Testville"),
        venue_row("p1", "Novel One", website="https://novel-one.test", address="2 St", evidence=False, **INSIDE),
        venue_row("p1", "Novel One", website="https://novel-one.test", address="2 St", **INSIDE),
        venue_row("p1", "Faraway Club", **OUTSIDE),
        # p2 finds Alpha and Charlie, the same novel place and another one
        venue_row("p2", "Alpha", city_name="Testville"),
        venue_row("p2", "Charlie", city_name="Testville"),
        venue_row("p2", "Novel One", **INSIDE),
        venue_row("p2", "Novel Two", **INSIDE),
    ]
    calls = [
        call_log("p1", query_id="v01", returned=10, requested=10, cost=0.01, latency_ms=400, urls=["https://a.test/1"]),
        call_log("p1", query_id="v02", returned=3, requested=10, cost=0.01, latency_ms=800, urls=["https://a.test/2"]),
        call_log("p1", query_id="v03", outcome="error", returned=0, cost=0.0),
        call_log("p2", query_id="v01", returned=5, requested=10, cost=0.02, latency_ms=300),
        call_log("p2", query_id="v02", returned=5, requested=10, cost=0.02, latency_ms=500),
    ]
    data = make_data(
        providers=[info("p1"), info("p2")],
        calls=calls,
        drafts=rows,
        venues=GOLDEN,
        labels=[label(rows[2], "valid", "L1"), label(rows[4], "wrong_place", "L2"), label(rows[8], "spam", "L3")],
    )
    return Analysis(data), rows


def test_recall_counts_distinct_golden_records_found() -> None:
    analysis, _ = _venue_run()

    p1 = compute_metrics(analysis, Flow.VENUE, "p1")
    p2 = compute_metrics(analysis, Flow.VENUE, "p2")

    assert (p1.recall_known, p1.found_known) == (0.5, 2)
    assert (p2.recall_known, p2.found_known) == (0.5, 2)
    assert p1.recall_by_city == {"testville": 0.5}


def test_novel_items_and_their_precision_come_from_the_labels() -> None:
    analysis, _ = _venue_run()

    p1 = compute_metrics(analysis, Flow.VENUE, "p1")
    p2 = compute_metrics(analysis, Flow.VENUE, "p2")

    assert p1.novel_count == 2  # Novel One (shared with p2) and Faraway Club
    assert (p1.precision_novel, p1.labeled_novel) == (0.5, 2)
    assert p1.novel_valid_est == pytest.approx(1.0)
    assert p2.novel_count == 2  # Novel One and Novel Two
    assert (p2.precision_novel, p2.labeled_novel) == (0.5, 2)  # one valid, one spam


def test_duplicates_and_coverage_inside_the_provider_are_measured() -> None:
    analysis, _ = _venue_run()

    p1 = compute_metrics(analysis, Flow.VENUE, "p1")

    assert p1.drafts == 5
    assert p1.dup_rate == pytest.approx(1 - 4 / 5)  # Alpha, Bravo, Novel One, Faraway Club
    assert p1.in_area_rate == pytest.approx(4 / 5)  # the faraway club is outside; Bravo is placed by its city name


def test_completeness_needs_every_required_field_from_the_provider_alone() -> None:
    analysis, _ = _venue_run()

    p1 = compute_metrics(analysis, Flow.VENUE, "p1")
    p2 = compute_metrics(analysis, Flow.VENUE, "p2")

    assert p1.field_completeness == pytest.approx(3 / 5)  # Alpha Bar and both Novel One drafts
    assert p2.field_completeness == 0
    assert p1.detail_depth == pytest.approx(0.0)


def test_evidence_rate_only_counts_verified_quotes() -> None:
    analysis, _ = _venue_run()

    assert compute_metrics(analysis, Flow.VENUE, "p1").lgbtq_evidence_rate == pytest.approx(4 / 5)


def test_price_and_reliability() -> None:
    analysis, _ = _venue_run()

    p1 = compute_metrics(analysis, Flow.VENUE, "p1")

    assert p1.calls == 3
    assert p1.search_cost_usd == pytest.approx(0.02)
    assert p1.usd_per_1k_calls == pytest.approx(0.02 * 1000 / 3)
    assert p1.cost_per_known_found == pytest.approx(0.02 / 2)
    assert p1.cost_per_valid_novel == pytest.approx(0.02 / 1.0)
    assert p1.error_rate == pytest.approx(1 / 3)
    assert p1.latency_s == pytest.approx(0.6)  # median of 400 and 800 ms
    assert p1.results_per_call == pytest.approx(6.5)


def test_extraction_cost_is_kept_apart_from_search_cost() -> None:
    from evals.models import LlmLog
    from tg_discovery.providers.base import ProviderCall

    data = make_data(
        providers=[info("p1")],
        calls=[call_log("p1", cost=0.01)],
        drafts=[venue_row("p1", "Alpha")],
        venues=GOLDEN,
    )
    data.llm_calls.append(
        LlmLog(
            provider="p1",
            flow=Flow.VENUE,
            city="testville",
            query_id="v01",
            model="m",
            call=ProviderCall(
                provider="llm:m", endpoint="r", units=10, cost_usd=0.003, latency_ms=1, status="ok", at=NOW
            ),
        )
    )

    metrics = compute_metrics(Analysis(data), Flow.VENUE, "p1")

    assert (metrics.search_cost_usd, metrics.extraction_cost_usd) == (pytest.approx(0.01), pytest.approx(0.003))


def test_union_recall_is_found_by_the_analysis() -> None:
    analysis, _ = _venue_run()

    found = {
        "testville": analysis.found_golden(Flow.VENUE, "p1", "testville")
        | analysis.found_golden(Flow.VENUE, "p2", "testville")
    }

    assert analysis.recall(Flow.VENUE, found)[0] == pytest.approx(3 / 4)


def test_the_pagination_ceiling_needs_a_full_page_that_still_brings_something_new() -> None:
    calls = [
        call_log("p1", query_id="v01", returned=10, requested=10, urls=["https://a.test/1"], at=NOW),
        call_log(
            "p1", query_id="v02", returned=10, requested=10, urls=["https://a.test/1"], at=NOW + timedelta(seconds=1)
        ),
        call_log(
            "p1", query_id="v03", returned=4, requested=10, urls=["https://a.test/9"], at=NOW + timedelta(seconds=2)
        ),
        call_log(
            "p1", query_id="v04", returned=2, requested=None, urls=["https://a.test/7"], at=NOW + timedelta(seconds=3)
        ),
    ]
    data = make_data(providers=[info("p1")], calls=calls, drafts=[venue_row("p1", "Alpha")], venues=GOLDEN)

    # call 1 hits the cap with a new page; call 2 hits the cap but repeats it; call 3 is below the cap;
    # call 4 has no cap and is not counted.
    assert compute_metrics(Analysis(data), Flow.VENUE, "p1").pagination_ceiling_hit_rate == pytest.approx(1 / 3)


def test_page_age_is_the_median_age_of_dated_results() -> None:
    dates = [NOW - timedelta(days=10), NOW - timedelta(days=30), None, NOW - timedelta(days=20)]
    data = make_data(
        providers=[info("p1")],
        calls=[call_log("p1", returned=4, urls=["https://a.test"] * 4, dates=dates)],
        drafts=[venue_row("p1", "Alpha")],
        venues=GOLDEN,
    )

    assert compute_metrics(Analysis(data), Flow.VENUE, "p1").median_source_age_days == 20


def test_languages_are_covered_by_queries_in_that_language_or_by_a_language_free_provider() -> None:
    two = city(languages=("en", "de"))
    english_only = make_data(
        providers=[info("p1")], cities=[two], calls=[call_log("p1")], drafts=[venue_row("p1", "Alpha", language="en")]
    )
    both = make_data(
        providers=[info("p1")],
        cities=[two],
        calls=[call_log("p1")],
        drafts=[venue_row("p1", "Alpha", language="en"), venue_row("p1", "Bravo", language="de")],
    )
    language_free = make_data(
        providers=[info("p1")], cities=[two], calls=[call_log("p1")], drafts=[venue_row("p1", "Alpha", language="*")]
    )

    assert compute_metrics(Analysis(english_only), Flow.VENUE, "p1").language_coverage == 0.5
    assert compute_metrics(Analysis(both), Flow.VENUE, "p1").language_coverage == 1.0
    assert compute_metrics(Analysis(language_free), Flow.VENUE, "p1").language_coverage == 1.0


def test_url_liveness_ignores_sites_that_block_bots() -> None:
    rows = [venue_row("p1", n, website=f"https://{n.lower()}.test") for n in ("Alpha", "Bravo", "Charlie", "Delta")]
    checks = [
        UrlCheck(url="https://alpha.test", status="alive", checked_at=NOW),
        UrlCheck(url="https://bravo.test", status="dead", http_status=404, checked_at=NOW),
        UrlCheck(url="https://charlie.test", status="blocked", http_status=403, checked_at=NOW),
    ]
    data = make_data(providers=[info("p1")], calls=[call_log("p1")], drafts=rows, venues=GOLDEN, url_checks=checks)

    assert compute_metrics(Analysis(data), Flow.VENUE, "p1").url_alive_rate == 0.5


def test_metrics_without_data_are_none_not_zero() -> None:
    data = make_data(providers=[info("p1")], calls=[call_log("p1", outcome="error", returned=0)])

    metrics = compute_metrics(Analysis(data), Flow.VENUE, "p1")

    assert metrics.drafts == 0
    assert metrics.recall_known is None
    assert metrics.field_completeness is None
    assert metrics.precision_novel is None
    assert metrics.latency_s is None
    assert metrics.error_rate == 1.0


def test_event_freshness_metrics() -> None:
    rows = [
        event_row("p1", "Old Party", TODAY - timedelta(days=30)),
        event_row("p1", "Running Festival", TODAY - timedelta(days=2), end=TODAY + timedelta(days=1)),
        event_row("p1", "Next Month", TODAY + timedelta(days=20)),
        event_row("p1", "No Date", None),
    ]
    canary = [
        CanaryObservation(checkpoint_hours=0, provider="p1", canary_id="c1", seen=False, at=NOW),
        CanaryObservation(checkpoint_hours=6, provider="p1", canary_id="c1", seen=True, at=NOW),
        CanaryObservation(checkpoint_hours=0, provider="p1", canary_id="c2", seen=False, at=NOW),
        CanaryObservation(checkpoint_hours=48, provider="p1", canary_id="c2", seen=True, at=NOW),
    ]
    data = make_data(
        providers=[info("p1", flows=(Flow.EVENT,))],
        flows=(Flow.EVENT,),
        calls=[call_log("p1", flow=Flow.EVENT, query_id="e01")],
        drafts=rows,
        events=[golden_event("e1", "Next Month", TODAY + timedelta(days=20))],
        canary=canary,
    )

    metrics = compute_all(Analysis(data))[Flow.EVENT]["p1"]

    assert metrics.stale_result_rate == pytest.approx(1 / 3)  # only dated drafts count; the festival is still running
    assert metrics.canary_hit_24h == 0.5  # c1 within a day, c2 only after two days
    assert metrics.recall_known == 1.0


def test_event_metrics_do_not_leak_into_the_venue_flow() -> None:
    analysis, _ = _venue_run()

    p1 = compute_metrics(analysis, Flow.VENUE, "p1")

    assert p1.canary_hit_24h is None
    assert p1.stale_result_rate is None


def test_date_helpers_use_the_run_date() -> None:
    assert date(2026, 10, 8) == TODAY
    assert NOW.tzinfo is UTC
    assert isinstance(NOW, datetime)
