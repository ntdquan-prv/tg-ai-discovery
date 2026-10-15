"""Brave Place Search: businesses and points of interest around a point, one request per query.

``GET /res/v1/local/place_search`` takes a centre and a radius (a bias, not a hard cutoff) and returns up to 100 places
with no pagination. Results carry no LGBTQ+ signal, so every draft still goes through the verify step. Ratings,
reviews, photos and the vendor's description are never read, and the place id (valid for about 8 hours, "Do not store
them for later use") is not kept. Storage: the Search API Terms of Use (1 Sep 2026, section 3(b)(i)) forbid anyone to
"store, cache, or create a database of Search Results, in whole or in part, other than transient storage required for
operation", so keep only derived facts unless the plan grants storage rights.
"""

import math
from datetime import date, datetime
from typing import cast

import httpx

from tg_discovery.domain.models import SourceRef, VenueDraft
from tg_discovery.domain.urls import clean_url, website_key
from tg_discovery.domain.venue_types import venue_type_from_categories
from tg_discovery.providers.base import PriceSpec, ProviderCall, ProviderError, VenueQuery, VenueResult
from tg_discovery.providers.metering import MeteredHttp

SEARCH_URL = "https://api.search.brave.com/res/v1/local/place_search"
MAX_COUNT = 100
PRICE = PriceSpec(
    unit="request",
    usd_per_unit=0.005,
    verified_on=date(2026, 10, 8),
    source_url="https://brave.com/search/api/",
    notes=(
        "Search plan, $5 per 1,000 requests, which includes Place Search; each month starts with $5 of free credit, "
        "which is not netted off here. Only successful requests are billed."
    ),
)

_NAME = "brave_place"
_ERROR_TEXT_CHARS = 200

# The values the reference lists for these two parameters (read 2026-10-08). The country list is short (Thailand is not
# on it), so other countries are sent as ALL: leaving the parameter out would mean US.
_COUNTRIES = frozenset(
    """
    AR AU AT BE BR CA CL DK FI FR DE GR HK IN ID IT JP KR MY MX NL NZ NO CN PL PT PH RU SA ZA ES SE CH TW TR GB US ALL
    """.split()  # noqa: SIM905 - one code per entry is easier to maintain than a quoted list
)
_LANGUAGES = frozenset(
    """
    ar eu bn bg ca zh-hans zh-hant hr cs da nl en en-gb et fi fr gl de el gu he hi hu is it ja jp kn ko lv lt ms ml mr
    nb pl pt-br pt-pt pa ro ru sr sk sl es sv ta te th tr uk vi
    """.split()  # noqa: SIM905
)


class BravePlace:
    """Brave Place Search as a venue provider."""

    name = _NAME
    queryless = False
    google_derived = False
    price = PRICE
    max_concurrency = 1
    # One request per second plus a margin (Brave counts in a 1-second sliding window), in case the key is on a plan
    # with that limit. The Search plan documents 50 per second: lower this once the key's X-RateLimit-Limit says so.
    min_interval_s = 1.1

    def __init__(self, http: MeteredHttp, api_key: str) -> None:
        self._http = http
        self._api_key = api_key

    async def find_venues(self, query: VenueQuery) -> VenueResult:
        """Search ``query.text`` around the centre of ``query.area``, within the radius that covers the box."""
        count = min(query.max_results, MAX_COUNT)
        lat, lng = query.area.center
        country, language = query.country.upper(), query.language.lower()
        params: dict[str, str | int | float] = {
            "latitude": round(lat, 6),
            "longitude": round(lng, 6),
            "radius": round(query.area.radius_km * 1000),
            "count": count,
            "country": country if country in _COUNTRIES else "ALL",
            # The default "strict" may hide adult venues (saunas, cruising clubs, sex shops) that TravelGay lists.
            "safesearch": "off",
        }
        if text := query.text.strip():
            params["q"] = text
        if language in _LANGUAGES:
            params["search_lang"] = language
        response, call = await self._http.request(
            "GET",
            SEARCH_URL,
            endpoint="place_search",
            headers={"X-Subscription-Token": self._api_key, "Accept": "application/json"},
            params=params,
            estimated_units=1.0,
        )
        payload = _payload(response, call)
        places = (_dict(place) for place in _list(payload.get("results")))
        venues = [draft for place in places if (draft := _draft(place, call.at)) is not None]
        return VenueResult(provider=self.name, venues=venues, call=call, requested=count)


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
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value) if math.isfinite(value) else None


def _clean(value: object) -> str | None:
    text = _text(value)
    return clean_url(text) if text else None


def _draft(place: dict[str, object], retrieved_at: datetime) -> VenueDraft | None:
    """Turn one result into a draft; results without a title are dropped."""
    name = _text(place.get("title"))
    if not name:
        return None
    postal = _dict(place.get("postal_address"))
    lat, lng = _position(place.get("coordinates")) or (None, None)
    listing = _clean(place.get("url"))
    page = listing or _clean(place.get("provider_url"))  # without a page there is no source to link to
    categories = [category for entry in _list(place.get("categories")) if (category := _text(entry))]
    return VenueDraft(
        name=name,
        venue_type=venue_type_from_categories(categories),
        address=_text(postal.get("displayAddress")) or _text(postal.get("streetAddress")),
        city=_text(postal.get("addressLocality")),
        country=_country(postal.get("country")),
        lat=lat,
        lng=lng,
        # ``url`` is the venue's own site when Brave knows it and a listing page (Yelp, for one) when it does not.
        website=listing if listing and website_key(listing) else None,
        phone=_text(_dict(place.get("contact")).get("telephone")),
        opening_hours=_opening_hours(place.get("opening_hours")),
        sources=[SourceRef(url=page, title=name, provider=_NAME, retrieved_at=retrieved_at)] if page else [],
    )


def _position(value: object) -> tuple[float, float] | None:
    """Brave sends ``[latitude, longitude]``."""
    pair = _list(value)
    if len(pair) != 2:
        return None
    lat, lng = _number(pair[0]), _number(pair[1])
    if lat is None or lng is None or abs(lat) > 90 or abs(lng) > 180:
        return None
    return lat, lng


def _country(value: object) -> str | None:
    code = _text(value) or ""
    return code.upper() if len(code) == 2 and code.isascii() and code.isalpha() else None


def _opening_hours(hours: object) -> str | None:
    """One short line from the weekly hours; ``current_day`` alone says nothing about the other days."""
    spans: dict[str, list[str]] = {}
    for entry in _list(_dict(hours).get("days")):
        # ``days`` is a list of days that each hold a list of slots; a flat list of slots is accepted as well.
        for slot in _list(entry) or [entry]:
            fields = _dict(slot)
            day, opens, closes = _text(fields.get("abbr_name")), _text(fields.get("opens")), _text(fields.get("closes"))
            if day and opens and closes:
                spans.setdefault(day, []).append(f"{opens}-{closes}")
    if not spans:
        return None
    first = next(iter(spans.values()))
    if len(spans) == 7 and all(times == first for times in spans.values()):
        return f"Daily {', '.join(first)}"
    return "; ".join(f"{day} {', '.join(times)}" for day, times in spans.items())
