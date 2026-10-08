"""The live side of a run: the providers and the model, built from the settings, asked about one city at a time.

Every call goes through the run's budget, so a run can never spend more than ``BUDGET_USD_PER_RUN``. A failing provider
is counted and skipped; it never loses what the others found.
"""

import asyncio
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime

import httpx
import structlog

from tg_discovery.config import Settings
from tg_discovery.domain.enums import Flow
from tg_discovery.domain.geo import BoundingBox
from tg_discovery.domain.models import scrub_google_derived
from tg_discovery.llm.bedrock_responses import LlmError
from tg_discovery.llm.factory import bedrock_from_settings
from tg_discovery.pipelines.common.budget import Budget
from tg_discovery.pipelines.common.extractor import DraftExtractor, ExtractionContext, Extractor
from tg_discovery.pipelines.common.queries import PlannedQuery, build_query_plan
from tg_discovery.pipelines.discovery import AreaError, CityBatch, CityRequest, CityTarget, RunEnvironment
from tg_discovery.providers.base import (
    BudgetExceededError,
    ProviderError,
    SearchHit,
    SearchProvider,
    SearchQuery,
    VenueProvider,
    VenueQuery,
)
from tg_discovery.providers.metering import MeteredHttp
from tg_discovery.providers.opendata.nominatim import Nominatim
from tg_discovery.providers.registry import build_providers

log = structlog.get_logger()

MAX_RESULTS = 10
VENUE_RESULTS = 20
CONCURRENCY = 8
# Providers that ignore the query text are asked once per city, under this pseudo query.
AREA_QUERY = PlannedQuery(id="area", text="area", language="*", flow=Flow.VENUE, freshness_days=None)


class LiveCollector:
    """Asks every available provider the same queries about a city and reads the answers into drafts."""

    def __init__(
        self,
        *,
        search_providers: list[SearchProvider],
        venue_providers: list[VenueProvider],
        extractor: DraftExtractor | None,
        clock: Callable[[], datetime],
    ) -> None:
        self._search = search_providers
        self._venue = venue_providers
        self._extractor = extractor
        self._clock = clock

    async def collect(self, flow: Flow, city: CityTarget, *, window_days: int, today: date) -> CityBatch:
        batch = CityBatch(city)
        slots = asyncio.Semaphore(CONCURRENCY)
        stop = asyncio.Event()
        queries = build_query_plan(
            flow,
            city=city.name,
            country_name=city.country_name,
            today=today,
            local=(),
            event_freshness_days=window_days,
        )
        jobs: list[Awaitable[None]] = []
        if flow is Flow.VENUE:
            jobs += [
                self._area(provider, batch, AREA_QUERY, slots, stop) for provider in self._venue if provider.queryless
            ]
        for query in queries:
            jobs += [
                self._ask(provider, batch, query, today, slots, stop)
                for provider in self._search
                if flow in provider.flows
            ]
            if flow is Flow.VENUE:
                jobs += [
                    self._area(provider, batch, query, slots, stop)
                    for provider in self._venue
                    if not provider.queryless
                ]
        await asyncio.gather(*jobs)
        batch.stats.stopped_by_budget = stop.is_set()
        return batch

    @staticmethod
    def _failed(batch: CityBatch, provider: str, error: Exception) -> None:
        batch.stats.failed_calls += 1
        batch.stats.errors[f"{provider}: {error}"[:200]] += 1
        log.warning("provider call failed", provider=provider, error=str(error)[:200])

    async def _ask(
        self,
        provider: SearchProvider,
        batch: CityBatch,
        query: PlannedQuery,
        today: date,
        slots: asyncio.Semaphore,
        stop: asyncio.Event,
    ) -> None:
        async with slots:
            if stop.is_set():
                return
            request = SearchQuery(
                text=query.text,
                flow=query.flow,
                country=batch.city.country,
                city=batch.city.name,
                language=query.language,
                max_results=MAX_RESULTS,
                freshness_days=query.freshness_days,
            )
            try:
                result = await provider.search(request)
            except BudgetExceededError:
                stop.set()
                return
            except Exception as error:  # one provider's failure must not lose what the others found
                self._failed(batch, provider.name, error)
                return
            batch.stats.calls += 1
            await self._read(provider, batch, query, result.hits, today, stop)

    async def _read(
        self,
        provider: SearchProvider,
        batch: CityBatch,
        query: PlannedQuery,
        hits: list[SearchHit],
        today: date,
        stop: asyncio.Event,
    ) -> None:
        if self._extractor is None or not hits:
            return
        city = batch.city
        context = ExtractionContext(
            flow=query.flow,
            provider=provider.name,
            city=city.name,
            country=city.country,
            timezone=city.timezone,
            today=today,
            retrieved_at=self._clock(),
        )
        try:
            extraction = await self._extractor.extract(hits, context)
        except BudgetExceededError:
            stop.set()
            return
        except (LlmError, ProviderError) as error:
            self._failed(batch, f"{provider.name} extraction", error)
            return
        except Exception as error:
            self._failed(batch, f"{provider.name} extraction", error)
            return
        for venue in extraction.venues:
            batch.venues.append((provider.name, scrub_google_derived(venue) if provider.google_derived else venue))
            batch.stats.drafts_by_provider[provider.name] += 1
        for event in extraction.events:
            batch.events.append((provider.name, scrub_google_derived(event) if provider.google_derived else event))
            batch.stats.drafts_by_provider[provider.name] += 1

    async def _area(
        self,
        provider: VenueProvider,
        batch: CityBatch,
        query: PlannedQuery,
        slots: asyncio.Semaphore,
        stop: asyncio.Event,
    ) -> None:
        async with slots:
            if stop.is_set():
                return
            request = VenueQuery(
                text=query.text,
                country=batch.city.country,
                city=batch.city.name,
                area=batch.city.area,
                language="en" if query.language == "*" else query.language,
                max_results=VENUE_RESULTS,
            )
            try:
                result = await provider.find_venues(request)
            except BudgetExceededError:
                stop.set()
                return
            except Exception as error:
                self._failed(batch, provider.name, error)
                return
            batch.stats.calls += 1
            for venue in result.venues:
                batch.venues.append((provider.name, scrub_google_derived(venue) if provider.google_derived else venue))
                batch.stats.drafts_by_provider[provider.name] += 1


