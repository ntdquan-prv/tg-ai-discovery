"""City geocoding with the public Nominatim instance: a city name and a country code give a centre and a bounding box.

This is a geocoder, not a venue provider: the planning step calls it once per city and must cache the answer. Usage
policy, read on 2026-10-08 (https://operations.osmfoundation.org/policies/nominatim/): "No heavy uses (an absolute
maximum of 1 request per second)", "Provide a valid HTTP Referer or User-Agent identifying the application (stock
User-Agents as set by http libraries will not do)", "Results must be cached on your side", a single thread, and
scripts that run at regular intervals are limited to 4 requests per minute. The data is ODbL (see
``osm_overpass.ODBL_ATTRIBUTION``).
"""

import math
from datetime import date
from typing import Literal, cast

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from tg_discovery.domain.geo import BoundingBox
from tg_discovery.providers.base import PriceSpec, ProviderCall, ProviderError
from tg_discovery.providers.metering import MeteredHttp

SEARCH_URL = "https://nominatim.openstreetmap.org/search"
PRICE = PriceSpec(
    unit="request",
    usd_per_unit=0.0,
    verified_on=date(2026, 10, 8),
    source_url="https://operations.osmfoundation.org/policies/nominatim/",
    notes=(
        "Public instance, free within its usage policy: at most 1 request per second from one thread, results cached, "
        "4 requests per minute for scripts that run at regular intervals."
    ),
)

OsmType = Literal["node", "way", "relation"]

_ERROR_TEXT_CHARS = 200
_OSM_TYPES: tuple[OsmType, ...] = ("node", "way", "relation")


class GeocodedCity(BaseModel):
    """Where a city is: the centre Nominatim reports and the bounding box of the OSM object it matched."""

    model_config = ConfigDict(frozen=True)

    display_name: str
    center_lat: float = Field(ge=-90, le=90)
    center_lng: float = Field(ge=-180, le=180)
    bbox: BoundingBox
    osm_type: OsmType
    osm_id: int


class Nominatim:
    """Finds a city with Nominatim's structured search."""

    name = "nominatim"
    google_derived = False
    price = PRICE
    max_concurrency = 1  # the policy asks for a single thread
    min_interval_s = 1.1  # "an absolute maximum of 1 request per second", plus a margin for network jitter

    def __init__(self, http: MeteredHttp, user_agent: str) -> None:
        if not user_agent.strip():
            msg = "Nominatim requires a User-Agent that identifies the application"
            raise ValueError(msg)
        self._http = http
        self._user_agent = user_agent.strip()

    async def geocode_city(self, city: str, country: str) -> GeocodedCity | None:
        """Look a city up inside one country.

        Args:
            city: The city name, in any language Nominatim knows.
            country: An ISO 3166-1 alpha-2 code; Nominatim never returns a place outside this country.

        Returns:
            The best match, or ``None`` when Nominatim knows no such place.

        Raises:
            ValueError: If ``city`` is blank or ``country`` is not a two-letter code.
        """
        name, code = city.strip(), country.strip().lower()
        if not name or len(code) != 2 or not (code.isascii() and code.isalpha()):
            msg = f"expected a city name and an ISO 3166-1 alpha-2 country code, got {city!r} and {country!r}"
            raise ValueError(msg)
        response, call = await self._http.request(
            "GET",
            SEARCH_URL,
            endpoint="search",
            headers={"User-Agent": self._user_agent, "Accept": "application/json"},
            params={"city": name, "countrycodes": code, "format": "jsonv2", "limit": 1},
            estimated_units=1.0,
        )
        results = _results(response, call)
        if not results:
            return None
        found = _geocoded(_dict(results[0]), name)
        if found is None:
            raise _unusable(call, "the first result has no usable position, bounding box or OSM id")
        return found


def _unusable(call: ProviderCall, reason: str) -> ProviderError:
    """A 200 response that cannot be used is a failed call like any other, so it is logged as one."""
    failed = call.model_copy(update={"status": "error", "error": reason[:_ERROR_TEXT_CHARS]})
    return ProviderError(f"{call.provider} {call.endpoint}: {failed.error}", failed)


def _results(response: httpx.Response, call: ProviderCall) -> list[object]:
    try:
        decoded: object = response.json()
    except ValueError:
        raise _unusable(call, "the response is not JSON") from None
    if not isinstance(decoded, list):
        raise _unusable(call, "the response is not a JSON array")
    return cast(list[object], decoded)


def _dict(value: object) -> dict[str, object]:
    return cast(dict[str, object], value) if isinstance(value, dict) else {}


def _list(value: object) -> list[object]:
    return cast(list[object], value) if isinstance(value, list) else []


def _text(value: object) -> str | None:
    return (value.strip() or None) if isinstance(value, str) else None


def _number(value: object) -> float | None:
    """Nominatim sends coordinates as strings and the OSM id as an integer or a string."""
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        return None
    try:
        number = float(value)
    except ValueError:
        return None
    return number if math.isfinite(number) else None


def _geocoded(item: dict[str, object], searched_name: str) -> GeocodedCity | None:
    lat, lng, osm_id = _number(item.get("lat")), _number(item.get("lon")), _number(item.get("osm_id"))
    osm_type = _text(item.get("osm_type"))
    # The documented order of boundingbox is min latitude, max latitude, min longitude, max longitude (S, N, W, E).
    bounds = [_number(value) for value in _list(item.get("boundingbox"))]
    if (
        lat is None
        or lng is None
        or osm_id is None
        or not osm_id.is_integer()
        or osm_type not in _OSM_TYPES
        or len(bounds) != 4
        or any(value is None for value in bounds)
    ):
        return None
    south, north, west, east = cast(list[float], bounds)
    try:
        return GeocodedCity(
            display_name=_text(item.get("display_name")) or searched_name,
            center_lat=lat,
            center_lng=lng,
            bbox=BoundingBox(south=south, west=west, north=north, east=east),
            osm_type=osm_type,
            osm_id=int(osm_id),
        )
    except ValidationError:  # coordinates outside the valid range
        return None
