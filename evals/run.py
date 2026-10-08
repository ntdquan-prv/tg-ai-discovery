"""Command line of the bake-off harness: ``python -m evals.run <command> --help``.

Commands: ``plan`` (what a run would cost, nothing is sent), ``run`` (execute, ``--dry-run`` to simulate everything,
``--replay DIR`` to rebuild a report from a stored run), ``canary`` (freshness probe at a checkpoint), ``check-urls``,
``label-export`` and ``label-import``.

A real run never starts without ``--yes`` (or an interactive "y") after the plan has been printed, and the budget
limit stops spending even if the plan was wrong.
"""

import argparse
import asyncio
import io
import sys
from collections.abc import Callable, Coroutine, Sequence
from contextlib import AsyncExitStack
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

import httpx

from evals import store
from evals.canary import CANARY_QUERY_IDS
from evals.data import load_run_data
from evals.dryrun import finish_dry_run
from evals.environment import EXAMPLE_TODAY, Environment, SetupError, flows_of, real_environment, simulated_environment
from evals.geocode import DEFAULT_RADIUS_KM, AreaError, resolve_areas
from evals.inputs import InputsError, load_inputs, snapshot_inputs
from evals.labels import LabelError, export_sheet, import_sheet
from evals.metrics import Analysis
from evals.models import CitySpec, RunMeta
from evals.pipeline import write_report
from evals.plan import estimate, render
from evals.runner import DEFAULT_MAX_RESULTS, Executor, RunPlan
from evals.store import RunDir
from evals.urlcheck import DEFAULT_PER_PROVIDER, check_urls
from tg_discovery.config import ProviderSettings
from tg_discovery.domain.enums import Flow
from tg_discovery.llm.schemas import LocalQuery
from tg_discovery.pipelines.common.extractor import Extraction, ExtractionContext
from tg_discovery.pipelines.common.queries import EVENT_FRESHNESS_DAYS, generate_local_queries
from tg_discovery.providers.base import SearchHit
from tg_discovery.providers.fetch.httpx_fetch import HttpxUrlProber
from tg_discovery.providers.registry import known_names

GOLDEN_INPUTS = Path("evals/golden")
EXAMPLE_INPUTS = Path("evals/examples")
RESULTS = Path("results")
DEFAULT_BUDGET_USD = 5.0


class CliError(ValueError):
    """A problem with how the command was called; the message says what to change."""


def _csv(value: str | None) -> list[str] | None:
    return [part.strip() for part in value.split(",") if part.strip()] if value else None


def _select(cities: list[CitySpec], wanted: list[str] | None) -> list[CitySpec]:
    if wanted is None:
        return cities
    known = {city.slug: city for city in cities}
    unknown = [slug for slug in wanted if slug not in known]
    if unknown:
        msg = f"unknown city {', '.join(unknown)}; cities.csv has {', '.join(known)}"
        raise CliError(msg)
    return [known[slug] for slug in wanted]


def _check_provider_names(names: list[str] | None) -> None:
    unknown = [name for name in names or [] if name not in known_names()]
    if unknown:
        msg = f"unknown provider {', '.join(unknown)}; known: {', '.join(known_names())}"
        raise CliError(msg)


class _NoExtractor:
    """Placeholder for runs without search providers, which never reach the extraction step."""

    model = "none"

    async def extract(self, hits: Sequence[SearchHit], ctx: ExtractionContext) -> Extraction:
        raise RuntimeError("no extractor is configured")


def _confirm(args: argparse.Namespace) -> None:
    if args.yes:
        return
    if not sys.stdin.isatty():
        msg = "this run would call paid APIs: read the plan above and pass --yes to go ahead"
        raise CliError(msg)
    if input("Spend up to the budget limit? [y/N] ").strip().lower() != "y":
        msg = "cancelled; nothing was sent"
        raise CliError(msg)


@dataclass(slots=True)
class _Resolved:
    cities: list[CitySpec]
    local_queries: dict[tuple[Flow, str], list[LocalQuery]]


