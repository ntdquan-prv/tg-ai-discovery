"""The providers the service knows about, what each one needs to run and how to build it.

Shared by the discovery pipelines and the bake-off harness. A provider whose credential is not configured is skipped
and the caller says so; nothing is ever called without a key. Providers whose terms need the owner's review (Brave,
Parallel, SerpApi) are built only when the owner has accepted them by name. SerpApi (the events pack of Google Search,
and Google Maps) is Google-derived data (guide section 14, question 8). Serper is not listed, and nor is DataForSEO,
whose Google Events endpoint was retired on 2026-09-15 like SerpApi's own Google Events API; the Tier 2 providers are
added only if the first round does not clear the floors.
"""

from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass

import httpx

from tg_discovery.config import ProviderSettings
from tg_discovery.domain.enums import Flow
from tg_discovery.providers.base import BudgetGuard, PriceSpec, SearchProvider, VenueProvider
from tg_discovery.providers.maps.brave_place import BravePlace
from tg_discovery.providers.maps.serpapi_maps import SerpApiMaps
from tg_discovery.providers.metering import DEFAULT_TIMEOUT_S, MeteredHttp
from tg_discovery.providers.opendata.osm_overpass import OsmOverpass
from tg_discovery.providers.search.brave import BraveLlmContext, BraveWebSearch
from tg_discovery.providers.search.exa import ExaSearch, worst_case_usd
from tg_discovery.providers.search.parallel import ParallelSearch
from tg_discovery.providers.search.perplexity import PerplexitySearch
from tg_discovery.providers.search.serpapi_events import SerpApiEvents
from tg_discovery.providers.search.tavily import TavilySearch


@dataclass(frozen=True, slots=True)
class Entry[P]:
    """How to recognise, price and build one provider."""

    name: str
    flows: frozenset[Flow]
    price: PriceSpec
    google_derived: bool
    setting: str  # the environment variable that holds its credential, for messages
    credential: Callable[[ProviderSettings], str | None]
    max_concurrency: int
    min_interval_s: float
    make: Callable[[str, MeteredHttp], P]
    queryless: bool = False
    terms: str | None = None  # what the vendor's terms say about storing results or benchmarking
    requires_acceptance: bool = False  # not built until the owner has accepted it by name
    retry_wait_s: float = 1.0  # Overpass asks to wait much longer than most APIs after a 429
    worst_case: Callable[[int], float] | None = None  # dollars one call can cost for N results, if not the list price
    timeout_s: float = DEFAULT_TIMEOUT_S  # SerpApi is slow enough to need more


BRAVE_TERMS = (
    "Brave API terms of 1 Sep 2026, read on 2026-10-08: 3(b)(i) forbids storing or caching Search Results beyond "
    "transient use, and 3(b)(xiii) forbids using them to create, evaluate or benchmark AI models or services. Ask "
    "Brave whether the bake-off is allowed, and hold off on storing drafts built from its results until it answers."
)
EXA_TERMS = (
    "Exa terms 4.2(a), reported by a research agent and not re-checked: no copying or reproducing obtained "
    "information without written permission; no explicit clause on storage or benchmarking."
)
OSM_TERMS = (
    "Overpass public instance (OSM wiki, read by a research agent): User-Agent required, no parallel scripts, and "
    "commercial use should run on a self-hosted or paid Overpass server. Data is ODbL: keep the attribution."
)
PARALLEL_TERMS = (
    "Parallel customer terms (no date shown), read on 2026-10-08: 2(b) says Output must not be copied, cached, stored "
    "or made available to other parties beyond one End Customer; 2(c) forbids building databases from Output and "
    "giving third parties benchmark results of the Services without Parallel's written consent. Ask Parallel first."
)
SERPAPI_TERMS = (
    "SerpApi terms (serpapi.com/legal, last updated 2026-08-27, read on 2026-10-08 through a summarising fetch "
    "tool): no clause found on storing results or on benchmarking; clause 2 forbids reselling the Service and clause "
    "13 leaves the use of the data to the customer. The open question is Google's side: Google v. SerpApi is still "
    "running (guide section 3.5). Run it only if the owner accepts Google-derived data (guide section 14, question 8)."
)
TAVILY_TERMS = "Tavily terms of 2026-05-04, reported by a research agent and not re-checked: no clause on storage."
PERPLEXITY_TERMS = "Perplexity terms could not be fetched (HTTP 403): storage and benchmarking terms are not verified."


