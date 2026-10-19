"""What the SerpApi adapters share: the endpoint, the price, the checks on a reply and a few JSON readers.

SerpApi runs the Google search itself and hands it back as JSON, so everything it sends is Google-derived data: the
adapters set ``google_derived`` and the runner then keeps only names, addresses, coordinates, URLs and ids (Google
allows storing only its place ids). Both engines are ``GET https://serpapi.com/search`` with the key in the ``api_key``
query parameter.

Billing (https://serpapi.com/pricing, read 2026-10-08): plans, not pay-as-you-go. "Only successful searches are counted
toward your monthly searches. Cached, errored, and failed searches are not." A free plan gives 250 searches a month, at
most 50 an hour. Identical searches within an hour are served from SerpApi's cache and are free.

Terms (https://serpapi.com/legal, last updated 2026-08-27, read 2026-10-08, not reviewed by counsel): no clause on
storing results or on benchmarking; clause 2 forbids reselling the Service; clause 13 says SerpApi is liable for the
lawful collection of public search data "but not for how that data is ultimately used". Google v. SerpApi is pending,
hence ``requires_acceptance``.
"""

import math
import re
from datetime import date
from typing import cast

import httpx

from tg_discovery.providers.base import PriceSpec, ProviderCall, ProviderError

SEARCH_URL = "https://serpapi.com/search"
PRICE = PriceSpec(
    unit="search",
    usd_per_unit=0.010,
    verified_on=date(2026, 10, 8),
    source_url="https://serpapi.com/pricing",
    notes=(
        "Billed by monthly plan: Free 250 searches (50 an hour), Starter 25 USD for 1,000 (0.025 each), Developer "
        "75 USD for 5,000 (0.015), Production 150 USD for 15,000 (0.010, includes the U.S. Legal Shield of up to "
        "2 million USD). The Production price is used here; the free plan costs nothing. Cached and failed searches "
        "are not counted."
    ),
)
# Courtesy spacing only: the free plan's limit of 50 searches an hour is not enforced here.
MAX_CONCURRENCY = 2
MIN_INTERVAL_S = 0.5
# Measured 2026-10-08: about 14 s per search at the median, 3 of 12 over 30 s.
TIMEOUT_S = 90.0
ERROR_TEXT_CHARS = 200

# SerpApi answers 200 with an ``error`` for a search that Google answered with nothing: that is an empty result.
_NO_RESULTS = "returned any results"
_LANGUAGE = re.compile(r"^[a-z]{2,3}(?:-[a-z0-9]{2,4})?$")


def locale(language: str, country: str) -> dict[str, str]:
    """``hl`` and ``gl`` for Google; a code that does not look like Google's is left out, not sent to fail."""
    params: dict[str, str] = {}
    if _LANGUAGE.match(code := language.lower()):
        params["hl"] = code
    if len(code := country.lower()) == 2 and code.isalpha():
        params["gl"] = code
    return params


def unusable(call: ProviderCall, reason: str) -> ProviderError:
    """A 200 response that cannot be used is a failed call like any other, so it is logged as one."""
    failed = call.model_copy(update={"status": "error", "error": reason[:ERROR_TEXT_CHARS]})
    return ProviderError(f"{call.provider} {call.endpoint}: {failed.error}", failed)


def read_json(response: httpx.Response, call: ProviderCall, *, secret: str | None = None) -> dict[str, object]:
    """The reply as a JSON object; an empty search is a normal answer and any other ``error`` fails the call."""
    try:
        decoded: object = response.json()
    except ValueError:
        raise unusable(call, "the response is not JSON") from None
    if not isinstance(decoded, dict):
        raise unusable(call, "the response is not a JSON object")
    payload = cast(dict[str, object], decoded)
    error = as_text(payload.get("error"))
    if error and _NO_RESULTS not in error.casefold():
        raise unusable(call, f"SerpApi said: {error.replace(secret, '***') if secret else error}")
    return payload


def as_dict(value: object) -> dict[str, object]:
    return cast(dict[str, object], value) if isinstance(value, dict) else {}


def as_list(value: object) -> list[object]:
    return cast(list[object], value) if isinstance(value, list) else []


def as_text(value: object) -> str | None:
    return (value.strip() or None) if isinstance(value, str) else None


def as_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value) if math.isfinite(value) else None


def as_texts(value: object) -> list[str]:
    """The non-empty strings of a list; anything else gives an empty list."""
    return [text for item in as_list(value) if (text := as_text(item))]