async def _local_queries(
    env: Environment, cities: Sequence[CitySpec], flows: Sequence[Flow], cache: Path, *, enabled: bool
) -> dict[tuple[Flow, str], list[LocalQuery]]:
    """Local-language queries per city and flow, generated once and cached so every run uses the same ones."""
    found: dict[tuple[Flow, str], list[LocalQuery]] = {}
    if not enabled or env.local_llm is None:
        return found
    for flow in flows:
        for city in cities:
            path = cache / f"{flow.value}-{city.slug}.json"
            if path.exists():
                found[(flow, city.slug)] = [LocalQuery.model_validate(item) for item in _read_list(path)]
                continue
            queries = await generate_local_queries(
                env.local_llm, flow, city=city.name, country_name=city.country_name, languages=city.languages
            )
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(_dump(queries), encoding="utf-8")
            found[(flow, city.slug)] = queries
    return found


def _read_list(path: Path) -> list[object]:
    import json

    loaded: object = json.loads(path.read_text(encoding="utf-8"))
    return loaded if isinstance(loaded, list) else []  # pyright: ignore[reportUnknownVariableType]


def _dump(queries: Sequence[LocalQuery]) -> str:
    import json

    return json.dumps([query.model_dump() for query in queries], ensure_ascii=False, indent=2)


# -- commands -----------------------------------------------------------------------------------------------------


async def _plan(args: argparse.Namespace) -> int:
    inputs = load_inputs(Path(args.inputs or GOLDEN_INPUTS))
    cities = _select(inputs.cities, _csv(args.cities))
    names = _csv(args.providers)
    _check_provider_names(names)
    plan = estimate(
        cities,
        flows_of(_csv(args.flows)),
        ProviderSettings(),
        args.budget_usd,
        names=names,
        accepted=_csv(args.accept_terms) or (),
        max_queries=args.max_queries,
    )
    print(render(plan))
    print("\nNothing was sent.")
    return 0


async def _run(args: argparse.Namespace) -> int:
    if args.replay:
        return _replay(Path(args.replay))
    run_id = args.run_id or ("dry-run" if args.dry_run else datetime.now().strftime("run-%Y%m%d-%H%M"))
    results = Path(args.results)
    run_dir = RunDir(results / run_id)
    if run_dir.exists() and not args.resume and not args.dry_run:
        msg = f"{run_dir.path} already exists: pass --resume to continue it or choose another --run-id"
        raise CliError(msg)
    inputs_dir = Path(args.inputs or (EXAMPLE_INPUTS if args.dry_run else GOLDEN_INPUTS))
    inputs = load_inputs(inputs_dir)
    selected = _select(inputs.cities, _csv(args.cities))
    flows = flows_of(_csv(args.flows))
    names = _csv(args.providers)
    accepted = _csv(args.accept_terms) or []
    _check_provider_names(names)
    settings = ProviderSettings()
    today = date.fromisoformat(args.today) if args.today else None

    async with AsyncExitStack() as stack:
        if args.dry_run:
            env = simulated_environment(inputs, selected, args.budget_usd, today or EXAMPLE_TODAY)
            print("Dry run: every provider, the extractor and the admins are simulated. Nothing is sent or spent.")
        else:
            env = await stack.enter_async_context(
                real_environment(settings, budget_usd=args.budget_usd, names=names, accepted=accepted, today=today)
            )
            plan = estimate(
                selected,
                flows,
                settings,
                args.budget_usd,
                names=names,
                accepted=accepted,
                max_queries=args.max_queries,
                max_results=args.max_results,
            )
            print(render(plan))
            if not env.search_providers and not env.venue_providers:
                reasons = "; ".join(f"{i.name}: {i.skipped_reason}" for i in env.infos if i.skipped_reason)
                msg = f"no provider can run ({reasons or 'none selected'})"
                raise CliError(msg)
            _confirm(args)

        cities = await resolve_areas(
            selected, env.geocoder, results / "_cache" / "geocode.json", radius_km=args.radius_km
        )
        if run_dir.exists() and args.dry_run:
            for stale in run_dir.path.glob("*"):
                if stale.is_file():
                    stale.unlink()
        snapshot_inputs(inputs_dir, run_dir.inputs)
        created = run_dir.read_meta().created_at if run_dir.exists() else env.clock()
        meta = RunMeta(
            run_id=run_id,
            created_at=created,
            today=env.today,
            dry_run=args.dry_run,
            budget_usd=args.budget_usd,
            cities=cities,
            flows=flows,
            providers=env.infos,
            event_freshness_days=args.event_freshness_days,
            extractor_model=env.extractor.model if env.extractor else None,
        )
        run_dir.write_meta(meta)

        local = await _local_queries(
            env, cities, flows, results / "_cache" / "queries", enabled=not args.no_local_queries
        )
        executor = Executor(
            RunPlan(
                cities=cities,
                flows=flows,
                search_providers=env.search_providers,
                venue_providers=env.venue_providers,
                extractor=env.extractor or _NoExtractor(),
                today=env.today,
                clock=env.clock,
                event_freshness_days=args.event_freshness_days,
                local_queries=local,
                max_results=args.max_results,
                max_queries=args.max_queries,
            ),
            run_dir,
        )
        summary = await executor.run(resume=args.resume)
        if args.dry_run:
            await finish_dry_run(executor, env, run_dir, inputs)
        stopped = summary.stopped_by_budget or executor.summary.stopped_by_budget
        run_dir.write_meta(meta.model_copy(update={"stopped_by_budget": stopped}))
        report = write_report(run_dir)

    print(
        f"{summary.calls} calls ({summary.failed_calls} failed), {summary.drafts} drafts, "
        f"${env.budget.spent_usd:.4f} spent of ${env.budget.limit_usd:.2f}"
    )
    if stopped:
        print("The budget ran out before every query was asked; re-run with --resume and a larger --budget-usd.")
    print(f"Report: {report}")
    return 0


