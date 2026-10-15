"""What the extraction model fills in, and the JSON schema handed to it as a function signature.

These are deliberately separate from the drafts in ``domain.models``: the model supplies only what it read on the
pages (plus the URLs it read it from), while provenance, timestamps, ids and the city's country and time zone are added
by code so that a page cannot make the model claim them.
"""

from enum import StrEnum
from typing import cast

from pydantic import BaseModel, Field, field_validator, model_validator

from tg_discovery.domain.enums import Audience, EventStatus, EventType, LgbtqLevel, OperatingStatus, RecurringType

_TEXT_FIELD = (str, str | None)


def _coerce[E: StrEnum](enum_type: type[E], value: object, default: E) -> E:
    """Map an unknown label from the model to a default instead of rejecting the whole record."""
    try:
        return enum_type(value)
    except ValueError:
        return default


class ExtractedEvidence(BaseModel):
    quote: str = Field(description="A verbatim quote of at most 300 characters copied from the page.")
    url: str = Field(description="The URL of the page the quote was copied from (one of the input pages).")


class ExtractedLgbtq(BaseModel):
    level: LgbtqLevel
    evidence: list[ExtractedEvidence] = Field(default_factory=list[ExtractedEvidence], max_length=2)

    @field_validator("level", mode="before")
    @classmethod
    def _lenient_level(cls, value: object) -> LgbtqLevel:
        return _coerce(LgbtqLevel, value, LgbtqLevel.UNCLEAR)


class _TextModel(BaseModel):
    """Base of the records the extraction model fills in: it sometimes wraps a single text in a list."""

    @model_validator(mode="before")
    @classmethod
    def _join_lists_in_text_fields(cls, data: object) -> object:
        """Read ``["a", "b"]`` as ``"a, b"`` where a text is expected (gpt-oss did it for every venue_type seen)."""
        if not isinstance(data, dict):
            return data
        fixed = dict(cast(dict[str, object], data))
        for name, field in cls.model_fields.items():
            value = fixed.get(name)
            if isinstance(value, list) and field.annotation in _TEXT_FIELD:
                texts = [item.strip() for item in cast(list[object], value) if isinstance(item, str) and item.strip()]
                fixed[name] = ", ".join(texts) or None
        return fixed


class ExtractedVenue(_TextModel):
    name: str
    venue_type: str | None = Field(
        default=None,
        description=(
            "One of Cafe, Restaurant, Adults, Bars, Dance Club, Saunas, Massage, Gyms, Attractions, Tours; "
            "null when the pages do not make it clear."
        ),
    )
    address: str | None = None
    city: str | None = Field(default=None, description="The city the page places this venue in.")
    lat: float | None = Field(default=None, description="Only if the page states coordinates.")
    lng: float | None = None
    website: str | None = Field(default=None, description="The venue's own website, never a social page or listing.")
    phone: str | None = None
    instagram: str | None = None
    facebook: str | None = None
    opening_hours: str | None = None
    description: str | None = Field(default=None, description="At most 600 characters, in your own words.")
    lgbtq: ExtractedLgbtq
    operating_status: OperatingStatus = OperatingStatus.UNKNOWN
    source_urls: list[str] = Field(description="URLs of the input pages that mention this venue.")

    @field_validator("operating_status", mode="before")
    @classmethod
    def _lenient_status(cls, value: object) -> OperatingStatus:
        return _coerce(OperatingStatus, value, OperatingStatus.UNKNOWN)


class VenueExtraction(BaseModel):
    venues: list[ExtractedVenue]


class ExtractedOccurrence(BaseModel):
    start_date: str = Field(description="ISO 8601 date, YYYY-MM-DD.")
    end_date: str | None = None
    start_time: str | None = Field(default=None, description="24-hour local time, HH:MM.")
    end_time: str | None = None
    date_inferred: bool = Field(default=False, description="True when the year was not stated and was filled in.")


class ExtractedEvent(_TextModel):
    name: str
    event_type: EventType = EventType.EVENT
    recurring_type: RecurringType = RecurringType.ONEDAY
    occurrences: list[ExtractedOccurrence] = Field(default_factory=list[ExtractedOccurrence])
    venue_name: str | None = None
    venue_address: str | None = None
    venue_city: str | None = None
    organizer: str | None = None
    audience: Audience = Audience.UNKNOWN
    official_url: str | None = Field(default=None, description="The event's own page or the organiser's site.")
    tickets_url: str | None = None
    facebook_url: str | None = None
    instagram_url: str | None = None
    price_text: str | None = None
    description: str | None = Field(default=None, description="At most 600 characters, in your own words.")
    lgbtq: ExtractedLgbtq
    status: EventStatus = EventStatus.UNKNOWN
    source_urls: list[str] = Field(description="URLs of the input pages that mention this event.")

    @field_validator("event_type", mode="before")
    @classmethod
    def _lenient_event_type(cls, value: object) -> EventType:
        return _coerce(EventType, value, EventType.EVENT)

    @field_validator("recurring_type", mode="before")
    @classmethod
    def _lenient_recurring_type(cls, value: object) -> RecurringType:
        return _coerce(RecurringType, value, RecurringType.ONEDAY)

    @field_validator("audience", mode="before")
    @classmethod
    def _lenient_audience(cls, value: object) -> Audience:
        return _coerce(Audience, value, Audience.UNKNOWN)

    @field_validator("status", mode="before")
    @classmethod
    def _lenient_status(cls, value: object) -> EventStatus:
        return _coerce(EventStatus, value, EventStatus.UNKNOWN)


class EventExtraction(BaseModel):
    events: list[ExtractedEvent]


class LocalQuery(BaseModel):
    text: str
    language: str = Field(description="ISO 639-1 code of the language the query is written in.")


class LocalQueries(BaseModel):
    queries: list[LocalQuery]


def tool_schema(model: type[BaseModel]) -> dict[str, object]:
    """JSON schema of ``model`` with every ``$ref`` inlined and the noisy ``title`` keys removed."""
    schema = model.model_json_schema()
    definitions = cast(dict[str, object], schema.pop("$defs", {}))
    return cast(dict[str, object], _inline(schema, definitions))


def _inline(node: object, definitions: dict[str, object]) -> object:
    if isinstance(node, dict):
        fields = cast(dict[str, object], node)
        reference = fields.get("$ref")
        if isinstance(reference, str):
            return _inline(definitions[reference.rsplit("/", 1)[-1]], definitions)
        return {
            key: _inline(value, definitions)
            for key, value in fields.items()
            if not (key == "title" and isinstance(value, str))
        }
    if isinstance(node, list):
        return [_inline(item, definitions) for item in cast(list[object], node)]
    return node
