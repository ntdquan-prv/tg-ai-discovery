"""Simulated providers, extractor and URL prober for ``--dry-run``: the harness runs end to end at no cost.

Everything here is synthetic and deterministic (seeded by provider, city and query), so a dry run is repeatable and its
report can be compared across code changes. None of it says anything about a real provider: the profiles below are
invented to make the report interesting (one provider that is cheap and stale, one fresh and noisy, one that fails...).
"""

import random
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Literal

from evals.canary import CANARY_FRESHNESS_DAYS
from evals.inputs import Inputs
from evals.models import CitySpec, Label
from tg_discovery.domain.enums import Flow, LgbtqLevel
from tg_discovery.domain.models import (
    EventDraft,
    EventLinks,
    EventOccurrence,
    EventVenue,
    Evidence,
    LgbtqAssessment,
    SourceRef,
    VenueDraft,
)
from tg_discovery.llm.bedrock_responses import LlmResult, LlmUsage
from tg_discovery.pipelines.common.extractor import Extraction, ExtractionContext, ExtractionStats
from tg_discovery.providers.base import (
    BudgetGuard,
    PriceSpec,
    ProviderCall,
    ProviderError,
    SearchHit,
    SearchQuery,
    SearchResult,
    VenueQuery,
    VenueResult,
)
from tg_discovery.providers.fetch.httpx_fetch import UrlProbe

NOVEL_POOL = 30
CHECKPOINTS = (0, 6, 24, 48)
HITS_PER_PAGE = 3
BOTH_FLOWS = frozenset({Flow.VENUE, Flow.EVENT})


@dataclass(frozen=True, slots=True)
class FakeProfile:
    name: str
    kind: Literal["search", "venue"]
    flows: frozenset[Flow]
    usd_per_call: float
    recall: float  # chance that a known record shows up in the results of one query
    noise: int  # at most this many novel items per query
    completeness: float  # chance that each optional field is filled in
    latency_ms: tuple[int, int]
    page_age_days: tuple[int, int] | None = None
    google_derived: bool = False
    queryless: bool = False
    error_rate: float = 0.0
    canary_rates: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)


PROFILES: tuple[FakeProfile, ...] = (
    FakeProfile("fake_osm", "venue", frozenset({Flow.VENUE}), 0.0, 0.5, 3, 0.9, (300, 900), queryless=True),
    FakeProfile("fake_places", "venue", frozenset({Flow.VENUE}), 0.005, 0.15, 2, 0.95, (200, 500), google_derived=True),
    FakeProfile("fake_search_a", "search", BOTH_FLOWS, 0.007, 0.12, 3, 0.7, (800, 1500), page_age_days=(30, 300)),
    FakeProfile("fake_search_b", "search", BOTH_FLOWS, 0.005, 0.08, 2, 0.5, (500, 900), page_age_days=(100, 700)),
    FakeProfile(
        "fake_search_fresh",
        "search",
        BOTH_FLOWS,
        0.008,
        0.10,
        4,
        0.8,
        (900, 2000),
        page_age_days=(0, 20),
        canary_rates=(0.6, 0.8, 0.95, 1.0),
    ),
    FakeProfile(
        "fake_flaky", "search", BOTH_FLOWS, 0.004, 0.10, 2, 0.6, (400, 700), page_age_days=(10, 200), error_rate=0.35
    ),
)


@dataclass(frozen=True, slots=True)
class VenueItem:
    name: str
    address: str | None
    lat: float | None
    lng: float | None
    website: str | None


@dataclass(frozen=True, slots=True)
class EventItem:
    name: str
    start: date | None
    venue: str | None
    website: str | None


def truth_for_name(name: str) -> Label:
    """What a simulated admin answers for a synthetic novel item; every label of the sheet shows up somewhere."""
    number = int(name.rsplit(" ", 1)[1])
    if name.startswith("Novel Event") and number % 6 == 0:
        return "past_or_cancelled"
    if number % 5 == 0:
        return "not_lgbtq"
    if number % 7 == 3:
        return "wrong_place"
    if number % 11 == 4:
        return "spam"
    return "valid"


