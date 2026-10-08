"""Executes a bake-off run: every provider gets the same queries and every result is stored as derived facts only.

The order of work is query-major (all providers answer query 1, then all answer query 2, ...) so a budget that runs out
halfway still leaves every provider with the same queries answered and the comparison fair. Nothing here keeps a raw
provider response: calls are logged as counts, URLs and costs, and results are stored as drafts.
"""

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime

import structlog

from evals import store
from evals.canary import CANARY_FRESHNESS_DAYS, CANARY_QUERY_IDS, first_match
from evals.models import CallLog, CanaryEvent, CanaryObservation, CitySpec, DraftRow, LlmLog
from evals.store import RunDir
from tg_discovery.domain.enums import Flow
from tg_discovery.domain.models import EventDraft, VenueDraft, scrub_google_derived
from tg_discovery.llm.bedrock_responses import LlmError, LlmUsage
from tg_discovery.llm.schemas import LocalQuery
from tg_discovery.pipelines.common.extractor import DraftExtractor, ExtractionContext, ExtractionStats
from tg_discovery.pipelines.common.queries import PlannedQuery, build_query_plan
from tg_discovery.providers.base import (
    BudgetExceededError,
    ProviderCall,
    ProviderError,
    SearchHit,
    SearchProvider,
    SearchQuery,
    VenueProvider,
    VenueQuery,
)

DEFAULT_MAX_RESULTS = 10
DEFAULT_VENUE_RESULTS = 20
# Providers that ignore the query text are asked once per city, under this pseudo query.
AREA_QUERY = PlannedQuery(id="area", text="area", language="*", flow=Flow.VENUE, freshness_days=None)

log = structlog.get_logger()


@dataclass(slots=True)
class RunPlan:
    """Everything a run needs; providers and the extractor arrive built, so tests and the dry run can swap them."""

    cities: Sequence[CitySpec]
    flows: Sequence[Flow]
    search_providers: Sequence[SearchProvider]
    venue_providers: Sequence[VenueProvider]
    extractor: DraftExtractor
    today: date
    clock: Callable[[], datetime]
    event_freshness_days: int
    local_queries: Mapping[tuple[Flow, str], Sequence[LocalQuery]] = field(
        default_factory=dict[tuple[Flow, str], Sequence[LocalQuery]]
    )
    max_results: int = DEFAULT_MAX_RESULTS
    max_queries: int | None = None  # a smoke test asks only the first few queries of each plan
    concurrency: int = 8


@dataclass(slots=True)
class RunSummary:
    calls: int = 0
    failed_calls: int = 0
    drafts: int = 0
    stopped_by_budget: bool = False


