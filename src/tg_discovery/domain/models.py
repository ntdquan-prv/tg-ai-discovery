"""Draft models: what a provider or the extractor believes about one venue or one event.

Drafts hold derived facts only. They never carry raw provider payloads, and for data that comes from Google nothing but
facts and a ``place_id``: ``scrub_google_derived`` removes the rest before a draft is stored (``KEEP_GOOGLE_DETAILS``
also keeps the phone, hours, description and price).
"""

from collections.abc import Iterable
from datetime import date, datetime, time
from typing import Self, overload

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator, model_validator

from tg_discovery.domain.enums import (
    Audience,
    EventStatus,
    EventType,
    LgbtqLevel,
    OperatingStatus,
    RecurringType,
)
from tg_discovery.domain.urls import picture_key

QUOTE_MAX_CHARS = 300
DESCRIPTION_MAX_CHARS = 600
ALT_MAX_CHARS = 200
MAX_EVENT_IMAGES = 20  # picture URLs kept for one event, however many pages and sources it was found on

# Fields a venue needs to be usable without further lookups, and the optional ones that make it richer.
VENUE_REQUIRED_FIELDS = ("name", "address", "coordinates", "website")
VENUE_OPTIONAL_FIELDS = ("venue_type", "phone", "opening_hours", "description")
EVENT_REQUIRED_FIELDS = ("name", "date", "venue_or_address", "url")
EVENT_OPTIONAL_FIELDS = ("organizer", "price_text", "start_time", "description")


