"""Renders ``report.md`` (and a draft of ADR 0001) from a run: scorecards, metrics, combinations and caveats."""

from collections.abc import Mapping, Sequence

from evals.data import RunData
from evals.decide import PRECISION_FLOOR, TIE_TOLERANCE, Combo, FlowDecision
from evals.metrics import ProviderMetrics
from tg_discovery.domain.enums import Flow

MAX_COMBO_ROWS = 12
LOW_GOLDEN = {Flow.VENUE: 15, Flow.EVENT: 10}  # below these the guide asks admins to list their own
HIGH_ERROR_RATE = 0.2

METRIC_DEFINITIONS = """\
**Realtime**: `canary hit ≤24 h` share of canary events seen within a day · `page age` median age of returned pages when
fetched · `stale` share of dated event drafts that had already ended · `latency` median seconds per successful call.
**Price**: `search cost` the provider's own calls (extraction is listed apart) · `$ per known found` · `$ per valid
novel` (uses the estimated number of valid novel items).
**Coverage**: `recall` share of TravelGay's known records rediscovered (cities weighted equally) · `novel` entities
TravelGay does not list · `est. valid novel` novel times labelled precision · `domains` distinct sites · `languages`
share of (city, language) pairs of the query plan that produced a draft.
**Completeness**: `fields` share of drafts with every required field supplied by the provider alone (venue: name,
address, coordinates, website; event: name, date, venue or address, URL) · `per call` results per call · `ceiling`
share of capped calls that returned the full cap and still brought a new page · `depth` share of optional fields filled.
**Quality**: `precision` share of labelled novel items that admins marked valid · `in area` · `URL alive` among probed
URLs that answered either way · `evidence` share of drafts with a verified LGBTQ+ quote · `dups` share of drafts that
repeat another draft of the same provider · `errors` share of calls that failed.
"""


def _num(value: float | None, digits: int = 2) -> str:
    return "n/a" if value is None else f"{value:.{digits}f}"


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.0f}%"


