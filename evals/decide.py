"""The decision rule of guide section 6.

1. Every metric is min-max normalised across the providers that were measured on it (higher or lower is better as the
   metric says) and averaged into its criterion's score. A metric on which all providers are equal says nothing about
   which is better and is left out rather than diluting the others.
2. A provider's weighted score combines the four criteria with the per-flow weights of guide section 3.4.
3. Combinations of up to ``MAX_COMBO_SIZE`` providers are checked against the hard floors: union recall of the known
   set and precision of the novel items.
4. The pick is the cheapest combination that clears the floors. Combinations within ``TIE_TOLERANCE`` of the cheapest
   are tied: fewer vendors wins, then not relying on Google data, then the higher mean weighted score.

The weights rank individual providers and break ties; the floors and the price decide the pick, exactly as the guide
words it. Both views are reported so the owner can overrule the pick.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from itertools import combinations
from statistics import mean
from typing import Literal

from evals.metrics import Analysis, ProviderMetrics
from tg_discovery.domain.enums import Flow

# criterion -> (metric, higher is better)
CRITERIA: Mapping[str, tuple[tuple[str, bool], ...]] = {
    "realtime": (
        ("canary_hit_24h", True),
        ("median_source_age_days", False),
        ("stale_result_rate", False),
        ("latency_s", False),
    ),
    "price": (
        ("search_cost_usd", False),
        ("cost_per_known_found", False),
        ("cost_per_valid_novel", False),
    ),
    "coverage": (
        ("recall_known", True),
        ("novel_valid_est", True),
        ("unique_domains", True),
        ("language_coverage", True),
    ),
    "completeness": (
        ("field_completeness", True),
        ("results_per_call", True),
        ("pagination_ceiling_hit_rate", False),
        ("detail_depth", True),
    ),
}
WEIGHTS: Mapping[Flow, Mapping[str, float]] = {
    Flow.VENUE: {"coverage": 35, "completeness": 30, "price": 25, "realtime": 10},
    Flow.EVENT: {"realtime": 35, "coverage": 30, "completeness": 20, "price": 15},
}
RECALL_FLOOR: Mapping[Flow, float] = {Flow.VENUE: 0.75, Flow.EVENT: 0.60}
PRECISION_FLOOR = 0.60
TIE_TOLERANCE = 0.10
MAX_COMBO_SIZE = 4

PrecisionStatus = Literal["ok", "fail", "unverified"]


@dataclass(frozen=True, slots=True)
class ProviderScore:
    provider: str
    criteria: Mapping[str, float | None]
    weighted: float | None


@dataclass(frozen=True, slots=True)
class Combo:
    providers: tuple[str, ...]
    union_recall: float | None
    min_city_recall: float | None
    novel_count: int
    labeled_novel: int
    precision: float | None
    precision_status: PrecisionStatus
    search_cost_usd: float
    extraction_cost_usd: float
    google_derived: bool
    mean_score: float

    @property
    def cost_usd(self) -> float:
        return self.search_cost_usd + self.extraction_cost_usd


@dataclass(frozen=True, slots=True)
class FlowDecision:
    flow: Flow
    scores: Mapping[str, ProviderScore]
    combos: Sequence[Combo]
    recall_floor: float
    precision_floor: float
    labels_available: bool
    pick: Combo | None
    pick_is_provisional: bool
    frontier: Sequence[Combo]
    best_recall: Combo | None


def _value(metrics: ProviderMetrics, name: str) -> float | None:
    value = getattr(metrics, name)
    return None if value is None else float(value)


def _normalise(values: Mapping[str, float | None], higher_is_better: bool) -> dict[str, float]:
    """Min-max scale to [0, 1]; a metric that does not tell the providers apart carries no information."""
    present = {provider: value for provider, value in values.items() if value is not None}
    if not present:
        return {}
    low, high = min(present.values()), max(present.values())
    if high == low:
        return {}
    span = high - low
    return {
        provider: (value - low) / span if higher_is_better else (high - value) / span
        for provider, value in present.items()
    }


def score_providers(flow: Flow, metrics: Mapping[str, ProviderMetrics]) -> dict[str, ProviderScore]:
    """Criterion scores and the weighted score of every provider; criteria without data are left out."""
    normalised: dict[tuple[str, str], dict[str, float]] = {}
    for criterion, components in CRITERIA.items():
        for name, higher in components:
            normalised[(criterion, name)] = _normalise({p: _value(m, name) for p, m in metrics.items()}, higher)
    scores: dict[str, ProviderScore] = {}
    for provider in metrics:
        per_criterion: dict[str, float | None] = {}
        for criterion, components in CRITERIA.items():
            parts = [
                normalised[(criterion, name)][provider]
                for name, _ in components
                if provider in normalised[(criterion, name)]
            ]
            per_criterion[criterion] = mean(parts) if parts else None
        weights = {c: WEIGHTS[flow][c] for c, s in per_criterion.items() if s is not None}
        weighted = (
            sum(WEIGHTS[flow][c] * (s or 0.0) for c, s in per_criterion.items() if s is not None)
            / sum(weights.values())
            if weights
            else None
        )
        scores[provider] = ProviderScore(provider=provider, criteria=per_criterion, weighted=weighted)
    return scores


def _combo(
    analysis: Analysis,
    flow: Flow,
    members: tuple[str, ...],
    metrics: Mapping[str, ProviderMetrics],
    scores: Mapping[str, ProviderScore],
    labels_available: bool,
    precision_floor: float,
) -> Combo:
    cities = [city.slug for city in analysis.attempted_cities(flow)]
    found: dict[str, set[str]] = {}
    for city in cities:
        found[city] = set()
        for provider in members:
            found[city] |= analysis.found_golden(flow, provider, city)
    recall, by_city = analysis.recall(flow, found)
    novel = {entity.id: entity for provider in members for entity in analysis.novel(flow, provider)}
    precision, labeled = analysis.precision(list(novel.values()))
    status: PrecisionStatus
    if not novel:
        status = "ok"
    elif labeled == 0 or precision is None:
        status = "unverified"
    else:
        status = "ok" if precision >= precision_floor else "fail"
    info = {item.name: item for item in analysis.data.meta.providers}
    weighted = [score for p in members if (score := scores[p].weighted) is not None]
    return Combo(
        providers=members,
        union_recall=recall,
        min_city_recall=min(by_city.values()) if by_city else None,
        novel_count=len(novel),
        labeled_novel=labeled,
        precision=precision,
        precision_status=status,
        search_cost_usd=sum(metrics[p].search_cost_usd for p in members),
        extraction_cost_usd=sum(metrics[p].extraction_cost_usd for p in members),
        google_derived=any(info[p].google_derived for p in members if p in info),
        mean_score=mean(weighted) if weighted else 0.0,
    )


def _tie_break(combo: Combo) -> tuple[int, bool, float]:
    return len(combo.providers), combo.google_derived, -combo.mean_score


def choose(combos: Sequence[Combo], recall_floor: float, *, labels_available: bool) -> tuple[Combo | None, bool]:
    """The cheapest combination clearing the floors, and whether the precision floor could not be checked for it."""
    allowed: tuple[PrecisionStatus, ...] = ("ok",) if labels_available else ("ok", "unverified")
    feasible = [
        c
        for c in combos
        if c.union_recall is not None and c.union_recall >= recall_floor and c.precision_status in allowed
    ]
    if not feasible:
        return None, False
    cheapest = min(c.cost_usd for c in feasible)
    tied = [c for c in feasible if c.cost_usd <= cheapest * (1 + TIE_TOLERANCE) + 1e-9]
    pick = min(tied, key=_tie_break)
    return pick, pick.precision_status == "unverified"


def pareto_frontier(combos: Sequence[Combo], *, labels_available: bool) -> list[Combo]:
    """Combinations that no cheaper (or equally cheap) combination matches on union recall."""
    allowed: tuple[PrecisionStatus, ...] = ("ok",) if labels_available else ("ok", "unverified")
    candidates = sorted(
        (c for c in combos if c.union_recall is not None and c.precision_status in allowed),
        key=lambda c: (c.cost_usd, -(c.union_recall or 0.0), len(c.providers)),
    )
    frontier: list[Combo] = []
    best = -1.0
    for combo in candidates:
        if (combo.union_recall or 0.0) > best + 1e-12:
            frontier.append(combo)
            best = combo.union_recall or 0.0
    return frontier


def decide(analysis: Analysis, flow: Flow, metrics: Mapping[str, ProviderMetrics]) -> FlowDecision:
    """Score the providers of one flow, search the combinations and apply the rule."""
    scores = score_providers(flow, metrics)
    labels_available = bool(analysis.data.labels)
    usable = [provider for provider, m in metrics.items() if m.calls and m.drafts]
    combos = [
        _combo(analysis, flow, members, metrics, scores, labels_available, PRECISION_FLOOR)
        for size in range(1, min(MAX_COMBO_SIZE, len(usable)) + 1)
        for members in combinations(usable, size)
    ]
    combos.sort(key=lambda c: (c.cost_usd, -(c.union_recall or 0.0), len(c.providers)))
    pick, provisional = choose(combos, RECALL_FLOOR[flow], labels_available=labels_available)
    with_recall = [c for c in combos if c.union_recall is not None]
    return FlowDecision(
        flow=flow,
        scores=scores,
        combos=combos,
        recall_floor=RECALL_FLOOR[flow],
        precision_floor=PRECISION_FLOOR,
        labels_available=labels_available,
        pick=pick,
        pick_is_provisional=provisional,
        frontier=pareto_frontier(combos, labels_available=labels_available),
        best_recall=max(with_recall, key=lambda c: (c.union_recall or 0.0, -c.cost_usd), default=None),
    )