class Executor:
    def __init__(self, plan: RunPlan, run_dir: RunDir) -> None:
        self._plan = plan
        self._run_dir = run_dir
        self._slots = asyncio.Semaphore(plan.concurrency)
        self._stop = asyncio.Event()
        self.summary = RunSummary()

    def _queries(self, city: CitySpec, flow: Flow, *, freshness: int | None = None) -> list[PlannedQuery]:
        return build_query_plan(
            flow,
            city=city.name,
            country_name=city.country_name,
            today=self._plan.today,
            local=self._plan.local_queries.get((flow, city.slug), ()),
            event_freshness_days=freshness or self._plan.event_freshness_days,
        )

    async def run(self, *, resume: bool = False) -> RunSummary:
        """Run every (city, flow, query, provider) combination, skipping the calls a previous run already finished."""
        done: set[tuple[str, str, str, str]] = self._run_dir.completed_calls() if resume else set()
        jobs: list[Awaitable[None]] = []
        for city in self._plan.cities:
            for flow in self._plan.flows:
                jobs += self._jobs(city, flow, done)
        await asyncio.gather(*jobs)
        self.summary.stopped_by_budget = self._stop.is_set()
        return self.summary

    def _jobs(self, city: CitySpec, flow: Flow, done: set[tuple[str, str, str, str]]) -> list[Awaitable[None]]:
        def todo(provider: str, query_id: str) -> bool:
            return (provider, flow.value, city.slug, query_id) not in done

        jobs: list[Awaitable[None]] = []
        if flow is Flow.VENUE:
            jobs += [
                self._area_job(provider, city, AREA_QUERY)
                for provider in self._plan.venue_providers
                if provider.queryless and todo(provider.name, AREA_QUERY.id)
            ]
        for query in self._queries(city, flow)[: self._plan.max_queries]:
            jobs += [
                self._search_job(provider, city, query)
                for provider in self._plan.search_providers
                if flow in provider.flows and todo(provider.name, query.id)
            ]
            if flow is Flow.VENUE:
                jobs += [
                    self._area_job(provider, city, query)
                    for provider in self._plan.venue_providers
                    if not provider.queryless and todo(provider.name, query.id)
                ]
        return jobs

    # -- search providers ------------------------------------------------------------------------------------------

    async def _search_job(self, provider: SearchProvider, city: CitySpec, query: PlannedQuery) -> None:
        async with self._slots:
            if self._stop.is_set():
                return
            hits = await self._search(provider, city, query, query_id=query.id)
            if hits is not None:
                await self._extract(provider, city, query, hits)

    async def _search(
        self, provider: SearchProvider, city: CitySpec, query: PlannedQuery, *, query_id: str
    ) -> list[SearchHit] | None:
        request = SearchQuery(
            text=query.text,
            flow=query.flow,
            country=city.country,
            city=city.name,
            language=query.language,
            max_results=self._plan.max_results,
            freshness_days=query.freshness_days,
        )
        try:
            result = await provider.search(request)
        except BudgetExceededError:
            self._stop.set()
            return None
        except ProviderError as error:
            self._log_call(provider.name, city, query, query_id, request.max_results, [], [], error.call)
            return None
        except Exception as error:
            # One adapter bug must not lose the rest of a paid run: record it as a failed call and carry on.
            crashed = self._crashed(provider.name, error)
            self._log_call(provider.name, city, query, query_id, request.max_results, [], [], crashed)
            return None
        self._log_call(
            provider.name,
            city,
            query,
            query_id,
            result.requested,
            [hit.url for hit in result.hits],
            [hit.published_at for hit in result.hits],
            result.call,
        )
        return result.hits

    async def _extract(
        self, provider: SearchProvider, city: CitySpec, query: PlannedQuery, hits: list[SearchHit]
    ) -> None:
        context = ExtractionContext(
            flow=query.flow,
            provider=provider.name,
            city=city.name,
            country=city.country,
            timezone=city.timezone,
            today=self._plan.today,
            retrieved_at=self._plan.clock(),
        )

        def record(
            *,
            model: str,
            usage: LlmUsage | None = None,
            call: ProviderCall | None = None,
            stats: ExtractionStats | None = None,
            error: str | None = None,
        ) -> None:
            self._run_dir.append(
                store.LLM_CALLS,
                LlmLog(
                    provider=provider.name,
                    flow=query.flow,
                    city=city.slug,
                    query_id=query.id,
                    model=model,
                    usage=usage,
                    call=call,
                    stats=stats,
                    error=error,
                ),
            )

        try:
            extraction = await self._plan.extractor.extract(hits, context)
        except BudgetExceededError:
            self._stop.set()
            return
        except (LlmError, ProviderError) as error:
            usage = error.usage if isinstance(error, LlmError) else None
            record(model=self._plan.extractor.model, usage=usage, call=error.call, error=str(error))
            return
        except Exception as error:
            # Same policy as for searches: log the failure and keep the run going.
            log.warning("extraction crashed", provider=provider.name, error=repr(error))
            record(model=self._plan.extractor.model, error=f"{type(error).__name__}: {error}")
            return
        if extraction.llm is not None:
            record(
                model=extraction.llm.model,
                usage=extraction.llm.usage,
                call=extraction.llm.call,
                stats=extraction.stats,
            )
        for venue in extraction.venues:
            self._store_draft(
                provider, city, query, venue=scrub_google_derived(venue) if provider.google_derived else venue
            )
        for event in extraction.events:
            self._store_draft(
                provider, city, query, event=scrub_google_derived(event) if provider.google_derived else event
            )

    # -- structured venue providers --------------------------------------------------------------------------------

    async def _area_job(self, provider: VenueProvider, city: CitySpec, query: PlannedQuery) -> None:
        async with self._slots:
            if self._stop.is_set():
                return
            if city.bbox is None:
                msg = f"{city.slug} has no bounding box; resolve the cities before running"
                raise ValueError(msg)
            request = VenueQuery(
                text=query.text,
                country=city.country,
                city=city.name,
                area=city.bbox,
                language="en" if query.language == "*" else query.language,
                max_results=DEFAULT_VENUE_RESULTS,
            )
            try:
                result = await provider.find_venues(request)
            except BudgetExceededError:
                self._stop.set()
                return
            except ProviderError as error:
                self._log_call(provider.name, city, query, query.id, request.max_results, [], [], error.call)
                return
            except Exception as error:
                crashed = self._crashed(provider.name, error)
                self._log_call(provider.name, city, query, query.id, request.max_results, [], [], crashed)
                return
            self._log_call(
                provider.name,
                city,
                query,
                query.id,
                result.requested,
                [venue.sources[0].url for venue in result.venues if venue.sources],
                [],
                result.call,
                returned=len(result.venues),
            )
            for venue in result.venues:
                self._store_draft(
                    provider, city, query, venue=scrub_google_derived(venue) if provider.google_derived else venue
                )

    # -- recording -------------------------------------------------------------------------------------------------

    def _crashed(self, provider: str, error: Exception) -> ProviderCall:
        """A call record for an adapter that raised something other than ``ProviderError``."""
        log.warning("adapter crashed", provider=provider, error=repr(error))
        return ProviderCall(
            provider=provider,
            endpoint="unknown",
            units=0,
            cost_usd=0,
            latency_ms=0,
            status="error",
            error=f"{type(error).__name__}: {error}"[:300],
            at=self._plan.clock(),
        )

    def _log_call(
        self,
        provider: str,
        city: CitySpec,
        query: PlannedQuery,
        query_id: str,
        requested: int | None,
        hit_urls: list[str],
        hit_dates: list[datetime | None],
        call: ProviderCall,
        *,
        returned: int | None = None,
    ) -> None:
        self.summary.calls += 1
        self.summary.failed_calls += call.status != "ok"
        self._run_dir.append(
            store.CALLS,
            CallLog(
                provider=provider,
                flow=query.flow,
                city=city.slug,
                query_id=query_id,
                language=query.language,
                requested=requested,
                returned=len(hit_urls) if returned is None else returned,
                hit_urls=hit_urls,
                hit_dates=hit_dates,
                outcome=call.status,
                call=call,
            ),
        )

    def _store_draft(
        self,
        provider: SearchProvider | VenueProvider,
        city: CitySpec,
        query: PlannedQuery,
        *,
        venue: VenueDraft | None = None,
        event: EventDraft | None = None,
    ) -> None:
        self.summary.drafts += 1
        self._run_dir.append(
            store.DRAFTS,
            DraftRow(
                provider=provider.name,
                flow=query.flow,
                city=city.slug,
                query_id=query.id,
                language=query.language,
                venue=venue,
                event=event,
            ),
        )

    # -- canary ----------------------------------------------------------------------------------------------------

    async def run_canary(self, checkpoint_hours: int, canaries: Mapping[str, Sequence[CanaryEvent]]) -> int:
        """Run the event queries once and record which canary events each provider's results contained."""
        recorded = 0
        for city in self._plan.cities:
            events = canaries.get(city.slug, ())
            if not events:
                continue
            queries = [
                query
                for query in self._queries(city, Flow.EVENT, freshness=CANARY_FRESHNESS_DAYS)
                if query.id in CANARY_QUERY_IDS
            ]
            for provider in self._plan.search_providers:
                if Flow.EVENT not in provider.flows:
                    continue
                batches = await asyncio.gather(
                    *(self._canary_search(provider, city, query, checkpoint_hours) for query in queries)
                )
                hits = [hit for batch in batches if batch for hit in batch]
                for event in events:
                    matched = first_match(event, hits)
                    self._run_dir.append(
                        store.CANARY,
                        CanaryObservation(
                            checkpoint_hours=checkpoint_hours,
                            provider=provider.name,
                            canary_id=event.id,
                            seen=matched is not None,
                            matched_url=matched,
                            at=self._plan.clock(),
                        ),
                    )
                    recorded += 1
        self.summary.stopped_by_budget = self._stop.is_set()
        return recorded

    async def _canary_search(
        self, provider: SearchProvider, city: CitySpec, query: PlannedQuery, checkpoint_hours: int
    ) -> list[SearchHit] | None:
        async with self._slots:
            if self._stop.is_set():
                return None
            return await self._search(provider, city, query, query_id=f"canary{checkpoint_hours}h-{query.id}")