class World:
    """The synthetic universe: TravelGay's golden records plus a pool of novel items per city."""

    def __init__(self, inputs: Inputs, cities: Sequence[CitySpec], today: date) -> None:
        self.inputs = inputs
        self.today = today
        self._cities = {name.casefold(): city for city in cities for name in city.names}
        self._novel_venues: dict[str, list[VenueItem]] = {}
        self._novel_events: dict[str, list[EventItem]] = {}

    def city(self, name: str) -> CitySpec:
        return self._cities[name.casefold()]

    def known_venues(self, city: CitySpec) -> list[VenueItem]:
        return [VenueItem(v.name, v.address, v.lat, v.lng, v.website) for v in self.inputs.venues.get(city.slug, [])]

    def known_events(self, city: CitySpec) -> list[EventItem]:
        return [
            EventItem(e.name, e.occurrences[0].start_date if e.occurrences else None, e.venue_name, e.website)
            for e in self.inputs.events.get(city.slug, [])
        ]

    def novel_venues(self, city: CitySpec) -> list[VenueItem]:
        if city.slug not in self._novel_venues:
            rng = random.Random(f"venues|{city.slug}")
            box = city.bbox
            south, west, north, east = (box.south, box.west, box.north, box.east) if box else (0.0, 0.0, 1.0, 1.0)
            pool: list[VenueItem] = []
            for number in range(1, NOVEL_POOL + 1):
                name = f"Novel Venue {city.slug} {number:02d}"
                shift = 2.0 if truth_for_name(name) == "wrong_place" else 0.0  # well outside the city
                lat, lng = rng.uniform(south, north) + shift, rng.uniform(west, east)
                pool.append(
                    VenueItem(name, f"{number} Simulated Street", lat, lng, f"https://novel-{city.slug}-{number}.test")
                )
            self._novel_venues[city.slug] = pool
        return self._novel_venues[city.slug]

    def novel_events(self, city: CitySpec) -> list[EventItem]:
        if city.slug not in self._novel_events:
            pool: list[EventItem] = []
            for number in range(1, NOVEL_POOL + 1):
                name = f"Novel Event {city.slug} {number:02d}"
                days = -20 if truth_for_name(name) == "past_or_cancelled" else 3 + number * 2
                site = f"https://novel-{city.slug}-{number}.test"
                pool.append(EventItem(name, self.today + timedelta(days=days), f"Simulated Hall {number % 5}", site))
            self._novel_events[city.slug] = pool
        return self._novel_events[city.slug]


def _slug(text: str) -> str:
    return re.sub(r"\W+", "-", text.casefold()).strip("-")[:40]


def _venue_line(rng: random.Random, completeness: float, item: VenueItem, city: str) -> str:
    """One synthetic page line; each optional field is dropped with the profile's complement probability."""

    def keep(value: str | None) -> str:
        return (value or "") if rng.random() < completeness else ""

    located = item.lat is not None and item.lng is not None and rng.random() < completeness
    coordinates = (f"{item.lat:.5f}", f"{item.lng:.5f}") if located and item.lat and item.lng else ("", "")
    return "|".join(["VENUE", item.name, keep(item.address), *coordinates, keep(item.website), "primary", city])


def _event_line(rng: random.Random, completeness: float, item: EventItem, city: str) -> str:
    def keep(value: str | None) -> str:
        return (value or "") if rng.random() < completeness else ""

    start = item.start.isoformat() if item.start else ""
    return "|".join(["EVENT", item.name, start, keep(item.venue), keep(item.website), "primary", city])