def _usd(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"${value:,.2f}" if value >= 1 else f"${value:.4f}"


def _table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    lines += ["| " + " | ".join(row) + " |" for row in rows]
    return "\n".join(lines)


def _combo_name(combo: Combo) -> str:
    return " + ".join(f"`{provider}`" for provider in combo.providers)


def _describe_combo(combo: Combo, decision: FlowDecision) -> str:
    if combo.precision_status == "unverified":
        precision = "precision not yet verified (no labelled novel items)"
    elif combo.precision is None:
        precision = "no novel items"
    else:
        precision = f"precision {_pct(combo.precision)} over {combo.labeled_novel} labelled items"
    return (
        f"union recall {_pct(combo.union_recall)} (floor {_pct(decision.recall_floor)}), {precision} "
        f"(floor {_pct(decision.precision_floor)}), cost {_usd(combo.cost_usd)} per run "
        f"(search {_usd(combo.search_cost_usd)} + extraction {_usd(combo.extraction_cost_usd)})"
    )


def _verdict(decision: FlowDecision) -> str:
    if decision.pick is not None:
        provisional = (
            "\n\n> **Provisional**: the precision floor could not be checked because the novel items of this "
            "combination have not been labelled. Label them (see `evals/README.md`) and re-run the report."
            if decision.pick_is_provisional
            else ""
        )
        return f"**Pick: {_combo_name(decision.pick)}**: {_describe_combo(decision.pick, decision)}.{provisional}"
    if decision.best_recall is not None:
        return (
            f"**No combination of up to four providers clears the floors.** The best union recall is "
            f"{_combo_name(decision.best_recall)}: {_describe_combo(decision.best_recall, decision)}."
        )
    return "**Nothing to decide yet**: no provider returned drafts for this flow."


def _flow_section(
    flow: Flow, metrics: Mapping[str, ProviderMetrics], decision: FlowDecision, cities: Sequence[str]
) -> str:
    ordered = sorted(metrics, key=lambda p: -(decision.scores[p].weighted or -1.0))
    out = [f"## {flow.value.capitalize()} flow", "", _verdict(decision), ""]

    out += ["### Providers, ranked by weighted score", ""]
    out.append(
        _table(
            ["Provider", "Weighted", "Realtime", "Price", "Coverage", "Completeness", "Recall", "Precision", "Cost"],
            [
                [
                    f"`{p}`",
                    _num(decision.scores[p].weighted),
                    *(
                        _num(decision.scores[p].criteria.get(c))
                        for c in ("realtime", "price", "coverage", "completeness")
                    ),
                    _pct(metrics[p].recall_known),
                    _pct(metrics[p].precision_novel),
                    _usd(metrics[p].search_cost_usd + metrics[p].extraction_cost_usd),
                ]
                for p in ordered
            ],
        )
    )
    out += [
        "",
        "Scores are 0 to 1, min-max normalised across the providers measured in this flow; a criterion without data is "
        "left out and the remaining weights are renormalised.",
        "",
    ]

    out += ["### Metrics by criterion", "", "**Realtime**", ""]
    out.append(
        _table(
            ["Provider", "Canary ≤24 h", "Page age (days)", "Stale", "Latency (s)"],
            [
                [
                    f"`{p}`",
                    _pct(metrics[p].canary_hit_24h),
                    _num(metrics[p].median_source_age_days, 0),
                    _pct(metrics[p].stale_result_rate),
                    _num(metrics[p].latency_s),
                ]
                for p in ordered
            ],
        )
    )
    out += ["", "**Price**", ""]
    out.append(
        _table(
            ["Provider", "Calls", "Search", "Extraction", "$ per 1k calls", "$ per known found", "$ per valid novel"],
            [
                [
                    f"`{p}`",
                    str(metrics[p].calls),
                    _usd(metrics[p].search_cost_usd),
                    _usd(metrics[p].extraction_cost_usd),
                    _usd(metrics[p].usd_per_1k_calls),
                    _usd(metrics[p].cost_per_known_found),
                    _usd(metrics[p].cost_per_valid_novel),
                ]
                for p in ordered
            ],
        )
    )
    out += ["", "**Coverage**", ""]
    out.append(
        _table(
            ["Provider", "Recall", "Known found", "Novel", "Est. valid novel", "Domains", "Languages"],
            [
                [
                    f"`{p}`",
                    _pct(metrics[p].recall_known),
                    str(metrics[p].found_known),
                    str(metrics[p].novel_count),
                    _num(metrics[p].novel_valid_est, 1),
                    str(metrics[p].unique_domains),
                    _pct(metrics[p].language_coverage),
                ]
                for p in ordered
            ],
        )
    )
    out += ["", "**Completeness**", ""]
    out.append(
        _table(
            ["Provider", "Drafts", "Fields", "Per call", "Ceiling", "Depth"],
            [
                [
                    f"`{p}`",
                    str(metrics[p].drafts),
                    _pct(metrics[p].field_completeness),
                    _num(metrics[p].results_per_call, 1),
                    _pct(metrics[p].pagination_ceiling_hit_rate),
                    _pct(metrics[p].detail_depth),
                ]
                for p in ordered
            ],
        )
    )
    out += ["", "**Quality**", ""]
    out.append(
        _table(
            ["Provider", "Precision", "Labelled", "In area", "URL alive", "Evidence", "Dups", "Errors"],
            [
                [
                    f"`{p}`",
                    _pct(metrics[p].precision_novel),
                    str(metrics[p].labeled_novel),
                    _pct(metrics[p].in_area_rate),
                    _pct(metrics[p].url_alive_rate),
                    _pct(metrics[p].lgbtq_evidence_rate),
                    _pct(metrics[p].dup_rate),
                    _pct(metrics[p].error_rate),
                ]
                for p in ordered
            ],
        )
    )

    out += ["", "### Recall by city", ""]
    out.append(
        _table(
            ["Provider", *cities],
            [[f"`{p}`", *(_pct(metrics[p].recall_by_city.get(city)) for city in cities)] for p in ordered],
        )
    )

    out += ["", "### Combinations", ""]
    out.append(
        f"Floors: union recall of the known set at least {_pct(decision.recall_floor)}, precision of the novel items "
        f"at least {_pct(PRECISION_FLOOR)}. The pick is the cheapest combination that clears both; combinations within "
        f"{_pct(TIE_TOLERANCE)} of the cheapest are tied and resolved by fewer vendors, then by not relying on Google "
        f"data, then by the higher mean weighted score. The table is the cost and recall frontier: no listed "
        f"combination is matched on recall by a cheaper one."
    )
    out.append("")
    shown = list(decision.frontier)
    for extra in (decision.pick, decision.best_recall):
        if extra is not None and extra not in shown:
            shown.append(extra)
    out.append(
        _table(
            ["", "Providers", "Union recall", "Weakest city", "Precision", "Novel", "Cost", "Mean score"],
            [
                [
                    "pick" if combo == decision.pick else "",
                    _combo_name(combo),
                    _pct(combo.union_recall),
                    _pct(combo.min_city_recall),
                    _pct(combo.precision) if combo.precision_status != "unverified" else "unverified",
                    str(combo.novel_count),
                    _usd(combo.cost_usd),
                    _num(combo.mean_score),
                ]
                for combo in shown[:MAX_COMBO_ROWS]
            ],
        )
    )
    return "\n".join(out)


def _caveats(data: RunData, metrics: Mapping[Flow, Mapping[str, ProviderMetrics]]) -> list[str]:
    caveats: list[str] = []
    if data.meta.dry_run:
        caveats.append(
            "This is a **dry run**: every provider and the extractor were simulated, so nothing here says anything "
            "about a real provider."
        )
    if data.meta.stopped_by_budget:
        caveats.append("The run **stopped when the budget ran out**; later queries were not asked of any provider.")
    if not data.labels:
        caveats.append(
            "No admin labels were imported, so precision, valid-novel counts and the precision floor are not measured."
        )
    if not data.canary_observations and Flow.EVENT in data.meta.flows:
        caveats.append("No canary probes were recorded, so the freshness metric is missing for the event flow.")
    if not data.url_checks:
        caveats.append("No URLs were probed, so `URL alive` is missing (run `check-urls`).")
    for flow, golden in (("venues", data.inputs.venues), ("events", data.inputs.events)):
        threshold = LOW_GOLDEN[Flow.VENUE if flow == "venues" else Flow.EVENT]
        small = [city.name for city in data.meta.cities if 0 < len(golden.get(city.slug, [])) < threshold]
        if small:
            caveats.append(
                f"Few known {flow} for {', '.join(small)} (below {threshold}): recall there is noisy; "
                "ask admins to list their own."
            )
    for flow, per_provider in metrics.items():
        for provider, m in per_provider.items():
            if m.error_rate is not None and m.error_rate > HIGH_ERROR_RATE:
                caveats.append(
                    f"`{provider}` failed {_pct(m.error_rate)} of its {flow.value} calls; "
                    "check its key, quota and rate limit."
                )
    skipped = [f"`{info.name}` ({info.skipped_reason})" for info in data.meta.providers if info.skipped_reason]
    if skipped:
        caveats.append("Skipped providers: " + ", ".join(skipped) + ".")
    return caveats


def render_report(
    data: RunData, metrics: Mapping[Flow, Mapping[str, ProviderMetrics]], decisions: Mapping[Flow, FlowDecision]
) -> str:
    meta = data.meta
    search_cost = sum(log.call.cost_usd for log in data.calls)
    extraction_cost = sum(log.call.cost_usd for log in data.llm_calls if log.call is not None)
    out = [f"# Bake-off report: {meta.run_id}", ""]
    if meta.dry_run:
        out += ["> **SIMULATED DATA.** Produced by `--dry-run`: no provider was called and no money was spent.", ""]
    out += [
        _table(
            ["", ""],
            [
                ["Created", f"{meta.created_at:%Y-%m-%d %H:%M} UTC (data dated {meta.today})"],
                ["Cities", ", ".join(f"{city.name} ({city.country})" for city in meta.cities)],
                ["Flows", ", ".join(flow.value for flow in meta.flows)],
                [
                    "Spend",
                    f"{_usd(search_cost + extraction_cost)} of a {_usd(meta.budget_usd)} budget "
                    f"(search {_usd(search_cost)}, extraction {_usd(extraction_cost)})",
                ],
                ["Extractor", f"`{meta.extractor_model}`" if meta.extractor_model else "none"],
                ["Event freshness filter", f"{meta.event_freshness_days} days"],
                [
                    "Labels / canary probes / URL checks",
                    f"{len(data.labels)} / {len(data.canary_observations)} / {len(data.url_checks)}",
                ],
            ],
        ),
        "",
        "### Providers",
        "",
    ]
    calls_by_provider: dict[str, int] = {}
    for log in data.calls:
        calls_by_provider[log.provider] = calls_by_provider.get(log.provider, 0) + 1
    out.append(
        _table(
            ["Provider", "Kind", "Flows", "Price", "Verified", "Calls", "Status"],
            [
                [
                    f"`{info.name}`",
                    info.kind,
                    ", ".join(flow.value for flow in info.flows),
                    f"{_usd(info.usd_per_unit)} per {info.price_unit}",
                    str(info.price_verified_on) if info.price_verified_on else "not re-verified",
                    str(calls_by_provider.get(info.name, 0)),
                    f"skipped: {info.skipped_reason}" if info.skipped_reason else "ran",
                ]
                for info in meta.providers
            ],
        )
    )
    caveats = _caveats(data, metrics)
    if caveats:
        out += ["", "### Read this first", ""] + [f"- {item}" for item in caveats]

    cities = [city.slug for city in meta.cities]
    for flow in meta.flows:
        if flow in decisions and metrics.get(flow):
            out += ["", _flow_section(flow, metrics[flow], decisions[flow], cities)]

    out += ["", "## Licensing and price notes", ""]
    out += [
        _table(
            ["Provider", "Google-derived", "Price source", "Price notes", "Vendor terms"],
            [
                [
                    f"`{info.name}`",
                    "yes" if info.google_derived else "no",
                    info.price_source or "n/a",
                    info.price_notes or "",
                    info.terms_note or "not checked",
                ]
                for info in meta.providers
            ],
        ),
        "",
        "The vendor terms above were read for this report and are not legal advice: the owner decides whether a "
        "provider may be used, and what may be stored from it, before it is enabled in production (guide section "
        "3.5). Providers marked as needing review do not run until the owner names them in `--accept-terms`. "
        "Google-derived data keeps only `place_id`/`data_id`.",
        "",
        "## Metric definitions",
        "",
        METRIC_DEFINITIONS,
    ]
    return "\n".join(out) + "\n"


def render_adr_draft(data: RunData, decisions: Mapping[Flow, FlowDecision]) -> str:
    """A pre-filled ``docs/decisions/0001-search-stack.md`` for the owner to edit; never final without a human."""
    lines = [
        "# 0001: Search stack for venue and event discovery",
        "",
        f"- Status: **draft**, generated {data.meta.created_at:%Y-%m-%d} from run `{data.meta.run_id}`",
        "- Deciders: the project owner",
        "",
        "## Context",
        "",
        "Provider choice is made by measurement (guide section 6): the same query plan went to every shortlisted "
        "provider and each was scored on realtime, price, coverage and completeness, under the floors of union recall "
        "and novel-item precision.",
        "",
        "## Decision",
        "",
    ]
    for flow in data.meta.flows:
        decision = decisions.get(flow)
        if decision is None or decision.pick is None:
            lines.append(f"- **{flow.value.capitalize()}**: no combination cleared the floors yet; see the report.")
            continue
        suffix = " (provisional: precision unverified)" if decision.pick_is_provisional else ""
        lines.append(
            f"- **{flow.value.capitalize()}**: {_combo_name(decision.pick)}{suffix}. "
            f"{_describe_combo(decision.pick, decision)}."
        )
    lines += [
        "",
        "## Consequences",
        "",
        "- Providers outside the pick stay available behind the same adapter contracts but are not scheduled.",
        "- Prices and terms are re-verified before production use; storage terms of the chosen vendors must be read.",
        "",
        "## Open items",
        "",
        "- Confirm the licensing stance on Google-derived data (guide section 14, question 8).",
        "- Re-run the bake-off when a vendor changes price or terms, or when the cities in scope change.",
        "",
    ]
    return "\n".join(lines)