class LiveGeocoder:
    """Turns a requested city into the area to search: its coordinates when given, otherwise Nominatim's answer."""

    def __init__(self, nominatim: Nominatim | None, radius_km: float) -> None:
        self._nominatim = nominatim
        self._radius_km = radius_km
        self._cache: dict[str, BoundingBox] = {}  # Nominatim's policy asks for results to be cached

    async def locate(self, request: CityRequest, country: str) -> BoundingBox | None:
        if request.lat is not None and request.lng is not None:
            return BoundingBox.around(request.lat, request.lng, self._radius_km)
        if self._nominatim is None:
            msg = f"{request.name}: give lat and lng, or set NOMINATIM_USER_AGENT so the city can be looked up"
            raise AreaError(msg)
        key = f"{country.upper()}|{request.name.casefold()}"
        if key not in self._cache:
            found = await self._nominatim.geocode_city(request.name, country)
            if found is None:
                return None
            self._cache[key] = BoundingBox.around(found.center_lat, found.center_lng, self._radius_km)
        return self._cache[key]


@asynccontextmanager
async def live_environment(settings: Settings) -> AsyncGenerator[RunEnvironment]:
    """Providers, model and geocoder for one run, spending from one budget; closed when the run ends."""
    budget = Budget(settings.budget_usd_per_run)
    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:
        built = build_providers(
            settings, client, budget, names=settings.enabled_providers, accepted=settings.accepted_providers
        )
        llm = bedrock_from_settings(settings, client, budget) if built.search else None
        extractor = Extractor(llm, max_output_tokens=settings.extractor_max_output_tokens) if llm else None
        nominatim = None
        if settings.nominatim_user_agent:
            http = MeteredHttp(
                Nominatim.name,
                Nominatim.price,
                client,
                budget,
                max_concurrency=Nominatim.max_concurrency,
                min_interval_s=Nominatim.min_interval_s,
            )
            nominatim = Nominatim(http, settings.nominatim_user_agent)
        yield RunEnvironment(
            collector=LiveCollector(
                search_providers=built.search,
                venue_providers=built.venue,
                extractor=extractor,
                clock=lambda: datetime.now(UTC),
            ),
            geocoder=LiveGeocoder(nominatim, settings.city_radius_km),
            budget=budget,
            providers=[provider.name for provider in (*built.search, *built.venue)],
            skipped=built.skipped,
        )
