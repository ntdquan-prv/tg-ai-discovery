"""OpenStreetMap venues from the public Overpass API: every object in an area that is tagged ``lgbtq=*``.

The query ignores ``VenueQuery.text`` (the harness calls it once per city), covers nodes, ways and relations, has no
result cap and nothing is deduplicated. Usage policy of the main instance, read on 2026-10-08 (OSM wiki, "Overpass
API"): under 10,000 queries and 1 GB a day is fair use, regular jobs should divide that by 100, requests must carry a
``User-Agent`` that identifies the app, "No parallel running of multiple scripts." and "Commercial use should use
self-hosted or paid Overpass servers." The data is ODbL: show ``ODBL_ATTRIBUTION`` wherever it is displayed.
"""

import math
from datetime import date, datetime
from typing import cast

import httpx

from tg_discovery.domain.enums import LgbtqLevel
from tg_discovery.domain.geo import BoundingBox
from tg_discovery.domain.models import Evidence, LgbtqAssessment, SourceRef, VenueDraft
from tg_discovery.domain.urls import clean_url
from tg_discovery.providers.base import PriceSpec, ProviderCall, ProviderError, VenueQuery, VenueResult
from tg_discovery.providers.metering import MeteredHttp

INTERPRETER_URL = "https://overpass-api.de/api/interpreter"
ODBL_ATTRIBUTION = (
    "© OpenStreetMap contributors. The data is available under the Open Database License (ODbL): "
    "https://www.openstreetmap.org/copyright"
)
PRICE = PriceSpec(
    unit="request",
    usd_per_unit=0.0,
    verified_on=date(2026, 10, 8),
    source_url="https://wiki.openstreetmap.org/wiki/Overpass_API",
    notes=(
        "Free on the public instance within fair use (under 10,000 queries and 1 GB a day, 100 queries a day for "
        "regular jobs); commercial use should use a self-hosted or paid server."
    ),
)

_NAME = "osm_overpass"
_ERROR_TEXT_CHARS = 200
# Below MeteredHttp's default client timeout, so that the server gives up before the client does.
_SERVER_TIMEOUT_S = 25
_ELEMENT_TYPES = frozenset({"node", "way", "relation"})

# Values of the lgbtq tag (https://wiki.openstreetmap.org/wiki/Key:lgbtq): "only" is a stricter "primary", while "yes"
# and "friendly" are deprecated spellings of "primary" and "welcome" that are still in the data.
_LEVELS = {
    "only": LgbtqLevel.PRIMARY,
    "primary": LgbtqLevel.PRIMARY,
    "yes": LgbtqLevel.PRIMARY,
    "welcome": LgbtqLevel.FRIENDLY,
    "friendly": LgbtqLevel.FRIENDLY,
}

# (tag key, tag values, TravelGay venue type). The first row that matches wins, so the more specific types come first.
_VENUE_TYPES: tuple[tuple[str, frozenset[str], str], ...] = (
    ("tourism", frozenset({"hotel", "hostel", "guest_house", "motel"}), "Hotels"),
    ("leisure", frozenset({"sauna"}), "Sauna"),
    ("leisure", frozenset({"fitness_centre"}), "Gym"),
    ("amenity", frozenset({"spa"}), "Spa"),
    ("leisure", frozenset({"spa"}), "Spa"),
    ("tourism", frozenset({"spa_resort"}), "Spa"),
    ("beauty", frozenset({"spa"}), "Spa"),
    ("amenity", frozenset({"nightclub"}), "Dance Club"),
    ("amenity", frozenset({"bar", "pub"}), "Bar"),
    ("amenity", frozenset({"cafe"}), "Cafe"),
    ("amenity", frozenset({"restaurant"}), "Restaurant"),
    ("natural", frozenset({"beach"}), "Beach"),
    ("leisure", frozenset({"beach_resort"}), "Beach"),
)
# shop=* is any kind of shop, except services that are not retail.
_SERVICE_SHOPS = frozenset({"massage", "beauty", "hairdresser", "tattoo"})
_SOCIAL_NETWORKS = ("facebook", "instagram", "twitter", "youtube", "tiktok")


class OsmOverpass:
    """Venues tagged ``lgbtq=*`` in OpenStreetMap, from one Overpass query per city.

    The tag value gives the level: ``primary`` (deprecated spelling ``yes``) and ``only`` are
    ``LgbtqLevel.PRIMARY``, ``welcome`` (deprecated spelling ``friendly``) is ``LgbtqLevel.FRIENDLY``. The evidence
    quote is the tag itself and its URL is the OSM object.
    """

    name = _NAME
    queryless = True
    google_derived = False
    price = PRICE
    max_concurrency = 1  # "No parallel running of multiple scripts."
    min_interval_s = 2.0  # a courtesy gap: the policy names no per-second limit

    def __init__(self, http: MeteredHttp, user_agent: str) -> None:
        if not user_agent.strip():
            msg = "Overpass requires a User-Agent that identifies the application"
            raise ValueError(msg)
        self._http = http
        self._user_agent = user_agent.strip()

    async def find_venues(self, query: VenueQuery) -> VenueResult:
        """Fetch every ``lgbtq``-tagged object inside ``query.area``; ``query.text`` and ``max_results`` are ignored."""
        response, call = await self._http.request(
            "POST",
            INTERPRETER_URL,
            endpoint="interpreter",
            headers={"User-Agent": self._user_agent, "Accept": "application/json"},
            data={"data": _overpass_ql(query.area)},
            estimated_units=1.0,
        )
        payload = _payload(response, call)
        # A timeout or memory limit comes back as a 200 with a "remark"; reading it as "no venues" would skew coverage.
        remark = _text(payload.get("remark"))
        if remark and "runtime error" in remark.lower():
            raise _unusable(call, remark)
        elements = (_dict(element) for element in _list(payload.get("elements")))
        venues = [draft for element in elements if (draft := _draft(element, call.at)) is not None]
        return VenueResult(provider=self.name, venues=venues, call=call, requested=None)


