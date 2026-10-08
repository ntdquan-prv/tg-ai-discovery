"""Builders for drafts and batches, so pipeline tests can state exactly what the providers found."""

from datetime import UTC, date, datetime, time, timedelta

from tg_discovery.domain.enums import EventStatus, LgbtqLevel, OperatingStatus
from tg_discovery.domain.geo import BoundingBox
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
from tg_discovery.pipelines.discovery import CityBatch, CityTarget

NOW = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)
TODAY = date(2026, 10, 8)
AREA = BoundingBox(south=9.9, west=19.9, north=10.1, east=20.1)
CITY = CityTarget(name="Testville", country="ZZ", country_name="Testland", timezone="Etc/UTC", area=AREA, tg_city_id=7)


def assessment(level: LgbtqLevel = LgbtqLevel.PRIMARY, *, quote: bool = True) -> LgbtqAssessment:
    evidence = [Evidence(quote="A bar for the community", url="https://src.test/page")] if quote else []
    return LgbtqAssessment(level=level, evidence=evidence)


def source(url: str = "https://src.test/page", provider: str = "exa") -> SourceRef:
    return SourceRef(url=url, title=None, provider=provider, retrieved_at=NOW)


def venue(
    name: str = "Rainbow Bar",
    *,
    lat: float | None = 10.0,
    lng: float | None = 20.0,
    website: str | None = "https://rainbow-bar.example",
    address: str | None = "1 Main Street",
    level: LgbtqLevel | None = LgbtqLevel.PRIMARY,
    quote: bool = True,
    status: OperatingStatus = OperatingStatus.UNKNOWN,
    external_ids: dict[str, str] | None = None,
    provider: str = "exa",
    venue_type: str | None = "Bar",
    phone: str | None = None,
    description: str | None = None,
) -> VenueDraft:
    return VenueDraft(
        name=name,
        venue_type=venue_type,
        address=address,
        lat=lat,
        lng=lng,
        website=website,
        phone=phone,
        description=description,
        lgbtq=assessment(level, quote=quote) if level else None,
        operating_status=status,
        external_ids=external_ids or {},
        sources=[source(provider=provider)],
    )


def event(
    name: str = "Drag Night",
    start: date | None = None,
    *,
    days_ahead: int = 10,
    start_time: time | None = time(21, 0),
    venue_name: str | None = "Rainbow Bar",
    address: str | None = "1 Main Street",
    official: str | None = "https://rainbow-bar.example/drag",
    tickets: str | None = None,
    level: LgbtqLevel | None = LgbtqLevel.PRIMARY,
    quote: bool = True,
    inferred: bool = False,
    status: EventStatus = EventStatus.UNKNOWN,
    provider: str = "exa",
    organizer: str | None = None,
    price: str | None = None,
    description: str | None = None,
    dated: bool = True,
) -> EventDraft:
    day = start or TODAY + timedelta(days=days_ahead)
    return EventDraft(
        name=name,
        occurrences=[EventOccurrence(start_date=day, start_time=start_time, date_inferred=inferred)] if dated else [],
        venue=EventVenue(name=venue_name, address=address),
        organizer=organizer,
        links=EventLinks(official=official, tickets=tickets),
        price_text=price,
        description=description,
        lgbtq=assessment(level, quote=quote) if level else None,
        status=status,
        sources=[source(provider=provider)],
    )


def batch(
    *, venues: list[tuple[str, VenueDraft]] | None = None, events: list[tuple[str, EventDraft]] | None = None
) -> CityBatch:
    return CityBatch(city=CITY, venues=venues or [], events=events or [])