class _Fake:
    """Budget accounting, call records and seeded randomness shared by the simulated providers."""

    def __init__(self, profile: FakeProfile, world: World, guard: BudgetGuard, clock: Callable[[], datetime]) -> None:
        self._profile = profile
        self._world = world
        self._guard = guard
        self._clock = clock
        self.price = PriceSpec(
            unit="request",
            usd_per_unit=profile.usd_per_call,
            verified_on=None,
            source_url="simulated",
            notes="simulated provider",
        )
        self.checkpoint_hours = 0  # set by the dry run before each canary probe

    @property
    def name(self) -> str:
        return self._profile.name

    @property
    def host(self) -> str:
        """A neutral host name for the pages this provider returns; it must not give the provider away."""
        return f"src{PROFILES.index(self._profile)}"

    @property
    def flows(self) -> frozenset[Flow]:
        return self._profile.flows

    @property
    def google_derived(self) -> bool:
        return self._profile.google_derived

    @property
    def queryless(self) -> bool:
        return self._profile.queryless

    def _charge(self, rng: random.Random) -> ProviderCall:
        """Bill the call (or fail it, for the flaky provider) and return its record."""
        reservation = self._guard.reserve(self.name, self.price.usd_per_unit)
        failed = rng.random() < self._profile.error_rate
        low, high = self._profile.latency_ms
        call = ProviderCall(
            provider=self.name,
            endpoint="simulated",
            units=0 if failed else 1,
            cost_usd=0.0 if failed else self.price.usd_per_unit,
            latency_ms=rng.randint(low, high),
            status="error" if failed else "ok",
            http_status=503 if failed else 200,
            error="simulated failure" if failed else None,
            at=self._clock(),
        )
        self._guard.settle(reservation, call.cost_usd)
        if failed:
            raise ProviderError("simulated failure", call)
        return call


class FakeSearchProvider(_Fake):
    """Returns page text that ``FakeExtractor`` can read back into drafts; the profile shapes what is in it."""

    async def search(self, query: SearchQuery) -> SearchResult:
        city = self._world.city(query.city)
        rng = random.Random(f"{self.name}|{city.slug}|{query.flow.value}|{query.text}")
        call = self._charge(rng)
        if query.flow is Flow.EVENT and query.freshness_days == CANARY_FRESHNESS_DAYS:
            return SearchResult(
                provider=self.name, hits=self._canary_hits(city, rng), call=call, requested=query.max_results
            )
        lines = self._venue_lines(city, rng) if query.flow is Flow.VENUE else self._event_lines(city, rng)
        rng.shuffle(lines)
        pages = [lines[start : start + HITS_PER_PAGE] for start in range(0, len(lines), HITS_PER_PAGE)]
        hits = [self._hit(city, query, rng, number, page) for number, page in enumerate(pages)]
        return SearchResult(provider=self.name, hits=hits[: query.max_results], call=call, requested=query.max_results)

    def _venue_lines(self, city: CitySpec, rng: random.Random) -> list[str]:
        profile = self._profile
        known = [item for item in self._world.known_venues(city) if rng.random() < profile.recall]
        novel = rng.sample(self._world.novel_venues(city), rng.randint(0, profile.noise))
        return [_venue_line(rng, profile.completeness, item, city.name) for item in [*known, *novel]]

    def _event_lines(self, city: CitySpec, rng: random.Random) -> list[str]:
        profile = self._profile
        known = [item for item in self._world.known_events(city) if rng.random() < profile.recall]
        novel = rng.sample(self._world.novel_events(city), rng.randint(0, profile.noise))
        return [_event_line(rng, profile.completeness, item, city.name) for item in [*known, *novel]]

    def _hit(
        self, city: CitySpec, query: SearchQuery, rng: random.Random, number: int, lines: Sequence[str]
    ) -> SearchHit:
        published = None
        if self._profile.page_age_days is not None:
            low, high = self._profile.page_age_days
            published = self._clock() - timedelta(days=rng.randint(low, high))
        return SearchHit(
            url=f"https://{self.host}.example/{city.slug}/{_slug(query.text)}/{number}",
            title=f"{query.text} ({number + 1})",
            content="\n".join(lines),
            published_at=published,
        )

    def _canary_hits(self, city: CitySpec, rng: random.Random) -> list[SearchHit]:
        """Canary events show up with the probability this profile has at the current checkpoint."""
        rate = 0.0
        if self.checkpoint_hours in CHECKPOINTS:
            rate = self._profile.canary_rates[CHECKPOINTS.index(self.checkpoint_hours)]
        seen = [event for event in self._world.inputs.canaries.get(city.slug, []) if rng.random() < rate]
        return [SearchHit(url=e.url, title=e.name, content=f"{e.name} was just announced.") for e in seen]


