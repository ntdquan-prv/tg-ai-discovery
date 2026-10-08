"""Gives every city an area to search: a bounding box from the owner's coordinates or from Nominatim, cached on disk."""

import json
from pathlib import Path
from typing import Protocol, cast

from evals.models import CitySpec
from tg_discovery.domain.geo import BoundingBox
from tg_discovery.providers.opendata.nominatim import GeocodedCity

DEFAULT_RADIUS_KM = 15.0


class AreaError(ValueError):
    """A city has no usable location."""


class Geocoder(Protocol):
    async def geocode_city(self, city: str, country: str) -> GeocodedCity | None: ...


def _load(path: Path) -> dict[str, dict[str, object]]:
    if not path.exists():
        return {}
    return cast(dict[str, dict[str, object]], json.loads(path.read_text(encoding="utf-8")))


def _save(path: Path, cache: dict[str, dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cache, indent=2, ensure_ascii=False), encoding="utf-8")


async def resolve_areas(
    cities: list[CitySpec], geocoder: Geocoder | None, cache_path: Path, *, radius_km: float = DEFAULT_RADIUS_KM
) -> list[CitySpec]:
    """Return the cities with a bounding box and a centre.

    A box in ``cities.csv`` wins; then a centre (the box is ``radius_km`` around it); otherwise Nominatim is asked once
    and the answer is cached, as its usage policy requires.
    """
    cache = _load(cache_path)
    resolved: list[CitySpec] = []
    for city in cities:
        if city.bbox is not None:
            resolved.append(city if city.center else city.model_copy(update={"center": city.bbox.center}))
        elif city.center is not None:
            box = BoundingBox.around(city.center[0], city.center[1], radius_km)
            resolved.append(city.model_copy(update={"bbox": box}))
        else:
            key = f"{city.country}|{city.name.casefold()}"
            if key not in cache:
                if geocoder is None:
                    msg = f"{city.slug}: give lat and lng in cities.csv, or set NOMINATIM_USER_AGENT to look it up"
                    raise AreaError(msg)
                found = await geocoder.geocode_city(city.name, city.country)
                if found is None:
                    msg = (
                        f"{city.slug}: Nominatim does not know {city.name} in {city.country}; give lat and lng instead"
                    )
                    raise AreaError(msg)
                cache[key] = {
                    "center": [found.center_lat, found.center_lng],
                    "bbox": found.bbox.model_dump(),
                    "display_name": found.display_name,
                }
                _save(cache_path, cache)
            entry = cache[key]
            lat, lng = cast(list[float], entry["center"])
            resolved.append(
                city.model_copy(update={"center": (lat, lng), "bbox": BoundingBox.model_validate(entry["bbox"])})
            )
    return resolved
