"""Perplexity Search API (not Sonar): ``POST https://api.perplexity.ai/search`` with ``Authorization: Bearer <key>``.

Freshness: ``freshness_days`` becomes ``search_after_date_filter`` (today in UTC minus the days, ``MM/DD/YYYY``), exact
to the day instead of the day, week, month or year buckets of ``search_recency_filter``. How undated pages are treated
is not documented. The language is not sent: ``search_language_filter`` filters hard, and a bias is wanted.
Cost: 0.005 USD a request for ``web`` search and 0.001 for ``fast``, with no token charge; the response reports no
usage, so every successful call is billed at that price (a 429 is not billed).
Rate limit: 50 query units a second for every usage tier, far above what the harness sends, so requests are not spaced.

Storage terms not verified: the Search service terms answered HTTP 403 to the fetch tool
(https://www.perplexity.ai/hub/legal/perplexity-api-terms-of-service-search). A search-engine snippet of them, not the
page, says the Customer does not own the Search API's Output, so have them read before results are stored.
"""

from datetime import UTC, date, datetime, timedelta
from typing import Literal

import httpx
from pydantic import BaseModel, ValidationError

from tg_discovery.domain.enums import Flow
from tg_discovery.domain.urls import clean_url
from tg_discovery.providers.base import PriceSpec, ProviderCall, ProviderError, SearchHit, SearchQuery, SearchResult
from tg_discovery.providers.metering import MeteredHttp

SEARCH_URL = "https://api.perplexity.ai/search"
MAX_RESULTS = 20  # web and fast search; only ``search_type=people`` goes higher
MAX_CONTENT_CHARS = 8_000

_MAX_TOKENS_PER_PAGE = MAX_CONTENT_CHARS // 4  # about four characters a token, so a page can fill the content cap

SearchType = Literal["web", "fast"]
DEFAULT_SEARCH_TYPE: SearchType = "web"

_PRICING_URL = "https://docs.perplexity.ai/docs/getting-started/pricing"
PRICES: dict[SearchType, PriceSpec] = {
    "web": PriceSpec(
        unit="request",
        usd_per_unit=0.005,
        verified_on=date(2026, 10, 8),
        source_url=_PRICING_URL,
        notes="Per successful request, no token charge. A request with several queries counts as one; this sends one.",
    ),
    "fast": PriceSpec(
        unit="request",
        usd_per_unit=0.001,
        verified_on=date(2026, 10, 8),
        source_url=_PRICING_URL,
        notes="Lower latency and cost than web search; otherwise billed like it.",
    ),
}


class _Result(BaseModel):
    """The parts of one result that the adapter reads; every one may be missing."""

    url: str | None = None
    title: str | None = None
    snippet: str | None = None
    date: str | None = None
    last_updated: str | None = None


class _Response(BaseModel):
    results: list[_Result] | None = None


def _utc_today() -> date:
    return datetime.now(UTC).date()


def _parse_datetime(value: str | None) -> datetime | None:
    """Read an ISO 8601 date or datetime as UTC; naive values are taken as UTC."""
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
    return SearchHit(
        url=clean_url(url) or url,
        title=(result.title or "").strip() or None,
        content=(result.snippet or "").strip()[:MAX_CONTENT_CHARS],
        published_at=_parse_datetime(result.date) or _parse_datetime(result.last_updated),
    )


class PerplexitySearch:
    """Perplexity web search: one ``/search`` call per query, content is the page snippet of each result.

    Args:
        api_key: Perplexity API key.
        http: Metered HTTP layer for this provider, built with ``max_concurrency`` and ``min_interval_s``. The adapter
            states its own cost on every call, so the price that layer was built with does not change what is billed;
            build it with ``PRICES[search_type]`` anyway so both agree.
        search_type: ``web`` (default, 0.005 USD a request) or ``fast`` (0.001, lower latency).
    """

    name = "perplexity_search"
    flows = frozenset({Flow.VENUE, Flow.EVENT})
    google_derived = False
    price = PRICES[DEFAULT_SEARCH_TYPE]  # an instance carries the price of the type it was built with
    max_concurrency = 4
    min_interval_s = 0.0

    def __init__(self, api_key: str, http: MeteredHttp, *, search_type: SearchType = DEFAULT_SEARCH_TYPE) -> None:
        self._api_key = api_key
        self._http = http
        self._search_type: SearchType = search_type
        self.price = PRICES[search_type]

    async def search(self, query: SearchQuery) -> SearchResult:
        requested = min(query.max_results, MAX_RESULTS)
        response, call = await self._http.request(
            "POST",
            SEARCH_URL,
            endpoint="search",
            headers={"Authorization": f"Bearer {self._api_key}"},
            json=self._body(query, requested),
            estimated_cost_usd=self.price.usd_per_unit,
            cost_from=self._billed_usd,
        )
        results = _parse(response, call).results or []
        hits = [hit for hit in map(_hit, results) if hit is not None]
        return SearchResult(provider=self.name, hits=hits, call=call, requested=requested)

    def _body(self, query: SearchQuery, requested: int) -> dict[str, object]:
        body: dict[str, object] = {
            "query": query.text,
            "search_type": self._search_type,
            "max_results": requested,
            "max_tokens_per_page": _MAX_TOKENS_PER_PAGE,
            "max_tokens": requested * _MAX_TOKENS_PER_PAGE,  # the total, so that every page gets its per-page budget
            "country": query.country.upper(),
        }
        if query.freshness_days is not None:
            after = _utc_today() - timedelta(days=query.freshness_days)
            body["search_after_date_filter"] = after.strftime("%m/%d/%Y")
        return body

    def _billed_usd(self, _response: httpx.Response) -> float:
        return self.price.usd_per_unit