def _secret(value: object) -> str | None:
    getter = getattr(value, "get_secret_value", None)
    return str(getter()) if getter is not None else None


BOTH = frozenset({Flow.VENUE, Flow.EVENT})
VENUE_ONLY = frozenset({Flow.VENUE})
EVENT_ONLY = frozenset({Flow.EVENT})

SEARCH_ENTRIES: tuple[Entry[SearchProvider], ...] = (
    Entry(
        BraveWebSearch.name,
        BOTH,
        BraveWebSearch.price,
        False,
        "BRAVE_API_KEY",
        lambda s: _secret(s.brave_api_key),
        BraveWebSearch.max_concurrency,
        BraveWebSearch.min_interval_s,
        lambda key, http: BraveWebSearch(key, http),
        terms=BRAVE_TERMS,
        requires_acceptance=True,
    ),
    Entry(
        BraveLlmContext.name,
        BOTH,
        BraveLlmContext.price,
        False,
        "BRAVE_API_KEY",
        lambda s: _secret(s.brave_api_key),
        BraveLlmContext.max_concurrency,
        BraveLlmContext.min_interval_s,
        lambda key, http: BraveLlmContext(key, http),
        terms=BRAVE_TERMS,
        requires_acceptance=True,
    ),
    Entry(
        ExaSearch.name,
        BOTH,
        ExaSearch.price,
        False,
        "EXA_API_KEY",
        lambda s: _secret(s.exa_api_key),
        ExaSearch.max_concurrency,
        ExaSearch.min_interval_s,
        lambda key, http: ExaSearch(key, http),
        worst_case=lambda results: worst_case_usd("auto", results),
        terms=EXA_TERMS,
    ),
    Entry(
        TavilySearch.name,
        BOTH,
        TavilySearch.price,
        False,
        "TAVILY_API_KEY",
        lambda s: _secret(s.tavily_api_key),
        TavilySearch.max_concurrency,
        TavilySearch.min_interval_s,
        lambda key, http: TavilySearch(key, http),
        terms=TAVILY_TERMS,
    ),
    Entry(
        PerplexitySearch.name,
        BOTH,
        PerplexitySearch.price,
        False,
        "PERPLEXITY_API_KEY",
        lambda s: _secret(s.perplexity_api_key),
        PerplexitySearch.max_concurrency,
        PerplexitySearch.min_interval_s,
        lambda key, http: PerplexitySearch(key, http),
        terms=PERPLEXITY_TERMS,
    ),
    Entry(
        ParallelSearch.name,
        BOTH,
        ParallelSearch.price,
        False,
        "PARALLEL_API_KEY",
        lambda s: _secret(s.parallel_api_key),
        ParallelSearch.max_concurrency,
        ParallelSearch.min_interval_s,
        lambda key, http: ParallelSearch(key, http),
        terms=PARALLEL_TERMS,
        requires_acceptance=True,
    ),
    Entry(
        SerpApiEvents.name,
        EVENT_ONLY,
        SerpApiEvents.price,
        True,
        "SERPAPI_API_KEY",
        lambda s: _secret(s.serpapi_api_key),
        SerpApiEvents.max_concurrency,
        SerpApiEvents.min_interval_s,
        lambda key, http: SerpApiEvents(key, http),
        terms=SERPAPI_TERMS,
        requires_acceptance=True,
        timeout_s=SerpApiEvents.timeout_s,
    ),
)

