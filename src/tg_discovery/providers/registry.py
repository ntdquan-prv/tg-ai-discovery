"""The providers the service knows about, what each one needs to run and how to build it.

A provider whose credential is not configured is skipped and the run says so; nothing is ever called without a key.
Providers flagged ``requires_acceptance`` (SerpApi) are built only when named in ``ACCEPTED_TERMS``. SerpApi data (the
events pack of Google Search, and Google Maps) is Google-derived, a legal grey area.

What each vendor's terms say, as read for the trial:

- Exa, terms 4.2(a), from a secondary summary, not verified on the vendor's page: no copying or reproducing obtained
  information without written permission; no explicit clause on storage.
- OSM Overpass, public instance (OSM wiki): User-Agent required, no parallel scripts, and commercial use should run on
  a self-hosted or paid Overpass server. Data is ODbL: keep the attribution.
- SerpApi (serpapi.com/legal, last updated 2026-08-27, read on 2026-10-08, not reviewed by counsel): no clause found
  on storing results; clause 2 forbids reselling the Service and clause 13 leaves the use of the data to the customer.
  The open question is Google's side: Google v. SerpApi is pending.
"""

from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass

import httpx

from tg_discovery.config import ProviderSettings
from tg_discovery.providers.base import BudgetGuard, PriceSpec, SearchProvider, VenueProvider
from tg_discovery.providers.maps.serpapi_maps import SerpApiMaps
from tg_discovery.providers.metering import DEFAULT_TIMEOUT_S, MeteredHttp
from tg_discovery.providers.opendata.osm_overpass import OsmOverpass
from tg_discovery.providers.search.exa import ExaSearch
from tg_discovery.providers.search.serpapi_events import SerpApiEvents


@dataclass(frozen=True, slots=True)
class Entry[P]:
    """How to recognise, price and build one provider."""

    name: str
    price: PriceSpec
    setting: str  # the environment variable that holds its credential, for messages
    credential: Callable[[ProviderSettings], str | None]
    max_concurrency: int
    min_interval_s: float
    make: Callable[[str, MeteredHttp], P]
    requires_acceptance: bool = False  # built only when named in ACCEPTED_TERMS
    retry_wait_s: float = 1.0  # Overpass asks to wait much longer than most APIs after a 429
    timeout_s: float = DEFAULT_TIMEOUT_S  # SerpApi is slow enough to need more


def _secret(value: object) -> str | None:
    getter = getattr(value, "get_secret_value", None)
    return str(getter()) if getter is not None else None


SEARCH_ENTRIES: tuple[Entry[SearchProvider], ...] = (
    Entry(
        ExaSearch.name,
        ExaSearch.price,
        "EXA_API_KEY",
        lambda s: _secret(s.exa_api_key),
        ExaSearch.max_concurrency,
        ExaSearch.min_interval_s,
        lambda key, http: ExaSearch(key, http),
    ),
    Entry(
        SerpApiEvents.name,
        SerpApiEvents.price,
        "SERPAPI_API_KEY",
        lambda s: _secret(s.serpapi_api_key),
        SerpApiEvents.max_concurrency,
        SerpApiEvents.min_interval_s,
        lambda key, http: SerpApiEvents(key, http),
        requires_acceptance=True,
        timeout_s=SerpApiEvents.timeout_s,
    ),
)

VENUE_ENTRIES: tuple[Entry[VenueProvider], ...] = (
    Entry(
        OsmOverpass.name,
        OsmOverpass.price,
        "NOMINATIM_USER_AGENT",
        lambda s: s.nominatim_user_agent,
        OsmOverpass.max_concurrency,
        OsmOverpass.min_interval_s,
        lambda agent, http: OsmOverpass(http, agent),
        retry_wait_s=30.0,
    ),
    Entry(
        SerpApiMaps.name,
        SerpApiMaps.price,
        "SERPAPI_API_KEY",
        lambda s: _secret(s.serpapi_api_key),
        SerpApiMaps.max_concurrency,
        SerpApiMaps.min_interval_s,
        lambda key, http: SerpApiMaps(http, key),
        requires_acceptance=True,
        timeout_s=SerpApiMaps.timeout_s,
    ),
)


@dataclass(slots=True)
class Built:
    search: list[SearchProvider]
    venue: list[VenueProvider]
    skipped: dict[str, str]  # provider name -> why it was not built


def _build[P](
    entries: Sequence[Entry[P]],
    wanted: Sequence[str] | None,
    settings: ProviderSettings,
    client: httpx.AsyncClient,
    guard: BudgetGuard,
    accepted: Collection[str],
    skipped: dict[str, str],
) -> list[P]:
    built: list[P] = []
    for entry in entries:
        if wanted is not None and entry.name not in wanted:
            continue
        if entry.requires_acceptance and entry.name not in accepted:
            skipped[entry.name] = f"its terms must be accepted first: add {entry.name} to ACCEPTED_TERMS"
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
) -> Built:
    """Build every wanted provider that has a credential and whose terms were accepted; list the rest."""
    skipped: dict[str, str] = {}
    return Built(
        search=_build(SEARCH_ENTRIES, names, settings, client, guard, accepted, skipped),
        venue=_build(VENUE_ENTRIES, names, settings, client, guard, accepted, skipped),
        skipped=skipped,
    )