def _overpass_ql(area: BoundingBox) -> str:
    values = "|".join(sorted(_LEVELS))
    bbox = f"{area.south:.6f},{area.west:.6f},{area.north:.6f},{area.east:.6f}"  # south, west, north, east
    return f'[out:json][timeout:{_SERVER_TIMEOUT_S}];\nnwr["lgbtq"~"^({values})$"]({bbox});\nout center tags;'


def _unusable(call: ProviderCall, reason: str) -> ProviderError:
    """A 200 response that cannot be used is a failed call like any other, so it is logged as one."""
    failed = call.model_copy(update={"status": "error", "error": reason[:_ERROR_TEXT_CHARS]})
    return ProviderError(f"{call.provider} {call.endpoint}: {failed.error}", failed)


def _payload(response: httpx.Response, call: ProviderCall) -> dict[str, object]:
    try:
        decoded: object = response.json()
    except ValueError:
        raise _unusable(call, "the response is not JSON") from None
    if not isinstance(decoded, dict):
        raise _unusable(call, "the response is not a JSON object")
    return cast(dict[str, object], decoded)


def _dict(value: object) -> dict[str, object]:
    return cast(dict[str, object], value) if isinstance(value, dict) else {}


def _list(value: object) -> list[object]:
    return cast(list[object], value) if isinstance(value, list) else []


def _text(value: object) -> str | None:
    return (value.strip() or None) if isinstance(value, str) else None


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        return None
    try:
        number = float(value)
    except ValueError:
        return None
    return number if math.isfinite(number) else None


def _tags(element: dict[str, object]) -> dict[str, str]:
    return {key: text for key, value in _dict(element.get("tags")).items() if (text := _text(value))}


def _draft(element: dict[str, object], retrieved_at: datetime) -> VenueDraft | None:
    """Turn one element into a draft; elements without a name or a usable id are dropped."""
    kind, ident = _text(element.get("type")), element.get("id")
    tags = _tags(element)
    name = tags.get("name")
    if not name or kind not in _ELEMENT_TYPES or not isinstance(ident, int) or isinstance(ident, bool):
        return None
    osm_id = f"{kind}/{ident}"
    osm_url = f"https://www.openstreetmap.org/{osm_id}"
    level_tag = tags.get("lgbtq", "").lower()
    level = _LEVELS.get(level_tag)
    assessment = (
        LgbtqAssessment(level=level, evidence=[Evidence(quote=f"lgbtq={level_tag}", url=osm_url)]) if level else None
    )
    lat, lng = _position(element) or (None, None)
    return VenueDraft(
        name=name,
        venue_type=_venue_type(tags),
        address=_address(tags),
        city=_city(tags),
        country=_country(tags),
        lat=lat,
        lng=lng,
        website=_website(tags),
        phone=_phone(tags),
        socials=_socials(tags),
        opening_hours=tags.get("opening_hours"),
        lgbtq=assessment,
        external_ids={"osm": osm_id},
        sources=[SourceRef(url=osm_url, provider=_NAME, retrieved_at=retrieved_at)],
    )


def _position(element: dict[str, object]) -> tuple[float, float] | None:
    # Nodes carry lat and lon themselves; ways and relations carry the centre of their bounding box.
    holder = _dict(element.get("center")) or element
    lat, lng = _number(holder.get("lat")), _number(holder.get("lon"))
    if lat is None or lng is None or abs(lat) > 90 or abs(lng) > 180:
        return None
    return lat, lng


def _venue_type(tags: dict[str, str]) -> str | None:
    for key, values, venue_type in _VENUE_TYPES:
        if _values(tags, key) & values:
            return venue_type
    return "Shop" if _values(tags, "shop") - _SERVICE_SHOPS else None


def _values(tags: dict[str, str], key: str) -> frozenset[str]:
    """The values of a tag; OSM separates several values with a semicolon."""
    return frozenset(part.strip() for part in tags.get(key, "").split(";") if part.strip())


def _city(tags: dict[str, str]) -> str | None:
    return tags.get("addr:city") or tags.get("addr:town") or tags.get("addr:village")


def _address(tags: dict[str, str]) -> str | None:
    """The street address; a house number alone is not one (the street often comes from a relation)."""
    street = tags.get("addr:street") or tags.get("addr:place")
    if not street:
        return tags.get("addr:full")
    line = " ".join(part for part in (street, tags.get("addr:housenumber")) if part)
    locality = " ".join(part for part in (tags.get("addr:postcode"), _city(tags)) if part)
    return f"{line}, {locality}" if locality else line


def _country(tags: dict[str, str]) -> str | None:
    code = tags.get("addr:country", "")
    return code.upper() if len(code) == 2 and code.isascii() and code.isalpha() else None


def _website(tags: dict[str, str]) -> str | None:
    for raw in (tags.get("website"), tags.get("contact:website")):
        for candidate in (raw or "").split(";"):
            if url := clean_url(candidate):
                return url
    return None


def _phone(tags: dict[str, str]) -> str | None:
    raw = tags.get("phone") or tags.get("contact:phone") or ""
    return raw.split(";")[0].strip() or None


def _socials(tags: dict[str, str]) -> dict[str, str]:
    socials: dict[str, str] = {}
    for network in _SOCIAL_NETWORKS:
        value = tags.get(f"contact:{network}", "")
        # A bare account name is no link, and clean_url would take "berlin.bar" for a domain name.
        if "/" in value and (url := clean_url(value)):
            socials[network] = url
    return socials
