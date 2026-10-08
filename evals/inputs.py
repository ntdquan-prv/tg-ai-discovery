"""Loads the owner-supplied CSV inputs: cities, golden venues, golden events and canary events.

Golden files accept the columns of the SQL in guide section 13.1 as exported, so a MySQL export can be used as is.
The guide's event query does not return the city, so add ``c.name AS city`` to it (see evals/README.md).
Every problem is reported with the file, the row and what to change; nothing is skipped silently.
"""

import csv
import shutil
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from evals.models import CanaryEvent, CitySpec, GoldenEvent, GoldenVenue
from tg_discovery.domain.geo import BoundingBox
from tg_discovery.domain.models import EventOccurrence
from tg_discovery.domain.urls import clean_url

CITIES_FILE = "cities.csv"
VENUES_FILE = "venues_golden.csv"
EVENTS_FILE = "events_golden.csv"
CANARY_FILE = "canary.csv"
INPUT_FILES = (CITIES_FILE, VENUES_FILE, EVENTS_FILE, CANARY_FILE)


class InputsError(ValueError):
    """An input file is missing something or contains a value that cannot be used."""


@dataclass(slots=True)
class Inputs:
    cities: list[CitySpec]
    venues: dict[str, list[GoldenVenue]] = field(default_factory=dict[str, list[GoldenVenue]])
    events: dict[str, list[GoldenEvent]] = field(default_factory=dict[str, list[GoldenEvent]])
    canaries: dict[str, list[CanaryEvent]] = field(default_factory=dict[str, list[CanaryEvent]])


def _rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        return [{(key or "").strip().lower(): (value or "").strip() for key, value in row.items()} for row in reader]


def _where(path: Path, number: int) -> str:
    return f"{path.name} row {number}"


def _need(path: Path, number: int, row: dict[str, str], column: str) -> str:
    value = row.get(column, "")
    if not value:
        msg = f"{_where(path, number)}: column '{column}' is required"
        raise InputsError(msg)
    return value


def _float(value: str) -> float | None:
    try:
        return float(value) if value else None
    except ValueError:
        return None  # MySQL stores coordinates as free text; an unusable value just means "no coordinates"


def _date(path: Path, number: int, value: str, column: str) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError as error:
        msg = f"{_where(path, number)}: '{column}' must be a date like 2026-11-14, got '{value}'"
        raise InputsError(msg) from error


def _split(value: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in value.split("|") if part.strip())


def load_cities(path: Path) -> list[CitySpec]:
    cities: list[CitySpec] = []
    for number, row in enumerate(_rows(path), start=2):
        slug = _need(path, number, row, "slug")
        timezone = _need(path, number, row, "timezone")
        try:
            ZoneInfo(timezone)
        except (ZoneInfoNotFoundError, ValueError) as error:
            msg = f"{_where(path, number)}: unknown time zone '{timezone}' (use an IANA name such as Asia/Bangkok)"
            raise InputsError(msg) from error
        lat, lng = _float(row.get("lat", "")), _float(row.get("lng", ""))
        edges = [_float(row.get(edge, "")) for edge in ("south", "west", "north", "east")]
        bbox = None
        if all(edge is not None for edge in edges):
            south, west, north, east = (edge or 0.0 for edge in edges)
            bbox = BoundingBox(south=south, west=west, north=north, east=east)
        elif any(edge is not None for edge in edges):
            msg = f"{_where(path, number)}: give all of south, west, north and east, or none of them"
            raise InputsError(msg)
        cities.append(
            CitySpec(
                slug=slug,
                name=_need(path, number, row, "name"),
                country=_need(path, number, row, "country").upper(),
                country_name=_need(path, number, row, "country_name"),
                timezone=timezone,
                languages=_split(row.get("languages", "")) or ("en",),
                aliases=_split(row.get("aliases", "")),
                tg_city_id=int(row["tg_city_id"]) if row.get("tg_city_id") else None,
                center=(lat, lng) if lat is not None and lng is not None else None,
                bbox=bbox,
            )
        )
    if not cities:
        msg = f"{path.name} lists no cities"
        raise InputsError(msg)
    slugs = [city.slug for city in cities]
    if len(set(slugs)) != len(slugs):
        msg = f"{path.name}: slugs must be unique"
        raise InputsError(msg)
    return cities


