"""The rest of a dry run: simulated canary probes, simulated admin labels and simulated URL checks.

All of it goes through the real code paths (canary runs, label export and import, URL checking), only the answers are
made up, so a dry run proves the harness works end to end without a key or a cost.
"""

import csv
from pathlib import Path

from evals import store
from evals.data import load_run_data
from evals.environment import Environment
from evals.fakes import CHECKPOINTS, truth_for_name
from evals.inputs import Inputs
from evals.labels import export_sheet, import_sheet
from evals.metrics import Analysis
from evals.runner import Executor
from evals.store import RunDir
from evals.urlcheck import check_urls

SIMULATED_LABELS_PER_PROVIDER = 15
SIMULATED_URLS_PER_PROVIDER = 40


def _fill_sheet(sheet: Path) -> None:
    """Answer every row the way a careful admin would, using what the synthetic items really are."""
    with sheet.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    for row in rows:
        row["label"] = truth_for_name(row["name"])
        row["labeler"] = "simulated"
    if rows:
        with sheet.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)


async def finish_dry_run(executor: Executor, env: Environment, run_dir: RunDir, inputs: Inputs) -> None:
    """Probe the canaries at every checkpoint, label a sample and check URLs, all with simulated answers."""
    for hours in CHECKPOINTS:
        env.before_canary(hours)
        await executor.run_canary(hours, inputs.canaries)
    data = load_run_data(run_dir)
    summary = export_sheet(Analysis(data), run_dir, per_provider=SIMULATED_LABELS_PER_PROVIDER, seed=7)
    _fill_sheet(summary.sheet)
    import_sheet(run_dir, run_dir.file(store.LABELING_SHEET))
    data = load_run_data(run_dir)
    await check_urls(data, run_dir, env.prober, per_provider=SIMULATED_URLS_PER_PROVIDER, clock=env.clock)
