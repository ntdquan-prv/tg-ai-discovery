"""What a run would cost, before anything is sent: calls per provider times what one call can cost.

For most vendors that is the list price of a request; for Exa it is the worst case the budget guard will reserve (a
request with ten pages of text). The figures are still indicative: the budget limit, not this table, is what stops the
spending.
"""

from collections.abc import Collection, Sequence
from dataclasses import dataclass

from evals.models import CitySpec
from evals.registry import CLI_HINT
from tg_discovery.config import ProviderSettings
from tg_discovery.domain.enums import Flow
from tg_discovery.pipelines.common.queries import PLAN_SIZE
from tg_discovery.providers.registry import SEARCH_ENTRIES, VENUE_ENTRIES, Entry, terms_refusal

# A typical extraction call: about 20,000 characters of pages in, a few records out.
TYPICAL_INPUT_TOKENS = 6_000
TYPICAL_OUTPUT_TOKENS = 2_000


@dataclass(frozen=True, slots=True)
class PlanLine:
    name: str
    flows: tuple[Flow, ...]
    calls: int
    usd_per_call: float
    skipped: str | None
    terms: str | None

    @property
    def cost_usd(self) -> float:
        return 0.0 if self.skipped else self.calls * self.usd_per_call


@dataclass(frozen=True, slots=True)
class Plan:
    lines: tuple[PlanLine, ...]
    extraction_calls: int
    extraction_usd: float
    budget_usd: float

    @property
    def search_usd(self) -> float:
        return sum(line.cost_usd for line in self.lines)

    @property
    def total_usd(self) -> float:
        return self.search_usd + self.extraction_usd


def _line(
    entry: Entry[object],
    cities: Sequence[CitySpec],
    flows: Sequence[Flow],
    settings: ProviderSettings,
    accepted: Collection[str],
    queries: int,
    max_results: int,
) -> PlanLine:
    active = tuple(flow for flow in flows if flow in entry.flows)
    per_flow = 1 if entry.queryless else queries
    skipped = terms_refusal(entry, accepted, hint=CLI_HINT) or (
        None if entry.credential(settings) else f"{entry.setting} is not set"
    )
    per_call = entry.worst_case(max_results) if entry.worst_case else entry.price.usd_per_unit
    return PlanLine(entry.name, active, len(cities) * len(active) * per_flow, per_call, skipped, entry.terms)


def estimate(
    cities: Sequence[CitySpec],
    flows: Sequence[Flow],
    settings: ProviderSettings,
    budget_usd: float,
    *,
    names: Sequence[str] | None = None,
    accepted: Collection[str] = (),
    max_queries: int | None = None,
    max_results: int = 10,
) -> Plan:
    """Calls and indicative cost per provider for the selected cities, flows and providers."""
    queries = min(PLAN_SIZE, max_queries or PLAN_SIZE)
    lines: list[PlanLine] = []
    extraction_calls = 0
    for kind, entries in (("search", SEARCH_ENTRIES), ("venue", VENUE_ENTRIES)):
        for entry in entries:
            if names is not None and entry.name not in names:
                continue
            line = _line(entry, cities, flows, settings, accepted, queries, max_results)
            lines.append(line)
            if kind == "search" and line.skipped is None:
                extraction_calls += line.calls  # every successful search is followed by one extraction call
    per_call = (
        TYPICAL_INPUT_TOKENS * settings.extractor_input_usd_per_1m_tokens
        + TYPICAL_OUTPUT_TOKENS * settings.extractor_output_usd_per_1m_tokens
    ) / 1_000_000
    return Plan(
        lines=tuple(lines),
        extraction_calls=extraction_calls,
        extraction_usd=extraction_calls * per_call,
        budget_usd=budget_usd,
    )


def render(plan: Plan) -> str:
    rows = [
        f"  {line.name:<20} {','.join(f.value for f in line.flows) or '-':<12} {line.calls:>5} calls  "
        f"${line.usd_per_call:<8.4f} per call  ${line.cost_usd:>8.4f}"
        + (f"   SKIPPED: {line.skipped}" if line.skipped else "")
        for line in plan.lines
    ]
    notes = [f"  terms, {line.name}: {line.terms}" for line in plan.lines if line.terms]
    return "\n".join(
        [
            "Plan (indicative; the budget limit is what stops spending):",
            *rows,
            f"  search total       ${plan.search_usd:.4f}",
            f"  extraction         ~{plan.extraction_calls} model calls, ~${plan.extraction_usd:.4f}",
            f"  indicative total   ${plan.total_usd:.4f}   budget limit ${plan.budget_usd:.2f}",
            *(["", "Vendor terms to read:", *notes] if notes else []),
        ]
    )