def _resolve_city(path: Path, number: int, value: str, cities: list[CitySpec]) -> str:
    wanted = value.casefold()
    for city in cities:
        if wanted == city.slug.casefold() or wanted in (name.casefold() for name in city.names):
            return city.slug
    msg = f"{_where(path, number)}: city '{value}' is not in {CITIES_FILE}; add it there or list it as an alias"
    raise InputsError(msg)


def load_golden_venues(path: Path, cities: list[CitySpec]) -> dict[str, list[GoldenVenue]]:
    venues: dict[str, list[GoldenVenue]] = defaultdict(list)
    for number, row in enumerate(_rows(path), start=2):
        venues[_resolve_city(path, number, _need(path, number, row, "city"), cities)].append(
            GoldenVenue(
                id=_need(path, number, row, "id"),
                city=_resolve_city(path, number, row["city"], cities),
                name=_need(path, number, row, "name"),
                address=row.get("address") or None,
                lat=_float(row.get("latitude", "")),
                lng=_float(row.get("longitude", "")),
                website=clean_url(row.get("website_url", "")),
                venue_type=row.get("venue_type") or None,
            )
        )
    return dict(venues)


def load_golden_events(path: Path, cities: list[CitySpec]) -> dict[str, list[GoldenEvent]]:
    """Group rows by event id: the schedule lives in several rows (one per ``recurring_dates`` entry)."""
    by_id: dict[str, GoldenEvent] = {}
    for number, row in enumerate(_rows(path), start=2):
        event_id = _need(path, number, row, "id")
        start = _date(path, number, _need(path, number, row, "start_date"), "start_date")
        end = _date(path, number, row.get("end_date", ""), "end_date")
        if start is None:
            continue
        occurrence = EventOccurrence(start_date=start, end_date=end if end and end > start else None)
        if event_id not in by_id:
            by_id[event_id] = GoldenEvent(
                id=event_id,
                city=_resolve_city(path, number, _need(path, number, row, "city"), cities),
                name=_need(path, number, row, "name"),
                venue_name=row.get("venue_name") or None,
                website=clean_url(row.get("website_url", "")),
                booking_url=clean_url(row.get("booking_url", "")),
            )
        by_id[event_id].occurrences.append(occurrence)
    events: dict[str, list[GoldenEvent]] = defaultdict(list)
    for event in by_id.values():
        events[event.city].append(event)
    return dict(events)


def load_canaries(path: Path, cities: list[CitySpec]) -> dict[str, list[CanaryEvent]]:
    canaries: dict[str, list[CanaryEvent]] = defaultdict(list)
    for number, row in enumerate(_rows(path), start=2):
        raw = _need(path, number, row, "announced_at")
        try:
            announced = datetime.fromisoformat(raw)
        except ValueError as error:
            msg = f"{_where(path, number)}: 'announced_at' must be an ISO date or datetime, got '{raw}'"
            raise InputsError(msg) from error
        city = _resolve_city(path, number, _need(path, number, row, "city"), cities)
        url = clean_url(_need(path, number, row, "url"))
        if url is None:
            msg = f"{_where(path, number)}: 'url' is not a web address"
            raise InputsError(msg)
        canaries[city].append(
            CanaryEvent(
                id=_need(path, number, row, "id"),
                city=city,
                name=_need(path, number, row, "name"),
                url=url,
                announced_at=announced if announced.tzinfo else announced.replace(tzinfo=UTC),
            )
        )
    return dict(canaries)


def load_inputs(directory: Path) -> Inputs:
    """Load every input file that exists in ``directory``; only ``cities.csv`` is mandatory."""
    cities_path = directory / CITIES_FILE
    if not cities_path.exists():
        msg = f"{cities_path} does not exist; see evals/README.md for the input files"
        raise InputsError(msg)
    cities = load_cities(cities_path)
    inputs = Inputs(cities=cities)
    if (path := directory / VENUES_FILE).exists():
        inputs.venues = load_golden_venues(path, cities)
    if (path := directory / EVENTS_FILE).exists():
        inputs.events = load_golden_events(path, cities)
    if (path := directory / CANARY_FILE).exists():
        inputs.canaries = load_canaries(path, cities)
    return inputs


def snapshot_inputs(source: Path, destination: Path) -> None:
    """Copy the input files into a run directory so the run can be replayed after the originals change."""
    destination.mkdir(parents=True, exist_ok=True)
    for name in INPUT_FILES:
        if (source / name).exists():
            shutil.copyfile(source / name, destination / name)
