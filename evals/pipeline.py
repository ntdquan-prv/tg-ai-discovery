"""From a run directory to a report: load, resolve entities, measure, decide and write the markdown."""

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from evals import store
from evals.data import RunData, load_run_data
from evals.decide import FlowDecision, decide
from evals.metrics import Analysis, ProviderMetrics, compute_all
from evals.report import render_adr_draft, render_report
from evals.store import RunDir
from tg_discovery.domain.enums import Flow


@dataclass(frozen=True, slots=True)
class Analysed:
    data: RunData
    analysis: Analysis
    metrics: Mapping[Flow, Mapping[str, ProviderMetrics]]
    decisions: Mapping[Flow, FlowDecision]


def analyse(run_dir: RunDir, *, inputs_dir: Path | None = None) -> Analysed:
    """Recompute everything from what the run stored; no provider is called."""
    data = load_run_data(run_dir, inputs_dir=inputs_dir)
    analysis = Analysis(data)
    metrics = compute_all(analysis)
    decisions = {flow: decide(analysis, flow, per_provider) for flow, per_provider in metrics.items() if per_provider}
    return Analysed(data=data, analysis=analysis, metrics=metrics, decisions=decisions)


def write_report(run_dir: RunDir, *, inputs_dir: Path | None = None) -> Path:
    """Rebuild ``report.md`` and ``adr_draft.md`` from the run directory and return the report's path."""
    analysed = analyse(run_dir, inputs_dir=inputs_dir)
    report = run_dir.file(store.REPORT)
    report.write_text(render_report(analysed.data, analysed.metrics, analysed.decisions), encoding="utf-8")
    run_dir.file("adr_draft.md").write_text(render_adr_draft(analysed.data, analysed.decisions), encoding="utf-8")
    return report