class FakeVenueProvider(_Fake):
    """A structured provider: returns venues directly, as OpenStreetMap or a places API would."""

    async def find_venues(self, query: VenueQuery) -> VenueResult:
        city = self._world.city(query.city)
        rng = random.Random(f"{self.name}|{city.slug}|{query.text}")
        call = self._charge(rng)
        profile = self._profile
        known = [item for item in self._world.known_venues(city) if rng.random() < profile.recall]
        novel = rng.sample(self._world.novel_venues(city), rng.randint(0, profile.noise))
        venues = [self._draft(city, rng, number, item) for number, item in enumerate([*known, *novel], start=1)]
        requested = None if profile.queryless else query.max_results
        return VenueResult(provider=self.name, venues=venues, call=call, requested=requested)

    def _draft(self, city: CitySpec, rng: random.Random, number: int, item: VenueItem) -> VenueDraft:
        profile = self._profile
        source = f"https://{self.host}.example/{city.slug}/{number}"
        filled = rng.random() < profile.completeness
        id_kind = "google_place_id" if profile.google_derived else "osm"
        return VenueDraft(
            name=item.name,
            address=item.address if filled else None,
            city=city.name,
            country=city.country,
            lat=item.lat if filled else None,
            lng=item.lng if filled else None,
            website=item.website if filled else None,
            phone="+00 000 0000" if rng.random() < 0.3 else None,
            opening_hours="Daily 20:00-02:00" if profile.google_derived else None,
            external_ids={id_kind: f"{self.name}-{city.slug}-{_slug(item.name)}"},  # stable per venue, like a real id
            lgbtq=LgbtqAssessment(
                level=LgbtqLevel.PRIMARY, evidence=[Evidence(quote=f"{item.name} is a gay venue", url=source)]
            ),
            sources=[SourceRef(url=source, title=None, provider=self.name, retrieved_at=self._clock())],
        )


