"""The query plans: what each provider is asked about a city.

The bake-off and the event flow use the shared plan, twelve English templates per flow, so that every provider gets the
same queries and the comparison is fair. For cities where English is not the local language, the last few are replaced
by queries written in the local language, generated once per city by a cheap model and cached by the caller.

Venue runs of the service use the venue-type plan instead: three queries for each type the admin picked (all ten when
none is picked), so the cost and the coverage follow the choice.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from functools import cache
from importlib.resources import files

from tg_discovery.domain.enums import Flow, VenueType
from tg_discovery.llm.bedrock_responses import FunctionCaller
from tg_discovery.llm.schemas import LocalQueries, LocalQuery, tool_schema

PLAN_SIZE = 12
LOCAL_QUERY_COUNT = 4
EVENT_FRESHNESS_DAYS = 60

# (id, template). ``{month}`` is the English month name and ``{year}`` the year of the plan date.
VENUE_TEMPLATES: tuple[tuple[str, str], ...] = (
    ("v01", "gay bars in {city}"),
    ("v02", "gay nightclubs and dance clubs in {city}"),
    ("v03", "lesbian bars in {city}"),
    ("v04", "gay saunas and bathhouses in {city}"),
    ("v05", "gay cruise clubs and cruising bars in {city}"),
    ("v06", "LGBTQ+ friendly cafes in {city}"),
    ("v07", "gay friendly restaurants in {city}"),
    ("v08", "gay shops and bookstores in {city}"),
    ("v09", "gay gyms, spas and massage in {city}"),
    ("v10", "gay beaches near {city}"),
    ("v11", "best gay nightlife guide {city} {country}"),
    ("v12", "LGBTQ+ venues directory {city}"),
)
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


# Venues are not limited to LGBTQ+ places (the owner's call on 2026-10-13): each type has a general query, one that
# keeps the gay and gay-friendly places in view, and a third wording for more coverage. The LGBTQ+ level of what comes
# back is still assessed, as a label for the reviewer.
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


def build_query_plan(
    flow: Flow,
    *,
    city: str,
    country_name: str,
    today: date,
    local: Sequence[LocalQuery] = (),
    event_freshness_days: int = EVENT_FRESHNESS_DAYS,
) -> list[PlannedQuery]:
    """Twelve queries for one city and flow; ``local`` replaces the last English templates one for one."""
    templates = VENUE_TEMPLATES if flow is Flow.VENUE else EVENT_TEMPLATES
    freshness = event_freshness_days if flow is Flow.EVENT else None
    fields = {"city": city, "country": country_name, "month": today.strftime("%B"), "year": str(today.year)}
    english = [
        PlannedQuery(query_id, template.format(**fields), "en", flow, freshness) for query_id, template in templates
    ]
    chosen = list(local)[: min(LOCAL_QUERY_COUNT, PLAN_SIZE)]
    kept = english[: PLAN_SIZE - len(chosen)]
    prefix = "v" if flow is Flow.VENUE else "e"
    localised = [
        PlannedQuery(f"{prefix}L{number:02d}", query.text, query.language, flow, freshness)
        for number, query in enumerate(chosen, start=1)
    ]
    return [*kept, *localised]


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


@cache
def _local_query_prompt() -> str:
    return files("tg_discovery.llm").joinpath("prompts", "local_queries.md").read_text(encoding="utf-8")


async def generate_local_queries(
    llm: FunctionCaller,
    flow: Flow,
    *,
    city: str,
    country_name: str,
    languages: Sequence[str],
    count: int = LOCAL_QUERY_COUNT,
) -> list[LocalQuery]:
    """Ask a cheap model for queries a local would type; empty when the city only needs English."""
    local_languages = [language for language in languages if language != "en"]
    if not local_languages:
        return []
    kind = "gay venues (bars, clubs, saunas, cafes)" if flow is Flow.VENUE else "gay and LGBTQ+ events and parties"
    prompt = (
        f"City: {city}, {country_name}\n"
        f"Languages: {', '.join(local_languages)}\n"
        f"Kind of result: {kind}\n"
        f"Number of queries: {count}"
    )
    result = await llm.call_function(
        system=_local_query_prompt(),
        prompt=prompt,
        tool_name="emit_candidates",
        parameters=tool_schema(LocalQueries),
        max_output_tokens=2048,
    )
    queries = LocalQueries.model_validate(result.arguments)
    return [query for query in queries.queries if query.language in local_languages][:count]
