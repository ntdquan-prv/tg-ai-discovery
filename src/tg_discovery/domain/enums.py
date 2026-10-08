"""Vocabularies shared across the service. Values match the TravelGay vocabulary where one exists."""

from enum import StrEnum


class Flow(StrEnum):
    """The two discovery flows; each has its own pipeline, tables and budget."""

    VENUE = "venue"
    EVENT = "event"


class LgbtqLevel(StrEnum):
    """How strongly a venue or event is tied to the LGBTQ+ community."""

    PRIMARY = "primary"  # exists for an LGBTQ+ audience
    FRIENDLY = "friendly"  # explicitly welcoming, but not exclusively LGBTQ+
    UNCLEAR = "unclear"
    NO = "no"


class OperatingStatus(StrEnum):
    OPEN = "open"
    TEMPORARILY_CLOSED = "temporarily_closed"
    PERMANENTLY_CLOSED = "permanently_closed"
    UNKNOWN = "unknown"


class EventType(StrEnum):
    EVENT = "event"
    VENUE_EVENT = "venue_event"
    PRIDE = "pride"


class RecurringType(StrEnum):
    ONEDAY = "oneday"
    WEEKLY = "weekly"
    CUSTOM = "custom"
    MONTHLY = "monthly"


class Audience(StrEnum):
    LESBIAN = "lesbian"
    GAY = "gay"
    BI = "bi"
    TRANS = "trans"
    QUEER = "queer"
    LGBT_ONLY = "lgbt_only"
    MIXED = "mixed"
    UNKNOWN = "unknown"


class EventStatus(StrEnum):
    SCHEDULED = "scheduled"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"
