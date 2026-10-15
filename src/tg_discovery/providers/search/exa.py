"""Exa search adapter (``exa``): one ``POST /search`` per query, with each result's page text in the same call.

- Endpoint: ``POST https://api.exa.ai/search`` with the ``x-api-key`` header; ``type`` is a constructor option.
- Freshness: ``freshness_days`` becomes ``startPublishedDate`` at 00:00 UTC that many days back, so the first day is
  covered whole.
- Cost: base price of the type (up to 10 results) plus 1 USD per 1,000 further results; the real figure is read from
  ``costDollars.total``. The reservation also assumes 1 USD per 1,000 text pages, as Exa bills each content view.
- Rate limit: 10 requests per second on ``/search`` (5 for the deep types).

Storage: Exa's terms (4.2(a)) bar copying or reproducing information obtained through the Services unless Exa allows it
in writing; keep derived facts only.
"""

from datetime import UTC, date, datetime, timedelta
from typing import Literal, cast

import httpx

from tg_discovery.domain.enums import Flow
from tg_discovery.domain.urls import clean_image_url, clean_url
from tg_discovery.providers.base import (
    PageImage,
    PriceSpec,
    ProviderCall,
    ProviderError,
    SearchHit,
    SearchQuery,
    SearchResult,
)
from tg_discovery.providers.metering import MeteredHttp

URL = "https://api.exa.ai/search"
MAX_RESULTS = 100
MAX_CONTENT_CHARS = 8_000
MAX_IMAGE_LINKS = 12  # pictures asked for per page (events only): ``contents.extras.richImageLinks``

ExaSearchType = Literal["instant", "fast", "auto", "deep-lite", "deep", "deep-reasoning"]

# USD per request by search type; the base price covers the first ten results.
_BASE_USD: dict[ExaSearchType, float] = {
    "instant": 0.004,
    "fast": 0.007,
    "auto": 0.007,
    "deep-lite": 0.012,
    "deep": 0.012,
    "deep-reasoning": 0.015,
}
_INCLUDED_RESULTS = 10
_EXTRA_RESULT_USD = 0.001  # each result above ten
_CONTENT_PAGE_USD = 0.001  # one content view of one page

PRICE = PriceSpec(
    unit="request",
    usd_per_unit=_BASE_USD["auto"],
    verified_on=date(2026, 10, 8),
    source_url="https://exa.ai/docs/admin/pricing",
    notes="Auto search with up to 10 results; per 1,000 requests instant is 4 USD, fast and auto 7, deep-lite and deep "
    "12, deep-reasoning 15. Each result above 10 and each AI summary page cost 1 USD per 1,000. The pricing table "
    "lists no per-page charge for text on /search, but the best-practices page says each view is billed separately, "
    "so the reservation includes it; the real cost is read from costDollars.total.",
)


def worst_case_usd(search_type: ExaSearchType, results: int) -> float:
    """Reserve the base price, every result above ten and one billed text view per page."""
    extra_results = max(results - _INCLUDED_RESULTS, 0)
    return _BASE_USD[search_type] + extra_results * _EXTRA_RESULT_USD + results * _CONTENT_PAGE_USD


def _start_published_date(days: int) -> str:
    """Return midnight UTC ``days`` days back as an ISO 8601 timestamp, so the first day of the window is covered."""
    start = datetime.now(UTC).date() - timedelta(days=days)
    return f"{start:%Y-%m-%d}T00:00:00.000Z"


def _mapping(value: object) -> dict[str, object]:
    return cast(dict[str, object], value) if isinstance(value, dict) else {}