VENUE_ENTRIES: tuple[Entry[VenueProvider], ...] = (
    Entry(
        OsmOverpass.name,
        VENUE_ONLY,
        OsmOverpass.price,
        False,
        "NOMINATIM_USER_AGENT",
        lambda s: s.nominatim_user_agent,
        OsmOverpass.max_concurrency,
        OsmOverpass.min_interval_s,
        lambda agent, http: OsmOverpass(http, agent),
        queryless=True,
        terms=OSM_TERMS,
        retry_wait_s=30.0,
    ),
    Entry(
        BravePlace.name,
        VENUE_ONLY,
        BravePlace.price,
        False,
        "BRAVE_API_KEY",
        lambda s: _secret(s.brave_api_key),
        BravePlace.max_concurrency,
        BravePlace.min_interval_s,
        lambda key, http: BravePlace(http, key),
        terms=BRAVE_TERMS,
        requires_acceptance=True,
    ),
    Entry(
        SerpApiMaps.name,
        VENUE_ONLY,
        SerpApiMaps.price,
        True,
        "SERPAPI_API_KEY",
        lambda s: _secret(s.serpapi_api_key),
        SerpApiMaps.max_concurrency,
        SerpApiMaps.min_interval_s,
        lambda key, http: SerpApiMaps(http, key),
        terms=SERPAPI_TERMS,
        requires_acceptance=True,
        timeout_s=SerpApiMaps.timeout_s,
    ),
)


@dataclass(slots=True)
class Built:
    search: list[SearchProvider]
    venue: list[VenueProvider]
    skipped: dict[str, str]  # provider name -> why it was not built


def known_names() -> list[str]:
    return [entry.name for entry in (*SEARCH_ENTRIES, *VENUE_ENTRIES)]


SERVICE_HINT = "add {name} to ACCEPTED_TERMS"


def terms_refusal(entry: Entry[object], accepted: Collection[str], *, hint: str = SERVICE_HINT) -> str | None:
    """Why a provider may not run before the owner has reviewed its terms, or ``None`` when it may.

    ``hint`` says how the owner accepts it where the caller runs; ``{name}`` stands for the provider.
    """
    if entry.requires_acceptance and entry.name not in accepted:
        return f"its terms need review first: {hint.format(name=entry.name)} once the owner has agreed"
    return None


def _build[P](
    entries: Sequence[Entry[P]],
    wanted: Sequence[str] | None,
    settings: ProviderSettings,
    client: httpx.AsyncClient,
    guard: BudgetGuard,
    accepted: Collection[str],
    hint: str,
    skipped: dict[str, str],
) -> list[P]:
    built: list[P] = []
    for entry in entries:
        if wanted is not None and entry.name not in wanted:
            continue
        refusal = terms_refusal(entry, accepted, hint=hint)
        if refusal is not None:
            skipped[entry.name] = refusal
            continue
        credential = entry.credential(settings)
        if not credential:
            skipped[entry.name] = f"{entry.setting} is not set"
            continue
        http = MeteredHttp(
            entry.name,
            entry.price,
            client,
            guard,
            max_concurrency=entry.max_concurrency,
            min_interval_s=entry.min_interval_s,
            retry_wait_s=entry.retry_wait_s,
            timeout_s=entry.timeout_s,
        )
        built.append(entry.make(credential, http))
    return built


def build_providers(
    settings: ProviderSettings,
    client: httpx.AsyncClient,
    guard: BudgetGuard,
    *,
    names: Sequence[str] | None = None,
    accepted: Collection[str] = (),
    hint: str = SERVICE_HINT,
) -> Built:
    """Build every wanted provider that has a credential and whose terms the owner accepted; list the rest."""
    skipped: dict[str, str] = {}
    return Built(
        search=_build(SEARCH_ENTRIES, names, settings, client, guard, accepted, hint, skipped),
        venue=_build(VENUE_ENTRIES, names, settings, client, guard, accepted, hint, skipped),
        skipped=skipped,
    )
