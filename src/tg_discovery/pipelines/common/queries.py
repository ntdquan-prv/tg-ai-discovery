"""The query plans: what the search providers are asked about a city.

Event runs ask twelve English questions, dated with the month and year of the run. Venue runs ask three questions for
each type the admin picked (all ten when none is picked), so the cost and the coverage follow the choice.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

from tg_discovery.domain.enums import Flow, VenueType

EVENT_FRESHNESS_DAYS = 60

# (id, template). ``{month}`` is the English month name and ``{year}`` the year of the run.
EVENT_TEMPLATES: tuple[tuple[str, str], ...] = (
    ("e01", "gay events in {city} this month"),
    ("e02", "LGBTQ+ parties in {city} this weekend"),
    ("e03", "drag shows in {city}"),
    ("e04", "lesbian events in {city}"),
    ("e05", "queer events {city} {month} {year}"),
    ("e06", "gay club night {city} tickets"),
    ("e07", "Pride {city} {year} events"),
    ("e08", "LGBTQ+ festival {city} {year}"),
    ("e09", "gay party calendar {city}"),
    ("e10", "circuit and dance party {city} gay"),
    ("e11", "trans and non-binary events {city}"),
    ("e12", "LGBTQ+ community events {city} upcoming"),
)


# Venues are not limited to LGBTQ+ places: per type, a general query, a gay or gay-friendly one and another wording.
VENUE_TYPE_TEMPLATES: dict[VenueType, tuple[str, str, str]] = {
    VenueType.CAFE: ("best cafes in {city}", "gay-friendly cafes in {city}", "coffee shops in {city} {country}"),
    VenueType.RESTAURANT: (
        "best restaurants in {city}",
        "gay-friendly restaurants in {city}",
        "popular local restaurants in {city} {country}",
    ),
    VenueType.ADULTS: (
        "gay cruise clubs in {city}",
        "gay sex clubs and darkrooms in {city}",
        "adult shops and adult clubs in {city}",
    ),
    VenueType.BARS: ("best bars in {city}", "gay bars in {city}", "cocktail bars and pubs in {city}"),
    VenueType.DANCE_CLUB: ("best nightclubs in {city}", "gay clubs in {city}", "dance clubs and discos in {city}"),
    VenueType.SAUNAS: ("saunas in {city}", "gay saunas in {city}", "bathhouses and steam rooms in {city}"),
    VenueType.MASSAGE: ("massage in {city}", "gay massage in {city}", "spas and massage centres in {city}"),
    VenueType.GYMS: ("gyms in {city}", "gay-friendly gyms in {city}", "fitness centres in {city} {country}"),
    VenueType.ATTRACTIONS: (
        "top attractions in {city}",
        "LGBTQ+ landmarks and history in {city}",
        "museums and sights in {city} {country}",
    ),
    VenueType.TOURS: ("city tours in {city}", "LGBTQ+ tours in {city}", "walking and food tours in {city}"),
}


@dataclass(frozen=True, slots=True)
class PlannedQuery:
    id: str
    text: str
    language: str
    flow: Flow
    freshness_days: int | None


def build_event_plan(
    *, city: str, country_name: str, today: date, freshness_days: int = EVENT_FRESHNESS_DAYS
) -> list[PlannedQuery]:
    """The twelve event queries for one city, asking for pages published in the last ``freshness_days``."""
    fields = {"city": city, "country": country_name, "month": today.strftime("%B"), "year": str(today.year)}
    return [
        PlannedQuery(query_id, template.format(**fields), "en", Flow.EVENT, freshness_days)
        for query_id, template in EVENT_TEMPLATES
    ]


def build_venue_plan(
    *, city: str, country_name: str, venue_types: Sequence[VenueType] | None = None
) -> list[PlannedQuery]:
    """Three queries for each venue type asked for, in the order of ``VenueType``; every type when none is given."""
    wanted = set(venue_types or VenueType)
    fields = {"city": city, "country": country_name}
    return [
        PlannedQuery(f"{venue_type.name.lower()}-{number}", template.format(**fields), "en", Flow.VENUE, None)
        for venue_type in VenueType
        if venue_type in wanted
        for number, template in enumerate(VENUE_TYPE_TEMPLATES[venue_type], start=1)
    ]
