"""SerpApi Google Maps: places on a map around a point, one search (one page of up to 20) per call.

``GET /search?engine=google_maps&type=search`` takes the search text and ``ll=@latitude,longitude,<height>m``, a map
centred on the box with the box's height in metres, and returns up to 20 places in ``local_results``. ``start`` pages
through them in steps of 20 with about 100 as the practical ceiling; this adapter does not page: one call is one page.

Everything here is Google-derived, so the runner scrubs the drafts: the phone, opening hours, description and price
read here are dropped unless ``KEEP_GOOGLE_DETAILS`` is on. Ratings, review counts, photos and thumbnails
are never read. A place keeps its ``place_id`` and ``data_id``, and its source is the Google Maps address built from the
``place_id``, which is the only page that is certain to exist. Field names follow serpapi.com/maps-local-results and
/maps-place-results (read on 2026-10-13): ``operating_hours`` is an object by day in ``local_results``, while a single
``place_results`` gives ``hours`` as a list of one-day objects (``hours`` in ``local_results`` is only the current
state, "Open, closes 2 AM"). Google gives no LGBTQ+ signal beyond the place category (a "Gay bar" is one), so the
level of any other place is left to the reviewer. Terms and price: see ``providers.serpapi``.
"""

import re
from datetime import datetime
from urllib.parse import quote

from tg_discovery.domain.enums import LgbtqLevel, OperatingStatus
from tg_discovery.domain.models import LgbtqAssessment, SourceRef, VenueDraft
from tg_discovery.domain.urls import clean_url, website_key
from tg_discovery.domain.venue_types import venue_type_from_categories
from tg_discovery.providers import serpapi
from tg_discovery.providers.base import VenueQuery, VenueResult
from tg_discovery.providers.metering import MeteredHttp
from tg_discovery.providers.serpapi import as_dict, as_list, as_number, as_text, as_texts

PRICE = serpapi.PRICE
PAGE_SIZE = 20
METRES_PER_DEGREE = 111_320
MAX_MAP_HEIGHT_M = 15_028_132  # the largest height the reference accepts

_NAME = "serpapi_maps"
_LGBTQ = re.compile(r"\b(?:gay|lesbian|lgbt\w*|queer)\b", re.IGNORECASE)


class SerpApiMaps:
    """Google Maps through SerpApi as a venue provider."""

    name = _NAME
    queryless = False
    google_derived = True
    price = PRICE
    max_concurrency = serpapi.MAX_CONCURRENCY
    min_interval_s = serpapi.MIN_INTERVAL_S
    timeout_s = serpapi.TIMEOUT_S

    def __init__(self, http: MeteredHttp, api_key: str) -> None:
        self._http = http
        self._api_key = api_key

    async def find_venues(self, query: VenueQuery) -> VenueResult:
        """Search ``query.text`` on a map that is centred on ``query.area`` and as tall as it."""
        count = min(query.max_results, PAGE_SIZE)
        params: dict[str, str | int | float] = {
            "engine": "google_maps",
            "type": "search",
            "ll": _map_view(query),
            "api_key": self._api_key,
        }
        if text := query.text.strip():
            params["q"] = text
        params |= serpapi.locale(query.language, query.country)
        response, call = await self._http.request(
            "GET", serpapi.SEARCH_URL, endpoint="google_maps", params=params, estimated_units=1.0
        )
        payload = serpapi.read_json(response, call, secret=self._api_key)
        # A search that Google resolves to one place answers with ``place_results`` instead of a list.
        places = [as_dict(place) for place in as_list(payload.get("local_results"))] or [
            as_dict(payload.get("place_results"))
        ]
        venues = [draft for place in places if (draft := _draft(place, call.at)) is not None][:count]
        return VenueResult(provider=self.name, venues=venues, call=call, requested=count)


def _map_view(query: VenueQuery) -> str:
    """``@latitude,longitude,<height>m``: the centre of the box and the height of a map that shows all of it."""
    lat, lng = query.area.center
    height = round((query.area.north - query.area.south) * METRES_PER_DEGREE)
    return f"@{lat:.6f},{lng:.6f},{min(max(height, 1), MAX_MAP_HEIGHT_M)}m"


def _draft(place: dict[str, object], retrieved_at: datetime) -> VenueDraft | None:
    """Turn one place into a draft; places without a title are dropped."""
    name = as_text(place.get("title"))
    if not name:
        return None
    lat, lng = _position(place.get("gps_coordinates")) or (None, None)
    categories = [*as_texts(place.get("types")), *as_texts([place.get("type")])]
    place_id, data_id = as_text(place.get("place_id")), as_text(place.get("data_id"))
    ids = {key: value for key, value in (("google_place_id", place_id), ("google_data_id", data_id)) if value}
    return VenueDraft(
        name=name,
        venue_type=venue_type_from_categories(categories),
        address=as_text(place.get("address")),
        lat=lat,
        lng=lng,
        website=_website(place.get("website")),
        phone=as_text(place.get("phone")),
        opening_hours=_opening_hours(place),
        description=as_text(place.get("description")),
        price_text=as_text(place.get("price")),
        lgbtq=LgbtqAssessment(level=LgbtqLevel.PRIMARY) if _LGBTQ.search(" ".join(categories)) else None,
        operating_status=_operating_status(as_text(place.get("open_state"))),
        external_ids=ids,
        sources=(
            [SourceRef(url=_maps_url(name, place_id), title=name, provider=_NAME, retrieved_at=retrieved_at)]
            if place_id
            else []
        ),
    )


def _position(value: object) -> tuple[float, float] | None:
    fields = as_dict(value)
    lat, lng = as_number(fields.get("latitude")), as_number(fields.get("longitude"))
    if lat is None or lng is None or abs(lat) > 90 or abs(lng) > 180:
        return None
    return lat, lng


def _opening_hours(place: dict[str, object]) -> str | None:
    """The week's hours as one line, ``Monday 6 PM-2 AM; Tuesday Closed``, from either shape Google sends."""
    days = dict(as_dict(place.get("operating_hours")))
    if not days:
        for day in as_list(place.get("hours")):
            days |= as_dict(day)
    week = [f"{day.capitalize()} {hours}" for day, value in days.items() if (hours := as_text(value))]
    return "; ".join(week) or None


def _website(value: object) -> str | None:
    """The place's own site; a Facebook page or a listing on an aggregator is not its website."""
    url = clean_url(as_text(value) or "")
    return url if url and website_key(url) else None


def _operating_status(open_state: str | None) -> OperatingStatus:
    """Google writes closures into ``open_state`` ("Permanently closed"); anything else says nothing about 'open'."""
    text = (open_state or "").casefold()
    if "permanently closed" in text:
        return OperatingStatus.PERMANENTLY_CLOSED
    if "temporarily closed" in text:
        return OperatingStatus.TEMPORARILY_CLOSED
    return OperatingStatus.UNKNOWN


def _maps_url(name: str, place_id: str) -> str:
    """The Google Maps URL for a place id (the "Maps URLs" format); it holds nothing but the id and the name."""
    return f"https://www.google.com/maps/search/?api=1&query={quote(name)}&query_place_id={quote(place_id)}"
