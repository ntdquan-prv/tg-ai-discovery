"""The bake-off metrics, per provider and flow.

Each metric is ``None`` when the run holds nothing to compute it from (no labels yet, no canary probes, a structured
provider that returns no page dates...) so a missing measurement is never mistaken for a zero.

Definitions, by criterion (cities are averaged with equal weight where a metric is per city):

* Realtime: ``canary_hit_24h`` share of canary events seen within 24 hours; ``median_source_age_days`` age of the
  returned pages when they were fetched; ``stale_result_rate`` share of dated event drafts that already ended;
  ``latency_s`` median seconds per successful call.
* Price: ``search_cost_usd`` what the provider's own calls cost (extraction is reported separately);
  ``cost_per_known_found``; ``cost_per_valid_novel`` (uses the estimated number of valid novel items).
* Coverage: ``recall_known`` share of TravelGay's golden records rediscovered; ``novel_count`` entities TravelGay does
  not list; ``novel_valid_est`` those entities times the labelled precision; ``unique_domains``;
  ``language_coverage`` share of the (city, language) pairs of the plan that yielded a draft.
* Completeness: ``field_completeness`` share of drafts with every required field supplied by the provider alone;
  ``results_per_call``; ``pagination_ceiling_hit_rate`` share of calls that returned the full cap and at least one new
  page; ``detail_depth`` share of the optional fields filled in.
* Quality: ``precision_novel`` share of labelled novel entities that admins marked valid; ``in_area_rate``;
  ``url_alive_rate``; ``lgbtq_evidence_rate`` share of drafts that carry a verified quote for an LGBTQ+ level;
  ``dup_rate`` share of drafts that repeat another draft of the same provider.
"""

from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from statistics import mean, median

from evals.data import RunData
from evals.matching import Entity
from evals.models import CallLog, CitySpec, DraftRow
from tg_discovery.domain.enums import Flow, LgbtqLevel
from tg_discovery.domain.models import (
    EVENT_OPTIONAL_FIELDS,
    VENUE_OPTIONAL_FIELDS,
    EventDraft,
    VenueDraft,
)
from tg_discovery.domain.urls import registrable_domain

AREA_MARGIN = 0.15  # a coordinate just outside the city's box (suburbs) still counts as in the area
CANARY_WINDOW_HOURS = 24


@dataclass(frozen=True, slots=True)
class ProviderMetrics:
    provider: str
    flow: Flow
    calls: int
    drafts: int
    error_rate: float | None
    # Realtime
    canary_hit_24h: float | None
    median_source_age_days: float | None
    stale_result_rate: float | None
    latency_s: float | None
    # Price
    search_cost_usd: float
    extraction_cost_usd: float
    usd_per_1k_calls: float | None
    cost_per_known_found: float | None
    cost_per_valid_novel: float | None
    # Coverage
    recall_known: float | None
    recall_by_city: Mapping[str, float]
    found_known: int
    novel_count: int
    novel_valid_est: float | None
    unique_domains: int
    language_coverage: float | None
    # Completeness
    field_completeness: float | None
    results_per_call: float | None
    pagination_ceiling_hit_rate: float | None
    detail_depth: float | None
    # Quality
    precision_novel: float | None
    labeled_novel: int
    in_area_rate: float | None
    url_alive_rate: float | None
    lgbtq_evidence_rate: float | None
    dup_rate: float | None


def _ratio(numerator: float, denominator: float) -> float | None:
    return numerator / denominator if denominator else None


def _draft(row: DraftRow) -> VenueDraft | EventDraft:
    draft = row.venue or row.event
    if draft is None:  # a DraftRow always carries one of the two
        msg = "draft row without a draft"
        raise ValueError(msg)
    return draft


