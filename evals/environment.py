"""What a run needs from the outside world, built for real (keys and network) or simulated (the dry run)."""

from collections.abc import AsyncGenerator, Callable, Collection, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime

import httpx

from evals.fakes import (
    PROFILES,
    FakeExtractor,
    FakeLocalQueryLlm,
    FakeUrlProber,
    World,
    build_fakes,
    example_clock,
)
from evals.geocode import Geocoder
from evals.inputs import Inputs
from evals.models import CitySpec, ProviderInfo
from evals.registry import CLI_HINT, provider_infos
from tg_discovery.config import ProviderSettings
from tg_discovery.domain.enums import Flow
from tg_discovery.llm.bedrock_responses import BedrockResponsesClient, FunctionCaller
from tg_discovery.llm.factory import LlmNotConfiguredError, bedrock_from_settings
from tg_discovery.pipelines.common.budget import Budget
from tg_discovery.pipelines.common.extractor import DraftExtractor, Extractor
from tg_discovery.providers.base import SearchProvider, VenueProvider
from tg_discovery.providers.fetch.httpx_fetch import HttpxUrlProber, UrlProber
from tg_discovery.providers.metering import MeteredHttp
from tg_discovery.providers.opendata.nominatim import Nominatim
from tg_discovery.providers.registry import build_providers

EXAMPLE_TODAY = date(2026, 10, 8)


class SetupError(RuntimeError):
    """The run cannot be set up with the current configuration."""


def _ignore(_hours: int) -> None:
    return None


@dataclass(slots=True)
class Environment:
    budget: Budget
    search_providers: list[SearchProvider]
    venue_providers: list[VenueProvider]
    extractor: DraftExtractor | None
    local_llm: FunctionCaller | None
    geocoder: Geocoder | None
    prober: UrlProber
    infos: list[ProviderInfo]
    today: date
    clock: Callable[[], datetime]
    simulated: bool
    before_canary: Callable[[int], None] = field(default=_ignore)


def simulated_environment(
    inputs: Inputs, cities: Sequence[CitySpec], budget_usd: float, today: date = EXAMPLE_TODAY
) -> Environment:
    """Synthetic providers, extractor and prober: the whole harness runs without a key, a network or a cost."""
    budget = Budget(budget_usd)
    clock = example_clock
    world = World(inputs, cities, today)
    search, venue = build_fakes(world, budget, clock)
    infos = [
        ProviderInfo(
            name=profile.name,
            kind=profile.kind,
            flows=sorted(profile.flows),
            google_derived=profile.google_derived,
            price_unit="request",
            usd_per_unit=profile.usd_per_call,
            price_source="simulated",
            price_notes="simulated provider",
        )
        for profile in PROFILES
    ]

    def set_checkpoint(hours: int) -> None:
        for provider in search:
            provider.checkpoint_hours = hours

    return Environment(
        budget=budget,
        search_providers=list(search),
        venue_providers=list(venue),
        extractor=FakeExtractor(budget, clock),
        local_llm=FakeLocalQueryLlm(),
        geocoder=None,
        prober=FakeUrlProber(),
        infos=infos,
        today=today,
        clock=clock,
        simulated=True,
        before_canary=set_checkpoint,
    )


def _bedrock(settings: ProviderSettings, client: httpx.AsyncClient, budget: Budget) -> BedrockResponsesClient:
    try:
        return bedrock_from_settings(settings, client, budget)
    except LlmNotConfiguredError as error:
        msg = f"{error}, or restrict the run to providers that need no extraction"
        raise SetupError(msg) from error


def _geocoder(settings: ProviderSettings, client: httpx.AsyncClient, budget: Budget) -> Nominatim | None:
    if not settings.nominatim_user_agent:
        return None
    http = MeteredHttp(
        Nominatim.name,
        Nominatim.price,
        client,
        budget,
        max_concurrency=Nominatim.max_concurrency,
        min_interval_s=Nominatim.min_interval_s,
    )
    return Nominatim(http, settings.nominatim_user_agent)


@asynccontextmanager
async def real_environment(
    settings: ProviderSettings,
    *,
    budget_usd: float,
    names: Sequence[str] | None = None,
    accepted: Collection[str] = (),
    today: date | None = None,
) -> AsyncGenerator[Environment]:
    """Providers whose key is set and whose terms were accepted, plus the extractor, geocoder and URL prober."""
    budget = Budget(budget_usd)
    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:
        built = build_providers(settings, client, budget, names=names, accepted=accepted, hint=CLI_HINT)
        llm = _bedrock(settings, client, budget) if built.search else None
        yield Environment(
            budget=budget,
            search_providers=built.search,
            venue_providers=built.venue,
            extractor=Extractor(llm, max_output_tokens=settings.extractor_max_output_tokens) if llm else None,
            local_llm=llm,
            geocoder=_geocoder(settings, client, budget),
            prober=HttpxUrlProber(client),
            infos=provider_infos(names, built.skipped),
            today=today or datetime.now(UTC).date(),
            clock=lambda: datetime.now(UTC),
            simulated=False,
        )


def flows_of(values: Sequence[str] | None) -> list[Flow]:
    """``--flows`` as flows; both when nothing was asked for."""
    if not values:
        return [Flow.VENUE, Flow.EVENT]
    try:
        return [Flow(value) for value in values]
    except ValueError as error:
        msg = f"unknown flow in {', '.join(values)}; use venue and/or event"
        raise SetupError(msg) from error