class _Model(BaseModel):
    """Base for drafts: blank strings become ``None`` and surrounding whitespace is stripped."""

    model_config = ConfigDict(extra="ignore")

    @field_validator("*", mode="before")
    @classmethod
    def _blank_to_none(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip() or None
        return value


class SourceRef(_Model):
    """Where a fact came from."""

    url: str
    title: str | None = None
    provider: str
    retrieved_at: datetime


class Evidence(_Model):
    """A short quote that supports the LGBTQ+ assessment."""

    quote: str
    url: str

    @field_validator("quote")
    @classmethod
    def _cap_quote(cls, value: str) -> str:
        return value[:QUOTE_MAX_CHARS]


class LgbtqAssessment(_Model):
    level: LgbtqLevel
    evidence: list[Evidence] = Field(default_factory=list[Evidence])


class _Described(_Model):
    description: str | None = None

    @field_validator("description")
    @classmethod
    def _cap_description(cls, value: str | None) -> str | None:
        return value[:DESCRIPTION_MAX_CHARS] if value else value


class VenueDraft(_Described):
    name: str
    venue_type: str | None = None
    address: str | None = None
    city: str | None = None
    country: str | None = None  # ISO 3166-1 alpha-2
    lat: float | None = Field(default=None, ge=-90, le=90)
    lng: float | None = Field(default=None, ge=-180, le=180)
    website: str | None = None
    phone: str | None = None
    socials: dict[str, str] = Field(default_factory=dict[str, str])
    opening_hours: str | None = None
    price_text: str | None = None  # as the source writes it: "$$", "$10-20"
    lgbtq: LgbtqAssessment | None = None
    operating_status: OperatingStatus = OperatingStatus.UNKNOWN
    external_ids: dict[str, str] = Field(default_factory=dict[str, str])
    sources: list[SourceRef] = Field(default_factory=list[SourceRef])

    @model_validator(mode="after")
    def _coordinates_come_in_pairs(self) -> Self:
        if (self.lat is None) != (self.lng is None):
            self.lat = self.lng = None
        return self

    @property
    def has_coordinates(self) -> bool:
        return self.lat is not None and self.lng is not None

    def _present(self) -> dict[str, bool]:
        return {
            "name": bool(self.name),
            "address": self.address is not None,
            "coordinates": self.has_coordinates,
            "website": self.website is not None,
            "venue_type": self.venue_type is not None,
            "phone": self.phone is not None,
            "opening_hours": self.opening_hours is not None,
            "description": self.description is not None,
        }

    @computed_field
    @property
    def missing_fields(self) -> list[str]:
        present = self._present()
        return [field for field in (*VENUE_REQUIRED_FIELDS, *VENUE_OPTIONAL_FIELDS) if not present[field]]

    @property
    def has_required_fields(self) -> bool:
        present = self._present()
        return all(present[field] for field in VENUE_REQUIRED_FIELDS)


class EventOccurrence(_Model):
    start_date: date
    end_date: date | None = None
    start_time: time | None = None
    end_time: time | None = None
    timezone: str | None = None
    date_inferred: bool = False  # the year was not stated and was filled in as "next occurrence"

    @property
    def last_date(self) -> date:
        return self.end_date or self.start_date


class EventVenue(_Model):
    name: str | None = None
    address: str | None = None
    city: str | None = None
    country: str | None = None
    lat: float | None = Field(default=None, ge=-90, le=90)
    lng: float | None = Field(default=None, ge=-180, le=180)
    google_place_id: str | None = None


class EventLinks(_Model):
    official: str | None = None
    tickets: str | None = None
    facebook: str | None = None
    instagram: str | None = None


class EventImage(_Model):
    """The address of a picture of an event. Only the URL is kept: the image itself is never downloaded or stored."""

    url: str
    source_url: str | None = None  # the page the picture was found on
    alt: str | None = None

    @field_validator("alt")
    @classmethod
    def _cap_alt(cls, value: str | None) -> str | None:
        return value[:ALT_MAX_CHARS] if value else value


def union_images(*groups: Iterable[EventImage]) -> list[EventImage]:
    """Every picture of the groups once, at most ``MAX_EVENT_IMAGES``: the first spelling of a picture is the one kept.

    Two addresses that are only different sizes or encodings of one picture count as one (``picture_key``).
    """
    seen: set[str] = set()
    kept: list[EventImage] = []
    for group in groups:
        for image in group:
            key = picture_key(image.url)
            if key not in seen and len(kept) < MAX_EVENT_IMAGES:
                seen.add(key)
                kept.append(image)
    return kept


class EventDraft(_Described):
    name: str
    event_type: EventType = EventType.EVENT
    recurring_type: RecurringType = RecurringType.ONEDAY
    occurrences: list[EventOccurrence] = Field(default_factory=list[EventOccurrence])
    venue: EventVenue = Field(default_factory=EventVenue)
    organizer: str | None = None
    # The organiser's own contacts, only an organisation's (see ``domain.contacts``): never a private person's.
    organizer_url: str | None = None
    organizer_email: str | None = None
    organizer_phone: str | None = None
    audience: Audience = Audience.UNKNOWN
    links: EventLinks = Field(default_factory=EventLinks)
    price_text: str | None = None
    lgbtq: LgbtqAssessment | None = None
    status: EventStatus = EventStatus.UNKNOWN
    images: list[EventImage] = Field(default_factory=list[EventImage])
    sources: list[SourceRef] = Field(default_factory=list[SourceRef])

    @property
    def first_start(self) -> date | None:
        return min((o.start_date for o in self.occurrences), default=None)

    @property
    def last_end(self) -> date | None:
        return max((o.last_date for o in self.occurrences), default=None)

    def _present(self) -> dict[str, bool]:
        return {
            "name": bool(self.name),
            "date": bool(self.occurrences),
            "venue_or_address": self.venue.name is not None or self.venue.address is not None,
            "url": self.links.official is not None or self.links.tickets is not None,
            "organizer": self.organizer is not None,
            "price_text": self.price_text is not None,
            "start_time": any(o.start_time is not None for o in self.occurrences),
            "description": self.description is not None,
        }

    @computed_field
    @property
    def missing_fields(self) -> list[str]:
        present = self._present()
        return [field for field in (*EVENT_REQUIRED_FIELDS, *EVENT_OPTIONAL_FIELDS) if not present[field]]

    @property
    def has_required_fields(self) -> bool:
        present = self._present()
        return all(present[field] for field in EVENT_REQUIRED_FIELDS)


def _without_quotes(assessment: LgbtqAssessment | None) -> LgbtqAssessment | None:
    return assessment.model_copy(update={"evidence": []}) if assessment else None


@overload
def scrub_google_derived(draft: VenueDraft, *, keep_details: bool = False) -> VenueDraft: ...
@overload
def scrub_google_derived(draft: EventDraft, *, keep_details: bool = False) -> EventDraft: ...
def scrub_google_derived(draft: VenueDraft | EventDraft, *, keep_details: bool = False) -> VenueDraft | EventDraft:
    """Keep only what may be stored from Google-derived data: names, addresses, coordinates, URLs and ids.

    Hours, phone numbers, descriptions, prices, pictures and evidence quotes can all originate in Google reviews or
    listings, so they are dropped; the LGBTQ+ level and the operating status stay because they are derived facts.
    ``keep_details`` (``KEEP_GOOGLE_DETAILS``) also keeps the phone, hours, description and price.
    """
    details: dict[str, object] = {}
    if isinstance(draft, VenueDraft):
        if not keep_details:
            details = {"phone": None, "opening_hours": None, "description": None, "price_text": None}
        return draft.model_copy(update={**details, "socials": {}, "lgbtq": _without_quotes(draft.lgbtq)})
    if not keep_details:
        details = {"description": None, "price_text": None}
    organizer = {"organizer": None, "organizer_url": None, "organizer_email": None, "organizer_phone": None}
    return draft.model_copy(update={**details, **organizer, "images": [], "lgbtq": _without_quotes(draft.lgbtq)})
