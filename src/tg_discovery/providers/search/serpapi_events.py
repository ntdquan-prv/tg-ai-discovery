"""SerpApi Google Search, read for its events pack: the events Google shows above the results of an events query.

SerpApi's Google Events API (``engine=google_events``) was retired after Google dropped its separate Events tab
(changelog 2026-08-24 to 08-30; HTTP 400 "Unsupported `google_events` search engine", checked 2026-10-08), and
DataForSEO retired its Google Events endpoint on 2026-09-15. What is left is the ``events_results`` list that
``GET /search?engine=google`` returns when Google shows an events pack: up to ten events, each with a ``title``, a
``type``, a ``date`` ("Oct 10", no year), a ``time`` and an ``address`` (venue, area). There is no link, price or
description, and no date filter, so the period lives in the query text.

One search is one Google results page, so it becomes one hit: the page's own address (``search_metadata.google_url``)
with the events as labelled lines. It goes through the same extractor as every other search result, which reads the
day and the time and fills in the missing year. A query that does not bring up the pack gives organic results only and
so no hits. Thumbnails are never passed on. Terms and price: see ``providers.serpapi``.
"""

from urllib.parse import quote_plus

from tg_discovery.domain.enums import Flow
from tg_discovery.providers import serpapi
from tg_discovery.providers.base import SearchHit, SearchQuery, SearchResult
from tg_discovery.providers.metering import MeteredHttp
from tg_discovery.providers.serpapi import as_dict, as_list, as_text, as_texts

PRICE = serpapi.PRICE
MAX_CONTENT_CHARS = 6_000  # what the extractor reads of one page


class SerpApiEvents:
    """Google's events pack through SerpApi as an event search provider."""

    name = "serpapi_events"
    flows = frozenset({Flow.EVENT})
    google_derived = True
    price = PRICE
    max_concurrency = serpapi.MAX_CONCURRENCY
    min_interval_s = serpapi.MIN_INTERVAL_S
    timeout_s = serpapi.TIMEOUT_S

    def __init__(self, api_key: str, http: MeteredHttp) -> None:
        self._api_key = api_key
        self._http = http

    async def search(self, query: SearchQuery) -> SearchResult:
        """Search ``query.text`` on Google; ``freshness_days`` has no counterpart: the query text carries the period."""
        params: dict[str, str | int | float] = {"engine": "google", "q": query.text, "api_key": self._api_key}
        params |= serpapi.locale(query.language, query.country)
        response, call = await self._http.request(
            "GET", serpapi.SEARCH_URL, endpoint="google_search", params=params, estimated_units=1.0
        )
        payload = serpapi.read_json(response, call, secret=self._api_key)
        return SearchResult(provider=self.name, hits=_hits(payload, query.text), call=call, requested=None)


def _hits(payload: dict[str, object], text: str) -> list[SearchHit]:
    """The whole events pack as one hit on the results page, or no hit when the page showed no events."""
    events = [lines for event in as_list(payload.get("events_results")) if (lines := _lines(as_dict(event)))]
    if not events:
        return []
    page = as_text(as_dict(payload.get("search_metadata")).get("google_url"))
    return [
        SearchHit(
            url=page or f"https://www.google.com/search?q={quote_plus(text)}",
            title=f"Google events for {text}",
            content="\n\n".join(events)[:MAX_CONTENT_CHARS],
        )
    ]


def _lines(event: dict[str, object]) -> str:
    """One event as labelled lines; an event without a title is not an event."""
    title = as_text(event.get("title"))
    if not title:
        return ""
    lines = [f"Event: {title}"]
    if kind := as_text(event.get("type")):
        lines.append(f"Type: {kind}")
    date = event.get("date")
    fields = as_dict(date)  # a string today; the retired events API sent an object
    day = (as_text(fields.get("when")) or as_text(fields.get("start_date"))) if fields else as_text(date)
    if when := ", ".join(part for part in (day, as_text(event.get("time"))) if part):
        lines.append(f"When: {when}")
    if address := as_texts(event.get("address")):
        lines.append(f"Where: {', '.join(address)}")
    return "\n".join(lines)
