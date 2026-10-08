"""The shared query plan: the same queries go to every provider so the comparison is fair.

Each flow has twelve English templates. For cities where English is not the local language, the last few are replaced
by queries written in the local language, generated once per city by a cheap model and cached by the caller.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from functools import cache
from importlib.resources import files

from tg_discovery.domain.enums import Flow
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
