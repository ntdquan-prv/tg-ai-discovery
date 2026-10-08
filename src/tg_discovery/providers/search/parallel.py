"""Parallel Search API: ``POST https://api.parallel.ai/v1/search`` with an ``x-api-key`` header.

Freshness: ``freshness_days`` becomes ``advanced_settings.source_policy.after_date`` (today in UTC minus the days),
exact to the day. There is no language parameter: the query text decides, and turbo only handles English and Japanese.
Cost: per request by mode, 0.001 USD for turbo and fast and 0.005 for basic and advanced, with 10 results included and
0.001 USD for each one beyond. The response lists SKU counts, not dollars, so the bill is worked out from the results.
Rate limit: 600 requests a minute per account, so requests are not spaced.

Storage terms (https://www.parallel.ai/customer-terms, no date shown): section 2(b) says Customer Output "shall not be
copied, cached, stored, or made available to other End Customers or other third parties", and section 2(c)(vi) forbids
using Output to "create databases, data brokerage, data selling/reselling businesses, or related products or services".
Have counsel read them before any excerpt is persisted.
"""

from datetime import UTC, date, datetime, timedelta
from functools import partial
from typing import Literal

import httpx
from pydantic import BaseModel, ValidationError

from tg_discovery.domain.enums import Flow
from tg_discovery.domain.urls import clean_url
from tg_discovery.providers.base import PriceSpec, ProviderCall, ProviderError, SearchHit, SearchQuery, SearchResult
from tg_discovery.providers.metering import MeteredHttp

SEARCH_URL = "https://api.parallel.ai/v1/search"
MAX_RESULTS = 20  # the public modes cap ``max_results`` at 20
INCLUDED_RESULTS = 10  # results a request's price covers
MAX_CONTENT_CHARS = 8_000

_MAX_QUERY_CHARS = 200  # the documented limit of one entry of ``search_queries``
_MAX_OBJECTIVE_CHARS = 5_000
_EXTRA_RESULT_USD = 0.001

Mode = Literal["turbo", "fast", "basic", "advanced"]
DEFAULT_MODE: Mode = "fast"  # as cheap as turbo, better quality, and the mode the vendor recommends for agents

_PRICING_URL = "https://docs.parallel.ai/getting-started/pricing"
_FAST_TIER = PriceSpec(
    unit="request",
    usd_per_unit=0.001,
    verified_on=date(2026, 10, 8),
    source_url=_PRICING_URL,
    notes="turbo and fast. Covers 10 results with excerpts; each further result adds 0.001 USD.",
)
_BASIC_TIER = PriceSpec(
    unit="request",
    usd_per_unit=0.005,
    verified_on=date(2026, 10, 8),
    source_url=_PRICING_URL,
    notes="basic and advanced. Covers 10 results with excerpts; each further result adds 0.001 USD.",
)
PRICES: dict[Mode, PriceSpec] = {"turbo": _FAST_TIER, "fast": _FAST_TIER, "basic": _BASIC_TIER, "advanced": _BASIC_TIER}


class _Result(BaseModel):
    """The parts of one result that the adapter reads; every one may be missing."""

    url: str | None = None
    title: str | None = None
    publish_date: str | None = None
    excerpts: list[str] | None = None


class _Response(BaseModel):
    results: list[_Result] | None = None


def _utc_today() -> date:
    return datetime.now(UTC).date()


def _parse_datetime(value: str | None) -> datetime | None:
    """Read an ISO 8601 date (the vendor sends ``YYYY-MM-DD``) or datetime as UTC; naive values are taken as UTC."""
    if not value or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
        return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
    except (ValueError, OverflowError):
        return None


def _parse(response: httpx.Response, call: ProviderCall) -> _Response:
    try:
        return _Response.model_validate_json(response.content)
    except ValidationError:
        # The vendor answered and billed, so the cost stays; only the record says the body was unusable.
        failed = call.model_copy(update={"status": "error", "error": "response body is not the expected JSON"})
        raise ProviderError(f"{call.provider} {call.endpoint}: {failed.error}", failed) from None


def _hit(result: _Result) -> SearchHit | None:
    url = (result.url or "").strip()
    if not url:
        return None
    excerpts = [text.strip() for text in result.excerpts or [] if text.strip()]
    return SearchHit(
        url=clean_url(url) or url,
        title=(result.title or "").strip() or None,
        content="\n\n".join(excerpts)[:MAX_CONTENT_CHARS],
        published_at=_parse_datetime(result.publish_date),
    )


class ParallelSearch:
    """Parallel web search: one ``/v1/search`` call per query, content is the excerpts of each result.

    Args:
        api_key: Parallel API key.
        http: Metered HTTP layer for this provider, built with ``max_concurrency`` and ``min_interval_s``. The adapter
            states its own cost (mode price plus extra results) on every call, so the price that layer was built with
            does not change what is billed; build it with ``PRICES[mode]`` anyway so both agree.
        mode: ``fast`` (default) and ``turbo`` cost 0.001 USD a request; ``basic`` and ``advanced`` 0.005 and return
            richer excerpts. ``turbo`` only covers English and Japanese queries.
    """

    name = "parallel"
    flows = frozenset({Flow.VENUE, Flow.EVENT})
    google_derived = False
    price = PRICES[DEFAULT_MODE]  # an instance carries the price of the mode it was built with
    max_concurrency = 4
    min_interval_s = 0.0

    def __init__(self, api_key: str, http: MeteredHttp, *, mode: Mode = DEFAULT_MODE) -> None:
        self._api_key = api_key
        self._http = http
        self._mode: Mode = mode
        self.price = PRICES[mode]

    async def search(self, query: SearchQuery) -> SearchResult:
        requested = min(query.max_results, MAX_RESULTS)
        worst_case_usd = self._cost_usd(requested)
        response, call = await self._http.request(
            "POST",
            SEARCH_URL,
            endpoint="search",
            headers={"x-api-key": self._api_key},
            json=self._body(query, requested),
            estimated_cost_usd=worst_case_usd,
            cost_from=partial(self._billed_usd, fallback=worst_case_usd),
        )
        results = _parse(response, call).results or []
        hits = [hit for hit in map(_hit, results) if hit is not None]
        return SearchResult(provider=self.name, hits=hits, call=call, requested=requested)

    def _body(self, query: SearchQuery, requested: int) -> dict[str, object]:
        settings: dict[str, object] = {
            "max_results": requested,
            "location": query.country.lower(),
            "excerpt_settings": {"max_chars_per_result": MAX_CONTENT_CHARS},
        }
        if query.freshness_days is not None:
            after = _utc_today() - timedelta(days=query.freshness_days)
            settings["source_policy"] = {"after_date": after.isoformat()}
        return {
            "objective": query.text[:_MAX_OBJECTIVE_CHARS],
            "search_queries": [query.text[:_MAX_QUERY_CHARS]],
            "mode": self._mode,
            "advanced_settings": settings,
        }

    def _cost_usd(self, results: int) -> float:
        """The mode's request price plus 0.001 USD for each result beyond the ones it covers."""
        return self.price.usd_per_unit + _EXTRA_RESULT_USD * max(0, results - INCLUDED_RESULTS)

    def _billed_usd(self, response: httpx.Response, *, fallback: float) -> float:
        """Cost of a call from the number of results it returned; ``fallback`` when the body cannot be counted."""
        try:
            returned = len(response.json()["results"])
        except (ValueError, KeyError, TypeError):
            return fallback
        return self._cost_usd(returned)
