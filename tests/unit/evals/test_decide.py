import pytest

from evals.decide import PRECISION_FLOOR, RECALL_FLOOR, Combo, decide, pareto_frontier, score_providers
from evals.metrics import Analysis, ProviderMetrics, compute_all
from tests.evals_builders import call_log, golden_venue, info, label, make_data, venue_row
from tg_discovery.domain.enums import Flow

NAMES = ["Alpha", "Bravo", "Charlie", "Delta"]
GOLDEN = [golden_venue(f"g{n}", name) for n, name in enumerate(NAMES, start=1)]


def _metrics(provider: str, **fields: object) -> ProviderMetrics:
    base: dict[str, object] = {
        "provider": provider,
        "flow": Flow.VENUE,
        "calls": 1,
        "drafts": 1,
        "error_rate": None,
        "canary_hit_24h": None,
        "median_source_age_days": None,
        "stale_result_rate": None,
        "latency_s": None,
        "search_cost_usd": 0.0,
        "extraction_cost_usd": 0.0,
        "usd_per_1k_calls": None,
        "cost_per_known_found": None,
        "cost_per_valid_novel": None,
        "recall_known": None,
        "recall_by_city": {},
        "found_known": 0,
        "novel_count": 0,
        "novel_valid_est": None,
        "unique_domains": 0,
        "language_coverage": None,
        "field_completeness": None,
        "results_per_call": None,
        "pagination_ceiling_hit_rate": None,
        "detail_depth": None,
        "precision_novel": None,
        "labeled_novel": 0,
        "in_area_rate": None,
        "url_alive_rate": None,
        "lgbtq_evidence_rate": None,
        "dup_rate": None,
    }
    return ProviderMetrics(**{**base, **fields})  # pyright: ignore[reportArgumentType]


def test_lower_is_better_for_price_and_higher_for_recall() -> None:
    scores = score_providers(
        Flow.VENUE,
        {
            "cheap": _metrics("cheap", search_cost_usd=0.1, recall_known=0.4, unique_domains=2),
            "dear": _metrics("dear", search_cost_usd=0.3, recall_known=0.8, unique_domains=9),
        },
    )

    assert scores["cheap"].criteria["price"] == 1.0
    assert scores["dear"].criteria["price"] == 0.0
    assert scores["cheap"].criteria["coverage"] == 0.0
    assert scores["dear"].criteria["coverage"] == 1.0


def test_a_metric_nobody_differs_on_is_left_out_instead_of_diluting_the_rest() -> None:
    equal = score_providers(
        Flow.VENUE, {"a": _metrics("a", search_cost_usd=0.1), "b": _metrics("b", search_cost_usd=0.1)}
    )
    mixed = score_providers(
        Flow.VENUE,
        {
            "a": _metrics("a", search_cost_usd=0.1, recall_known=0.9, unique_domains=4),
            "b": _metrics("b", search_cost_usd=0.1, recall_known=0.3, unique_domains=4),
        },
    )

    assert equal["a"].criteria["price"] is None
    assert equal["a"].weighted is None  # one metric and nobody differs: nothing to rank on
    assert (mixed["a"].criteria["coverage"], mixed["b"].criteria["coverage"]) == (
        1.0,
        0.0,
    )  # the equal domains do not count


def test_weights_follow_the_flow_and_skip_criteria_without_data() -> None:
    metrics = {
        "a": _metrics("a", search_cost_usd=0.3, recall_known=0.9, unique_domains=7),
        "b": _metrics("b", search_cost_usd=0.1, recall_known=0.3, unique_domains=1),
    }

    venue = score_providers(Flow.VENUE, metrics)
    event = score_providers(Flow.EVENT, metrics)

    # a: coverage 1, price 0.  Venue weights 35 and 25, event weights 30 and 15; nothing else has data.
    assert venue["a"].weighted == pytest.approx(35 / 60)
    assert event["a"].weighted == pytest.approx(30 / 45)
    assert venue["b"].weighted == pytest.approx(25 / 60)


def test_a_provider_missing_a_metric_is_not_penalised_for_it() -> None:
    scores = score_providers(
        Flow.EVENT,
        {
            "a": _metrics("a", canary_hit_24h=0.8, latency_s=1.0),
            "b": _metrics("b", canary_hit_24h=0.4, latency_s=2.0),
            "c": _metrics("c", latency_s=1.5),  # never probed with canaries
        },
    )

    assert scores["a"].criteria["realtime"] == 1.0
    assert scores["b"].criteria["realtime"] == 0.0
    assert scores["c"].criteria["realtime"] == 0.5  # judged on latency alone, not scored 0 for the missing canary