def _items(value: object) -> list[object]:
    return cast(list[object], value) if isinstance(value, list) else []


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _parse_time(value: object) -> datetime | None:
    """Read an ISO 8601 date or datetime as UTC (one without an offset is taken as UTC); ``None`` if it is neither."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _body(response: httpx.Response) -> dict[str, object] | None:
    """Return the body as a JSON object, or ``None`` when it is anything else."""
    try:
        body: object = response.json()
    except ValueError:
        return None
    return cast(dict[str, object], body) if isinstance(body, dict) else None


def _reported_cost(response: httpx.Response) -> float | None:
    """Return the dollars Exa reports for the call (``costDollars.total``), or ``None`` when it omits them."""
    total = _mapping(_mapping(_body(response)).get("costDollars")).get("total")
    return float(total) if isinstance(total, int | float) and not isinstance(total, bool) else None


def _payload(response: httpx.Response, call: ProviderCall) -> dict[str, object]:
    """Return the JSON object in the body; anything else is a failed call, billed, so the record keeps its cost."""
    body = _body(response)
    if body is None:
        msg = "the response body is not a JSON object"
        raise ProviderError(
            f"{call.provider} {call.endpoint}: {msg}", call.model_copy(update={"status": "error", "error": msg})
        )
    return body


def _content(item: dict[str, object]) -> str:
    """Return the richest text Exa gave for a result (page text, else highlights, else summary), capped."""
    highlights = "\n\n".join(
        highlight for highlight in (_text(part) for part in _items(item.get("highlights"))) if highlight
    )
    candidates = (_text(item.get("text")), highlights, _text(item.get("summary")))
    return next((candidate for candidate in candidates if candidate), "")[:MAX_CONTENT_CHARS]


def _page_images(item: dict[str, object]) -> list[PageImage]:
    """The pictures of the page from ``extras``: ``richImageLinks`` (url and alt) or else plain ``imageLinks``."""
    extras = _mapping(item.get("extras"))
    rich = [_mapping(entry) for entry in _items(extras.get("richImageLinks"))]
    candidates = [(_text(entry.get("url")), _text(entry.get("alt")) or None) for entry in rich] or [
        (_text(link), None) for link in _items(extras.get("imageLinks"))
    ]
    images: list[PageImage] = []
    for raw, alt in candidates:
        url = clean_image_url(raw)
        if url and all(image.url != url for image in images) and len(images) < MAX_IMAGE_LINKS:
            images.append(PageImage(url=url, alt=alt))
    return images


def _hit(raw: object) -> SearchHit | None:
    item = _mapping(raw)
    url = _text(item.get("url"))
    if not url:
        return None
    return SearchHit(
        url=clean_url(url) or url,
        title=_text(item.get("title")) or None,
        content=_content(item),
        published_at=_parse_time(item.get("publishedDate")),
        image=clean_image_url(_text(item.get("image"))),
        images=_page_images(item),
    )


def _hits(payload: dict[str, object]) -> list[SearchHit]:
    hits = (_hit(raw) for raw in _items(payload.get("results")))
    return [hit for hit in hits if hit is not None]


class ExaSearch:
    """Exa neural search; ``search_type`` picks the mode (``instant``, ``fast``, ``auto``, ``deep-lite``...)."""

    name = "exa"
    flows = frozenset({Flow.VENUE, Flow.EVENT})
    google_derived = False
    price = PRICE
    max_concurrency = 4
    min_interval_s = 0.1  # 10 requests per second; the deep types allow 5, which four slow calls never reach

    def __init__(self, api_key: str, http: MeteredHttp, *, search_type: ExaSearchType = "auto") -> None:
        if search_type not in _BASE_USD:
            msg = f"unknown Exa search type {search_type!r}"
            raise ValueError(msg)
        self._api_key = api_key
        self._http = http
        self._search_type: ExaSearchType = search_type

    async def search(self, query: SearchQuery) -> SearchResult:
        """Run one search; ``requested`` is ``numResults``, which Exa caps at 100.

        Each hit carries up to ``MAX_CONTENT_CHARS`` characters of page text and Exa's main picture of the page (its
        ``image`` field, returned by default). Event searches also ask for up to ``MAX_IMAGE_LINKS`` pictures of the
        page (``contents.extras.richImageLinks``); the pricing page lists no charge for them, and the real cost is
        read from ``costDollars.total`` either way. Only the URLs are kept.
        """
        requested = min(query.max_results, MAX_RESULTS)
        contents: dict[str, object] = {"text": {"maxCharacters": MAX_CONTENT_CHARS}}
        if query.flow is Flow.EVENT:
            contents["extras"] = {"richImageLinks": MAX_IMAGE_LINKS}
        body: dict[str, object] = {
            "query": query.text,
            "type": self._search_type,
            "numResults": requested,
            "contents": contents,
        }
        if query.country:
            body["userLocation"] = query.country
        if query.freshness_days is not None:
            body["startPublishedDate"] = _start_published_date(query.freshness_days)
        worst_case = worst_case_usd(self._search_type, requested)

        def reported_or_worst_case(response: httpx.Response) -> float:
            # A response without costDollars is billed at the worst case instead of being under-reported.
            reported = _reported_cost(response)
            return worst_case if reported is None else reported

        response, call = await self._http.request(
            "POST",
            URL,
            endpoint="search",
            headers={"x-api-key": self._api_key},
            json=body,
            estimated_cost_usd=worst_case,
            cost_from=reported_or_worst_case,
        )
        return SearchResult(provider=self.name, hits=_hits(_payload(response, call)), call=call, requested=requested)