class FakeExtractor:
    """Reads the synthetic ``VENUE|...`` and ``EVENT|...`` lines back into drafts; stands in for the LLM extractor."""

    model = "fake-extractor"
    usd_per_1k_chars = 0.0002

    def __init__(self, guard: BudgetGuard, clock: Callable[[], datetime]) -> None:
        self._guard = guard
        self._clock = clock

    async def extract(self, hits: Sequence[SearchHit], ctx: ExtractionContext) -> Extraction:
        stats = ExtractionStats(pages_sent=len(hits))
        venues: list[VenueDraft] = []
        events: list[EventDraft] = []
        for hit in hits:
            source = SourceRef(url=hit.url, title=hit.title, provider=ctx.provider, retrieved_at=ctx.retrieved_at)
            for line in hit.content.splitlines():
                stats.items_returned += 1
                fields = line.split("|")
                if fields[0] == "VENUE" and ctx.flow is Flow.VENUE and len(fields) == 8:
                    venues.append(self._venue(fields, hit.url, source, ctx))
                elif fields[0] == "EVENT" and ctx.flow is Flow.EVENT and len(fields) == 7:
                    events.append(self._event(fields, hit.url, source, ctx))
                else:
                    stats.items_invalid += 1
        chars = sum(len(hit.content) for hit in hits)
        return Extraction(venues=venues, events=events, stats=stats, llm=self._bill(chars))

    def _bill(self, chars: int) -> LlmResult:
        cost = chars / 1000 * self.usd_per_1k_chars
        reservation = self._guard.reserve(f"llm:{self.model}", cost)
        self._guard.settle(reservation, cost)
        call = ProviderCall(
            provider=f"llm:{self.model}",
            endpoint="simulated",
            units=chars // 3,
            cost_usd=cost,
            latency_ms=300,
            status="ok",
            at=self._clock(),
        )
        usage = LlmUsage(input_tokens=chars // 3, output_tokens=chars // 10)
        return LlmResult(model=self.model, arguments={}, usage=usage, call=call)

    @staticmethod
    def _venue(fields: list[str], url: str, source: SourceRef, ctx: ExtractionContext) -> VenueDraft:
        _, name, address, lat, lng, website, level, city = fields
        evidence = Evidence(quote=f"{name} is a gay venue", url=url)
        return VenueDraft(
            name=name,
            address=address or None,
            city=city,
            country=ctx.country,
            lat=float(lat) if lat else None,
            lng=float(lng) if lng else None,
            website=website or None,
            lgbtq=LgbtqAssessment(level=LgbtqLevel(level), evidence=[evidence]),
            sources=[source],
        )

    @staticmethod
    def _event(fields: list[str], url: str, source: SourceRef, ctx: ExtractionContext) -> EventDraft:
        _, name, start, venue, website, level, city = fields
        evidence = Evidence(quote=f"{name} is a gay event", url=url)
        return EventDraft(
            name=name,
            occurrences=[EventOccurrence(start_date=date.fromisoformat(start))] if start else [],
            venue=EventVenue(name=venue or None, city=city, country=ctx.country),
            links=EventLinks(official=website or None),
            lgbtq=LgbtqAssessment(level=LgbtqLevel(level), evidence=[evidence]),
            sources=[source],
        )


class FakeLocalQueryLlm:
    """Stands in for the model that writes local-language queries: two canned queries per requested language."""

    model = "fake-local-queries"

    async def call_function(
        self, *, system: str, prompt: str, tool_name: str, parameters: dict[str, object], max_output_tokens: int
    ) -> LlmResult:
        city = re.search(r"City: ([^,\n]+)", prompt)
        languages = re.search(r"Languages: ([^\n]+)", prompt)
        name = city.group(1) if city else "city"
        queries = [
            {"text": f"{name} {word} ({language})", "language": language}
            for language in (languages.group(1).split(", ") if languages else [])
            for word in ("bar", "party")
        ]
        call = ProviderCall(
            provider=f"llm:{self.model}",
            endpoint="simulated",
            units=0,
            cost_usd=0.0,
            latency_ms=1,
            status="ok",
            at=example_clock(),
        )
        return LlmResult(model=self.model, arguments={"queries": queries}, usage=LlmUsage(), call=call)


class FakeUrlProber:
    """Deterministic liveness: the synthetic sites answer, except every ninth one and anything marked as dead."""

    async def probe(self, url: str) -> UrlProbe:
        number = re.search(r"-(\d+)\.test", url)
        if "dead" in url or (number and int(number.group(1)) % 9 == 0):
            return UrlProbe(status="dead", http_status=404, reason="simulated")
        if "blocked" in url:
            return UrlProbe(status="blocked", http_status=403, reason="simulated")
        return UrlProbe(status="alive", http_status=200)


def build_fakes(
    world: World, guard: BudgetGuard, clock: Callable[[], datetime]
) -> tuple[list[FakeSearchProvider], list[FakeVenueProvider]]:
    """Every simulated provider, split by kind the way the runner wants them."""
    search = [FakeSearchProvider(p, world, guard, clock) for p in PROFILES if p.kind == "search"]
    venue = [FakeVenueProvider(p, world, guard, clock) for p in PROFILES if p.kind == "venue"]
    return search, venue


def example_clock() -> datetime:
    """The fixed instant of the dry run, so its output is identical from one run to the next."""
    return datetime(2026, 10, 8, 12, 0, tzinfo=UTC)