def _replay(directory: Path) -> int:
    run_dir = RunDir(directory)
    if not run_dir.exists():
        msg = f"{directory} is not a run directory (no {store.META})"
        raise CliError(msg)
    print(f"Report: {write_report(run_dir)}  (rebuilt from the stored run; no provider was called)")
    return 0


async def _canary(args: argparse.Namespace) -> int:
    run_dir = RunDir(Path(args.run))
    if not run_dir.exists():
        msg = f"{args.run} is not a run directory: run the pilot first, then probe the canaries into it"
        raise CliError(msg)
    meta = run_dir.read_meta()
    if args.inputs:
        snapshot_inputs(Path(args.inputs), run_dir.inputs)
    inputs = load_inputs(run_dir.inputs)
    if not inputs.canaries:
        msg = "no canary events: put canary.csv in the inputs (see evals/README.md)"
        raise CliError(msg)
    names = [info.name for info in meta.providers if info.skipped_reason is None]
    accepted = _csv(args.accept_terms) or []
    settings = ProviderSettings()
    async with real_environment(settings, budget_usd=args.budget_usd, names=names, accepted=accepted) as env:
        canary_plan = estimate(
            meta.cities,
            [Flow.EVENT],
            settings,
            args.budget_usd,
            names=names,
            accepted=accepted,
            max_queries=len(CANARY_QUERY_IDS),
        )
        print(render(canary_plan))
        if not env.search_providers:
            msg = "no search provider can run for the canary probe"
            raise CliError(msg)
        _confirm(args)
        executor = Executor(
            RunPlan(
                cities=meta.cities,
                flows=[Flow.EVENT],
                search_providers=env.search_providers,
                venue_providers=[],
                extractor=_NoExtractor(),
                today=env.today,
                clock=env.clock,
                event_freshness_days=meta.event_freshness_days,
            ),
            run_dir,
        )
        recorded = await executor.run_canary(args.checkpoint_hours, inputs.canaries)
    print(f"{recorded} observations at +{args.checkpoint_hours} h, ${env.budget.spent_usd:.4f} spent")
    print(f"Report: {write_report(run_dir)}")
    return 0


async def _check_urls(args: argparse.Namespace) -> int:
    run_dir = RunDir(Path(args.run))
    if not run_dir.exists():
        msg = f"{args.run} is not a run directory"
        raise CliError(msg)
    data = load_run_data(run_dir)
    async with httpx.AsyncClient() as client:
        probed = await check_urls(data, run_dir, HttpxUrlProber(client), per_provider=args.per_provider)
    print(f"{probed} URLs probed (HEAD requests behind the SSRF guard). Report: {write_report(run_dir)}")
    return 0


async def _label_export(args: argparse.Namespace) -> int:
    run_dir = RunDir(Path(args.run))
    summary = export_sheet(Analysis(load_run_data(run_dir)), run_dir, per_provider=args.per_provider, seed=args.seed)
    print(f"{summary.items} items for the admins: {summary.sheet}")
    print("Send them labeling_sheet.csv and labeling_instructions.txt only; label_key.csv names the providers.")
    return 0