def _in_area(draft: VenueDraft | EventDraft, city: CitySpec) -> bool | None:
    """Whether a draft lies in the city: by coordinates when it has them, otherwise by the city named in its text."""
    if isinstance(draft, VenueDraft):
        lat, lng, named, address = draft.lat, draft.lng, draft.city, draft.address
    else:
        lat, lng, named, address = draft.venue.lat, draft.venue.lng, draft.venue.city, draft.venue.address
    if lat is not None and lng is not None and city.bbox is not None:
        return city.bbox.contains(lat, lng, margin_ratio=AREA_MARGIN)
    names = [name.casefold() for name in city.names]
    for text in (named, address):
        if text and any(name in text.casefold() for name in names):
            return True
    return False if named else None


def own_urls(draft: VenueDraft | EventDraft) -> list[str]:
    """The addresses a draft claims for itself (a venue's website, an event's page and ticket link)."""
    if isinstance(draft, VenueDraft):
        return [draft.website] if draft.website else []
    return [url for url in (draft.links.official, draft.links.tickets) if url]


def _optional_fill(draft: VenueDraft | EventDraft) -> float:
    optional = VENUE_OPTIONAL_FIELDS if isinstance(draft, VenueDraft) else EVENT_OPTIONAL_FIELDS
    missing = sum(field in optional for field in draft.missing_fields)
    return 1 - missing / len(optional)


def _has_verified_evidence(draft: VenueDraft | EventDraft) -> bool:
    lgbtq = draft.lgbtq
    return lgbtq is not None and lgbtq.level in (LgbtqLevel.PRIMARY, LgbtqLevel.FRIENDLY) and bool(lgbtq.evidence)


def _is_stale(draft: EventDraft, today: date) -> bool | None:
    return draft.last_end < today if draft.last_end is not None else None


class Analysis:
    """Indexes of a run shared by the metrics and the combination search."""

    def __init__(self, data: RunData) -> None:
        self.data = data
        self.cities: dict[str, CitySpec] = {city.slug: city for city in data.meta.cities}
        self.today = data.meta.today
        # Canary probes are a separate, smaller query set: they count towards spend, not towards the run's metrics.
        self._calls: dict[tuple[Flow, str], list[CallLog]] = defaultdict(list)
        for call in data.calls:
            if not call.is_canary:
                self._calls[(call.flow, call.provider)].append(call)
        self._rows: dict[tuple[Flow, str], list[DraftRow]] = defaultdict(list)
        for row in data.drafts:
            self._rows[(row.flow, row.provider)].append(row)
        self._entities: dict[tuple[Flow, str], list[Entity]] = defaultdict(list)
        for entity in data.entities:
            for provider in entity.providers:
                self._entities[(entity.flow, provider)].append(entity)
        self.golden: dict[tuple[Flow, str], set[str]] = defaultdict(set)
        for city, venues in data.inputs.venues.items():
            self.golden[(Flow.VENUE, city)] = {venue.id for venue in venues}
        for city, events in data.inputs.events.items():
            self.golden[(Flow.EVENT, city)] = {event.id for event in events}

    def providers(self, flow: Flow) -> list[str]:
        """Providers with at least one call for the flow, in the order the run listed them."""
        called = {provider for (call_flow, provider) in self._calls if call_flow is flow}
        return [info.name for info in self.data.meta.providers if info.name in called]

    def attempted_cities(self, flow: Flow) -> list[CitySpec]:
        """Cities where the flow actually ran (a run cut short by its budget may not have reached them all)."""
        attempted = {call.city for (call_flow, _), calls in self._calls.items() if call_flow is flow for call in calls}
        return [city for city in self.data.meta.cities if city.slug in attempted]

    def calls(self, flow: Flow, provider: str) -> list[CallLog]:
        return self._calls[(flow, provider)]

    def rows(self, flow: Flow, provider: str) -> list[DraftRow]:
        return self._rows[(flow, provider)]

    def entities(self, flow: Flow, provider: str) -> list[Entity]:
        return self._entities[(flow, provider)]

    def found_golden(self, flow: Flow, provider: str, city: str) -> set[str]:
        return {
            entity.golden_id
            for entity in self.entities(flow, provider)
            if entity.city == city and entity.golden_id is not None
        }

    def novel(self, flow: Flow, provider: str) -> list[Entity]:
        return [entity for entity in self.entities(flow, provider) if entity.is_novel]

    def is_valid(self, entity: Entity) -> bool | None:
        """The admins' verdict on a novel entity: ``True`` valid, ``False`` any other label, ``None`` not labelled."""
        record = self.data.label_of(entity)
        return None if record is None else record.label == "valid"

    def precision(self, entities: list[Entity]) -> tuple[float | None, int]:
        """Share of the labelled entities that are valid, and how many were labelled."""
        verdicts = [verdict for entity in entities if (verdict := self.is_valid(entity)) is not None]
        return _ratio(sum(verdicts), len(verdicts)), len(verdicts)

    def recall(self, flow: Flow, found: Mapping[str, set[str]]) -> tuple[float | None, dict[str, float]]:
        """Macro-average recall over the cities with golden records, given the golden ids found per city."""
        by_city = {
            city.slug: len(found.get(city.slug, set())) / len(self.golden[(flow, city.slug)])
            for city in self.attempted_cities(flow)
            if self.golden[(flow, city.slug)]
        }
        return (mean(by_city.values()) if by_city else None), by_city