def _scenario(*, labels: bool = False, bad_novel: bool = False, google: str | None = None):
    """Three providers over four known venues.

    p1 finds Alpha and Bravo (cost 0.02), p2 finds Charlie and Delta (0.05), p3 finds Alpha, Bravo and Charlie (0.20).
    """
    rows = [
        venue_row("p1", "Alpha"),
        venue_row("p1", "Bravo"),
        venue_row("p2", "Charlie"),
        venue_row("p2", "Delta"),
        venue_row("p3", "Alpha"),
        venue_row("p3", "Bravo"),
        venue_row("p3", "Charlie"),
    ]
    novel = venue_row("p2", "Mystery Place")
    if bad_novel:
        rows.append(novel)
    calls = [call_log("p1", cost=0.02), call_log("p2", cost=0.05), call_log("p3", cost=0.20)]
    verdicts = [label(novel, "spam")] if labels and bad_novel else []
    data = make_data(
        providers=[
            info("p1", google=google == "p1"),
            info("p2", google=google == "p2"),
            info("p3", google=google == "p3"),
        ],
        calls=calls,
        drafts=rows,
        venues=GOLDEN,
        labels=verdicts,
    )
    analysis = Analysis(data)
    return analysis, compute_all(analysis)[Flow.VENUE]


def test_the_pick_is_the_cheapest_combination_that_reaches_the_recall_floor() -> None:
    analysis, metrics = _scenario()

    decision = decide(analysis, Flow.VENUE, metrics)

    assert decision.recall_floor == RECALL_FLOOR[Flow.VENUE] == 0.75
    assert decision.pick is not None
    assert decision.pick.providers == ("p1", "p2")
    assert decision.pick.union_recall == 1.0
    assert decision.pick.cost_usd == pytest.approx(0.07)
    assert not decision.pick_is_provisional  # no novel items, so nothing for the precision floor to judge


def test_a_single_provider_that_just_reaches_the_floor_is_a_candidate() -> None:
    analysis, metrics = _scenario()

    decision = decide(analysis, Flow.VENUE, metrics)

    single = next(c for c in decision.combos if c.providers == ("p3",))
    assert single.union_recall == 0.75
    assert single in decision.combos


def test_tied_costs_prefer_fewer_vendors_then_non_google_sources() -> None:
    analysis, metrics = _scenario()
    free_google = Combo(("p1", "p2"), 0.9, 0.9, 0, 0, None, "ok", 0.10, 0.0, True, 0.5)
    free_plain = Combo(("p1", "p3"), 0.9, 0.9, 0, 0, None, "ok", 0.10, 0.0, False, 0.5)
    single = Combo(("p3",), 0.9, 0.9, 0, 0, None, "ok", 0.105, 0.0, True, 0.5)  # within 10% of the cheapest
    from evals.decide import choose

    pick, _ = choose([free_google, free_plain, single], 0.75, labels_available=False)

    assert pick == single  # one vendor beats two, even though it is Google-derived and 5% dearer
    pick, _ = choose([free_google, free_plain], 0.75, labels_available=False)
    assert pick == free_plain  # same vendor count and cost: not relying on Google wins
    assert analysis is not None
    assert metrics


def test_a_combination_with_bad_labelled_novel_items_fails_the_precision_floor() -> None:
    analysis, metrics = _scenario(labels=True, bad_novel=True)

    decision = decide(analysis, Flow.VENUE, metrics)

    assert decision.labels_available
    assert PRECISION_FLOOR == 0.60
    assert decision.pick is not None
    assert "p2" not in decision.pick.providers  # p2 returned the spam item, so every combination holding it fails
    assert decision.pick.providers == ("p3",)
    failing = next(c for c in decision.combos if c.providers == ("p1", "p2"))
    assert failing.precision_status == "fail"


def test_unlabelled_novel_items_make_the_pick_provisional_until_labels_exist() -> None:
    analysis, metrics = _scenario(bad_novel=True)

    decision = decide(analysis, Flow.VENUE, metrics)

    assert not decision.labels_available
    assert decision.pick is not None
    assert decision.pick.providers == ("p1", "p2")
    assert decision.pick.precision_status == "unverified"
    assert decision.pick_is_provisional


def test_without_a_feasible_combination_the_best_recall_is_reported() -> None:
    rows = [venue_row("p1", "Alpha"), venue_row("p2", "Bravo")]
    data = make_data(
        providers=[info("p1"), info("p2")],
        calls=[call_log("p1"), call_log("p2")],
        drafts=rows,
        venues=GOLDEN,
    )
    analysis = Analysis(data)

    decision = decide(analysis, Flow.VENUE, compute_all(analysis)[Flow.VENUE])

    assert decision.pick is None
    assert decision.best_recall is not None
    assert decision.best_recall.union_recall == 0.5
    assert decision.best_recall.providers == ("p1", "p2")


def test_the_frontier_only_keeps_combinations_that_buy_more_recall() -> None:
    analysis, metrics = _scenario()
    decision = decide(analysis, Flow.VENUE, metrics)

    frontier = pareto_frontier(decision.combos, labels_available=False)

    recalls = [c.union_recall or 0.0 for c in frontier]
    costs = [c.cost_usd for c in frontier]
    assert recalls == sorted(recalls)
    assert len(set(recalls)) == len(recalls)
    assert costs == sorted(costs)
    assert frontier[-1].union_recall == 1.0
    assert frontier[-1].providers == ("p1", "p2")
