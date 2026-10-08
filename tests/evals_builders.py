"""Builders for hand-made bake-off runs, so metric and decision tests can state exactly what the run contains."""

from collections.abc import Sequence
from datetime import UTC, date, datetime

from evals.data import RunData, build_run_data
from evals.inputs import Inputs
from evals.models import (
    CallLog,
    CanaryObservation,
    CitySpec,
    DraftRow,
    GoldenEvent,
    GoldenVenue,
    LabelRecord,
    ProviderInfo,
    RunMeta,
    UrlCheck,
)
from tg_discovery.domain.enums import Flow, LgbtqLevel
from tg_discovery.domain.fingerprint import event_fingerprint, venue_keys
from tg_discovery.domain.geo import BoundingBox
from tg_discovery.domain.models import (
    EventDraft,
    EventLinks,
    EventOccurrence,
    EventVenue,
    Evidence,
    LgbtqAssessment,
    VenueDraft,
)
from tg_discovery.providers.base import ProviderCall

NOW = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)
TODAY = date(2026, 10, 8)
CITY = "testville"


def city(slug: str = CITY, *, languages: tuple[str, ...] = ("en",)) -> CitySpec:
    return CitySpec(
        slug=slug,
        name=slug.capitalize(),
        country="ZZ",
        country_name="Testland",
        timezone="Etc/UTC",
        languages=languages,
        center=(10.0, 20.0),
        bbox=BoundingBox(south=9.9, west=19.9, north=10.1, east=20.1),
    )


def info(
    name: str, *, flows: Sequence[Flow] = (Flow.VENUE,), google: bool = False, kind: str = "search"
) -> ProviderInfo:
    return ProviderInfo(
        name=name,
        kind="venue" if kind == "venue" else "search",
        flows=list(flows),
        google_derived=google,
        price_unit="request",
        usd_per_unit=0.01,
    )


def call_log(
    provider: str,
    *,
    flow: Flow = Flow.VENUE,
    query_id: str = "v01",
    returned: int = 3,
    requested: int | None = 10,
    cost: float = 0.01,
    latency_ms: int = 500,
    outcome: str = "ok",
    urls: Sequence[str] = (),
    dates: Sequence[datetime | None] = (),
    at: datetime = NOW,
    language: str = "en",
    city_slug: str = CITY,
) -> CallLog:
    status = "ok" if outcome == "ok" else ("timeout" if outcome == "timeout" else "error")
    return CallLog(
        provider=provider,
        flow=flow,
        city=city_slug,
        query_id=query_id,
        language=language,
        requested=requested,
        returned=returned,
        hit_urls=list(urls),
        hit_dates=list(dates),
        outcome=status,
        call=ProviderCall(
            provider=provider,
            endpoint="search",
            units=1,
            cost_usd=cost,
            latency_ms=latency_ms,
            status=status,
            at=at,
        ),
    )


def venue_row(
    provider: str,
    name: str,
    *,
    lat: float | None = None,
    lng: float | None = None,
    website: str | None = None,
    address: str | None = None,
    city_name: str | None = None,
    query_id: str = "v01",
    language: str = "en",
    evidence: bool = True,
    city_slug: str = CITY,
) -> DraftRow:
    lgbtq = LgbtqAssessment(
        level=LgbtqLevel.PRIMARY,
        evidence=[Evidence(quote=f"{name} is a gay bar", url="https://src.test/page")] if evidence else [],
    )
    return DraftRow(
        provider=provider,
        flow=Flow.VENUE,
        city=city_slug,
        query_id=query_id,
        language=language,
        venue=VenueDraft(name=name, lat=lat, lng=lng, website=website, address=address, city=city_name, lgbtq=lgbtq),
    )


def event_row(
    provider: str,
    name: str,
    start: date | None,
    *,
    end: date | None = None,
    venue: str | None = None,
    url: str | None = None,
    query_id: str = "e01",
    language: str = "en",
    city_slug: str = CITY,
) -> DraftRow:
    return DraftRow(
        provider=provider,
        flow=Flow.EVENT,
        city=city_slug,
        query_id=query_id,
        language=language,
        event=EventDraft(
            name=name,
            occurrences=[EventOccurrence(start_date=start, end_date=end)] if start else [],
            venue=EventVenue(name=venue),
            links=EventLinks(official=url),
        ),
    )


def golden_venue(
    venue_id: str, name: str, *, lat: float | None = None, lng: float | None = None, website: str | None = None
) -> GoldenVenue:
    return GoldenVenue(id=venue_id, city=CITY, name=name, lat=lat, lng=lng, website=website)


def golden_event(event_id: str, name: str, start: date, *, venue: str | None = None) -> GoldenEvent:
    return GoldenEvent(
        id=event_id, city=CITY, name=name, venue_name=venue, occurrences=[EventOccurrence(start_date=start)]
    )


def keys_of(row: DraftRow) -> list[str]:
    """The identity keys the labels are matched on."""
    if row.venue is not None:
        return venue_keys(row.venue, row.city)
    assert row.event is not None
    return [event_fingerprint(row.event, row.city)]


def label(row: DraftRow, verdict: str, item_id: str = "L1") -> LabelRecord:
    return LabelRecord.model_validate({"item_id": item_id, "entity_id": "x", "keys": keys_of(row), "label": verdict})


def make_data(
    *,
    providers: Sequence[ProviderInfo],
    flows: Sequence[Flow] = (Flow.VENUE,),
    calls: Sequence[CallLog] = (),
    drafts: Sequence[DraftRow] = (),
    venues: Sequence[GoldenVenue] = (),
    events: Sequence[GoldenEvent] = (),
    labels: Sequence[LabelRecord] = (),
    canary: Sequence[CanaryObservation] = (),
    url_checks: Sequence[UrlCheck] = (),
    cities: Sequence[CitySpec] | None = None,
) -> RunData:
    the_cities = list(cities or [city()])
    meta = RunMeta(
        run_id="test",
        created_at=NOW,
        today=TODAY,
        dry_run=False,
        budget_usd=5.0,
        cities=the_cities,
        flows=list(flows),
        providers=list(providers),
        event_freshness_days=60,
    )
    inputs = Inputs(
        cities=the_cities,
        venues={CITY: list(venues)} if venues else {},
        events={CITY: list(events)} if events else {},
    )
    return build_run_data(meta, inputs, list(calls), [], list(drafts), list(canary), list(url_checks), list(labels))