def _canary_hit_rate(analysis: Analysis, provider: str) -> float | None:
    observed = [o for o in analysis.data.canary_observations if o.provider == provider]
    probed = {o.canary_id for o in observed}
    hits = {o.canary_id for o in observed if o.seen and o.checkpoint_hours <= CANARY_WINDOW_HOURS}
    return _ratio(len(hits), len(probed))


def _source_age_days(calls: list[CallLog]) -> float | None:
    ages = [
        max((log.call.at.date() - published.date()).days, 0)
        for log in calls
        if log.outcome == "ok"
        for published in log.hit_dates
        if published is not None
    ]
    return float(median(ages)) if ages else None


def _ceiling_hit_rate(calls: list[CallLog]) -> float | None:
    """Share of capped calls that returned the full cap and still brought a page not seen in an earlier call."""
    seen: set[str] = set()
    capped = hits = 0
    for log in sorted((log for log in calls if log.outcome == "ok"), key=lambda log: log.call.at):
        fresh = {url for url in log.hit_urls if url not in seen}
        seen.update(log.hit_urls)
        if log.requested is None:
            continue
        capped += 1
        hits += log.returned >= log.requested and bool(fresh)
    return _ratio(hits, capped)


def _unique_domains(calls: list[CallLog], rows: list[DraftRow]) -> int:
    urls = [url for log in calls if log.outcome == "ok" for url in log.hit_urls]
    for row in rows:
        draft = _draft(row)
        urls += [source.url for source in draft.sources] + own_urls(draft)
    return len({domain for url in urls if (domain := registrable_domain(url))})


def _language_coverage(analysis: Analysis, flow: Flow, rows: list[DraftRow]) -> float | None:
    """Share of the (city, language) pairs of the plan that produced at least one draft; ``*`` covers every language."""
    wanted = {(city.slug, language) for city in analysis.attempted_cities(flow) for language in city.languages}
    if not wanted:
        return None
    produced = {(row.city, row.language) for row in rows}
    covered = {pair for pair in wanted if pair in produced or (pair[0], "*") in produced}
    return len(covered) / len(wanted)


def _url_alive_rate(analysis: Analysis, rows: list[DraftRow]) -> float | None:
    urls = {url for row in rows for url in own_urls(_draft(row))}
    checks = [analysis.data.url_checks[url] for url in urls if url in analysis.data.url_checks]
    decided = [check for check in checks if check.status in ("alive", "dead")]
    return _ratio(sum(check.status == "alive" for check in decided), len(decided))