async def _label_import(args: argparse.Namespace) -> int:
    run_dir = RunDir(Path(args.run))
    summary = import_sheet(run_dir, Path(args.file))
    print(f"{summary.imported} verdicts imported ({summary.blank} rows left blank): {summary.by_label}")
    print(f"Report: {write_report(run_dir)}")
    return 0


# -- wiring ------------------------------------------------------------------------------------------------------


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m evals.run", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    def selection(sub: argparse.ArgumentParser) -> None:
        sub.add_argument("--inputs", help="folder with cities.csv and the golden files (default evals/golden)")
        sub.add_argument("--cities", help="comma-separated city slugs (default: all)")
        sub.add_argument("--flows", help="comma-separated: venue,event (default: both)")
        sub.add_argument("--providers", help="comma-separated provider names (default: all that have a key)")
        sub.add_argument("--accept-terms", help="comma-separated providers whose vendor terms the owner has reviewed")
        sub.add_argument("--budget-usd", type=float, default=DEFAULT_BUDGET_USD, help="spend limit (hard cap 50)")
        sub.add_argument(
            "--max-queries", type=int, help="ask only the first N queries of each plan (a cheap smoke test)"
        )

    plan = commands.add_parser("plan", help="print the calls and indicative cost of a run; sends nothing")
    selection(plan)
    plan.set_defaults(handler=_plan)

    run = commands.add_parser("run", help="execute a run, or simulate one with --dry-run, or rebuild a report")
    selection(run)
    run.add_argument("--run-id", help="folder name under results/ (default: a timestamp, or 'dry-run')")
    run.add_argument("--results", default=str(RESULTS), help="where runs are stored (default results)")
    run.add_argument("--yes", action="store_true", help="confirm that the run may spend up to the budget")
    run.add_argument(
        "--dry-run", action="store_true", help="simulate providers, extractor and admins; nothing is spent"
    )
    run.add_argument("--replay", metavar="DIR", help="rebuild the report of a stored run without calling anyone")
    run.add_argument("--resume", action="store_true", help="continue a run: skip the calls that already succeeded")
    run.add_argument("--event-freshness-days", type=int, default=EVENT_FRESHNESS_DAYS)
    run.add_argument("--max-results", type=int, default=DEFAULT_MAX_RESULTS, help="results asked per search call")
    run.add_argument("--no-local-queries", action="store_true", help="use the English queries only")
    run.add_argument("--radius-km", type=float, default=DEFAULT_RADIUS_KM, help="box around a city given as a centre")
    run.add_argument("--today", help="pretend today is this date (YYYY-MM-DD)")
    run.set_defaults(handler=_run)

    canary = commands.add_parser("canary", help="probe the canary events at one checkpoint of an existing run")
    canary.add_argument("--run", required=True, help="the run directory, e.g. results/pilot-001")
    canary.add_argument("--checkpoint-hours", type=int, required=True, choices=(0, 6, 24, 48))
    canary.add_argument("--inputs", help="folder with a newer canary.csv to copy into the run")
    canary.add_argument("--accept-terms")
    canary.add_argument("--budget-usd", type=float, default=2.0)
    canary.add_argument("--yes", action="store_true")
    canary.set_defaults(handler=_canary)

    urls = commands.add_parser("check-urls", help="probe a sample of each provider's own URLs for liveness")
    urls.add_argument("--run", required=True)
    urls.add_argument("--per-provider", type=int, default=DEFAULT_PER_PROVIDER)
    urls.set_defaults(handler=_check_urls)

    export = commands.add_parser("label-export", help="write the blind sheet the admins will label")
    export.add_argument("--run", required=True)
    export.add_argument("--per-provider", type=int, default=30)
    export.add_argument("--seed", type=int, default=7)
    export.set_defaults(handler=_label_export)

    imported = commands.add_parser("label-import", help="read the labelled sheet back into the run")
    imported.add_argument("--run", required=True)
    imported.add_argument("--file", required=True)
    imported.set_defaults(handler=_label_import)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(encoding="utf-8", errors="replace")  # Windows consoles default to a legacy code page
    args = _parser().parse_args(argv)
    handler: Callable[[argparse.Namespace], Coroutine[object, object, int]] = args.handler
    try:
        return asyncio.run(handler(args))
    except (CliError, InputsError, LabelError, AreaError, SetupError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