def compute_metrics(analysis: Analysis, flow: Flow, provider: str) -> ProviderMetrics:
    data = analysis.data
    calls = analysis.calls(flow, provider)
    ok_calls = [log for log in calls if log.outcome == "ok"]
    rows = analysis.rows(flow, provider)
    drafts = [_draft(row) for row in rows]

    found: dict[str, set[str]] = {
        city.slug: analysis.found_golden(flow, provider, city.slug) for city in analysis.attempted_cities(flow)
    }
    recall, recall_by_city = analysis.recall(flow, found)
    found_total = sum(len(ids) for ids in found.values())
    novel = analysis.novel(flow, provider)
    precision, labeled = analysis.precision(novel)
    novel_valid = precision * len(novel) if precision is not None else None

    search_cost = sum(log.call.cost_usd for log in calls)
    extraction_cost = sum(
        log.call.cost_usd
        for log in data.llm_calls
        if log.provider == provider and log.flow is flow and log.call is not None
    )

    cities = analysis.cities
    area = [_in_area(draft, cities[row.city]) for row, draft in zip(rows, drafts, strict=True)]
    decided_area = [verdict for verdict in area if verdict is not None]

    distinct_per_city: dict[str, set[str]] = defaultdict(set)
    for entity in analysis.entities(flow, provider):
        distinct_per_city[entity.city].add(entity.id)
    stale = [
        flag
        for draft in drafts
        if isinstance(draft, EventDraft) and (flag := _is_stale(draft, analysis.today)) is not None
    ]

    return ProviderMetrics(
        provider=provider,
        flow=flow,
        calls=len(calls),
        drafts=len(drafts),
        error_rate=_ratio(len(calls) - len(ok_calls), len(calls)),
        canary_hit_24h=_canary_hit_rate(analysis, provider) if flow is Flow.EVENT else None,
        median_source_age_days=_source_age_days(calls),
        stale_result_rate=_ratio(sum(stale), len(stale)) if flow is Flow.EVENT else None,
        latency_s=median(log.call.latency_ms for log in ok_calls) / 1000 if ok_calls else None,
        search_cost_usd=search_cost,
        extraction_cost_usd=extraction_cost,
        usd_per_1k_calls=_ratio(search_cost * 1000, len(calls)),
        cost_per_known_found=_ratio(search_cost, found_total),
        cost_per_valid_novel=_ratio(search_cost, novel_valid or 0),
        recall_known=recall,
        recall_by_city=recall_by_city,
        found_known=found_total,
        novel_count=len(novel),
        novel_valid_est=novel_valid,
        unique_domains=_unique_domains(ok_calls, rows),
        language_coverage=_language_coverage(analysis, flow, rows),
        field_completeness=_ratio(sum(draft.has_required_fields for draft in drafts), len(drafts)),
        results_per_call=mean(log.returned for log in ok_calls) if ok_calls else None,
        pagination_ceiling_hit_rate=_ceiling_hit_rate(calls),
        detail_depth=mean(_optional_fill(draft) for draft in drafts) if drafts else None,
        precision_novel=precision,
        labeled_novel=labeled,
        in_area_rate=_ratio(sum(decided_area), len(decided_area)),
        url_alive_rate=_url_alive_rate(analysis, rows),
        lgbtq_evidence_rate=_ratio(sum(_has_verified_evidence(draft) for draft in drafts), len(drafts)),
        dup_rate=(1 - sum(len(ids) for ids in distinct_per_city.values()) / len(drafts)) if drafts else None,
    )


def compute_all(analysis: Analysis) -> dict[Flow, dict[str, ProviderMetrics]]:
    """Metrics for every provider that ran, keyed by flow and provider name."""
    return {
        flow: {provider: compute_metrics(analysis, flow, provider) for provider in analysis.providers(flow)}
        for flow in analysis.data.meta.flows
    }
