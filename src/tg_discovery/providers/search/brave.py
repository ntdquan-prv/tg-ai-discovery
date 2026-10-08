"""Brave Search adapters: Web Search (``brave_web``) and LLM Context (``brave_llm_context``).

- Endpoints: GET ``/res/v1/web/search`` and ``/res/v1/llm/context`` on api.search.brave.com, with the
  ``X-Subscription-Token`` header.
- Freshness: ``freshness_days`` rounds up to ``pd`` (1 day), ``pw`` (7), ``pm`` (31) or ``py`` (365); beyond a year, no
  filter is sent.
- Cost: 5 USD per 1,000 requests for both endpoints (Search plan), one request per call; only successful requests are
  billed.
- Rate limit: 50 requests per second on the Search plan.

Storage: Brave's terms (3(b)(i)) forbid storing or caching Search Results beyond transient use; keep derived facts only.
"""

from datetime import UTC, date, datetime
from typing import cast

import httpx

from tg_discovery.domain.enums import Flow
from tg_discovery.domain.urls import clean_url
from tg_discovery.providers.base import PriceSpec, ProviderCall, ProviderError, SearchHit, SearchQuery, SearchResult
from tg_discovery.providers.metering import MeteredHttp

WEB_URL = "https://api.search.brave.com/res/v1/web/search"
LLM_CONTEXT_URL = "https://api.search.brave.com/res/v1/llm/context"
WEB_MAX_RESULTS = 20
LLM_CONTEXT_MAX_RESULTS = 50
MAX_CONTENT_CHARS = 8_000

# LLM Context token budgets: one URL can carry about MAX_CONTENT_CHARS (roughly four characters per token) and the total
# is Brave's maximum, so Brave's own extraction is not what limits a hit.
_CONTEXT_TOKENS = 32_768
_CONTEXT_TOKENS_PER_URL = 2_048

_PRICING_URL = "https://api-dashboard.search.brave.com/documentation/pricing"
WEB_PRICE = PriceSpec(
    unit="request",
    usd_per_unit=0.005,
    verified_on=date(2026, 10, 8),
    source_url=_PRICING_URL,
    notes="Search plan: 5 USD per 1,000 requests, 5 USD of free credit a month, 50 requests per second. Only "
    "successful requests are billed.",
)
LLM_CONTEXT_PRICE = PriceSpec(
    unit="request",
    usd_per_unit=0.005,
    verified_on=date(2026, 10, 8),
    source_url=_PRICING_URL,
    notes="Same Search plan and price as Web Search (the plan lists LLM Context among its endpoints); one request "
    "whatever the token budget.",
)

_FRESHNESS_WINDOWS = ((1, "pd"), (7, "pw"), (31, "pm"), (365, "py"))  # (days covered, Brave code)

# Closed lists in Brave's API reference (read 2026-10-08), the same for both endpoints. The reference types both
# parameters as enums and documents 422 for invalid ones, so a value outside the lists is never sent.
_COUNTRIES = frozenset(
    """
    AR AU AT BE BR CA CL DK FI FR DE GR HK IN ID IT JP KR MY MX NL NZ NO CN PL PT PH RU SA ZA ES SE CH TW TR GB US
    """.split()  # noqa: SIM905 - a space-separated list is easier to compare with Brave's documentation
)
_LANGUAGES = frozenset(
    """
    ar eu bn bg ca zh-hans zh-hant hr cs da nl en en-gb et fi fr gl de el gu he hi hu is it ja jp kn ko lv lt ms ml mr
    nb pl pt-br pt-pt pa ro ru sr sk sl es sv ta te th tr uk vi
    """.split()  # noqa: SIM905 - see above
)


def _freshness(days: int | None) -> str | None:
    """Return the smallest Brave window covering ``days``: 1 gives ``pd``, 2-7 ``pw``, 8-31 ``pm``, 32-365 ``py``.

    More than a year has no window, so no filter is sent.
    """
    if days is None:
        return None
    return next((code for covered, code in _FRESHNESS_WINDOWS if days <= covered), None)


def _country(code: str) -> str:
    """Return the country when Brave supports it; any other country is searched worldwide (``ALL``)."""
    country = code.strip().upper()
    return country if country in _COUNTRIES else "ALL"


def _search_lang(language: str, country: str) -> str | None:
    """Map an ISO 639-1 code onto Brave's language list; ``None`` (no preference sent) for languages it lacks."""
    code = language.strip().lower().replace("_", "-")
    if code == "pt":
        code = "pt-br" if country.upper() == "BR" else "pt-pt"
    elif code == "zh":
        code = "zh-hant" if country.upper() in {"TW", "HK"} else "zh-hans"
    elif code == "no":
        code = "nb"
    return code if code in _LANGUAGES else None


def _common_params(query: SearchQuery) -> dict[str, str]:
    """The ``country``, ``search_lang`` and ``freshness`` parameters, which both endpoints take."""
    params = {"country": _country(query.country)}
    if (language := _search_lang(query.language, query.country)) is not None:
        params["search_lang"] = language
    if (freshness := _freshness(query.freshness_days)) is not None:
        params["freshness"] = freshness
    return params


def _headers(api_key: str) -> dict[str, str]:
    return {"X-Subscription-Token": api_key, "Accept": "application/json"}


def _payload(response: httpx.Response, call: ProviderCall) -> dict[str, object]:
    """Decode the JSON object in the body; anything else is a failed call, billed, so the record keeps its cost."""
    try:
        body: object = response.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        msg = "the response body is not a JSON object"
        raise ProviderError(
            f"{call.provider} {call.endpoint}: {msg}", call.model_copy(update={"status": "error", "error": msg})
        )
    return cast(dict[str, object], body)


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


def _content(snippets: list[str]) -> str:
    """Join the distinct, non-empty snippets in order and keep at most ``MAX_CONTENT_CHARS`` characters."""
    return "\n\n".join(dict.fromkeys(snippet for snippet in snippets if snippet))[:MAX_CONTENT_CHARS]


def _hit(url: object, title: object, content: str, published_at: datetime | None) -> SearchHit | None:
    """Build a hit, or ``None`` when the result has no URL."""
    raw_url = _text(url)
    if not raw_url:
        return None
    return SearchHit(
        url=clean_url(raw_url) or raw_url, title=_text(title) or None, content=content, published_at=published_at
    )


def _web_hit(raw: object) -> SearchHit | None:
    item = _mapping(raw)
    snippets = [_text(item.get("description")), *(_text(snippet) for snippet in _items(item.get("extra_snippets")))]
    return _hit(item.get("url"), item.get("title"), _content(snippets), _parse_time(item.get("page_age")))


def _web_hits(payload: dict[str, object]) -> list[SearchHit]:
    hits = (_web_hit(raw) for raw in _items(_mapping(payload.get("web")).get("results")))
    return [hit for hit in hits if hit is not None]


def _page_date(age: object) -> datetime | None:
    """Pick a date from ``sources[url].age``: full date, ``YYYY-MM-DD``, relative age, ISO 8601 timestamp (if known).

    The last position keeps the time of day, so the entries are tried from the end.
    """
    for entry in reversed(_items(age)):
        if (parsed := _parse_time(entry)) is not None:
            return parsed
    return None


def _context_hit(raw: object, sources: dict[str, object]) -> SearchHit | None:
    item = _mapping(raw)
    url = _text(item.get("url"))
    source = _mapping(sources.get(url))
    snippets = [_text(snippet) for snippet in _items(item.get("snippets"))]
    return _hit(url, item.get("title") or source.get("title"), _content(snippets), _page_date(source.get("age")))


def _context_hits(payload: dict[str, object]) -> list[SearchHit]:
    sources = _mapping(payload.get("sources"))
    hits = (_context_hit(raw, sources) for raw in _items(_mapping(payload.get("grounding")).get("generic")))
    return [hit for hit in hits if hit is not None]


class BraveWebSearch:
    """Brave Web Search: ranked pages with a description and, with ``extra_snippets``, up to five more excerpts."""

    name = "brave_web"
    flows = frozenset({Flow.VENUE, Flow.EVENT})
    google_derived = False
    price = WEB_PRICE
    max_concurrency = 4
    min_interval_s = 0.05  # the plan allows 50 requests per second; 20 leaves headroom for timing jitter

    def __init__(self, api_key: str, http: MeteredHttp) -> None:
        self._api_key = api_key
        self._http = http

    async def search(self, query: SearchQuery) -> SearchResult:
        """Run one Web Search request; ``requested`` is ``count``, which Brave caps at 20."""
        requested = min(query.max_results, WEB_MAX_RESULTS)
        params: dict[str, str | int | bool] = {
            "q": query.text,
            "count": requested,
            "extra_snippets": True,
            "text_decorations": False,  # plain text instead of highlight markup around the matched terms
            **_common_params(query),
        }
        response, call = await self._http.request(
            "GET", WEB_URL, endpoint="web_search", headers=_headers(self._api_key), params=params
        )
        return SearchResult(
            provider=self.name, hits=_web_hits(_payload(response, call)), call=call, requested=requested
        )


class BraveLlmContext:
    """Brave LLM Context: page chunks that Brave extracted and ranked for the query, with the page dates."""

    name = "brave_llm_context"
    flows = frozenset({Flow.VENUE, Flow.EVENT})
    google_derived = False
    price = LLM_CONTEXT_PRICE
    max_concurrency = 4
    min_interval_s = 0.05  # the plan allows 50 requests per second; 20 leaves headroom for timing jitter

    def __init__(self, api_key: str, http: MeteredHttp) -> None:
        self._api_key = api_key
        self._http = http

    async def search(self, query: SearchQuery) -> SearchResult:
        """Run one LLM Context request; ``requested`` is ``count`` (and the URL cap), which Brave caps at 50."""
        requested = min(query.max_results, LLM_CONTEXT_MAX_RESULTS)
        params: dict[str, str | int | bool] = {
            "q": query.text,
            "count": requested,
            "maximum_number_of_urls": requested,
            "maximum_number_of_tokens": _CONTEXT_TOKENS,
            "maximum_number_of_tokens_per_url": _CONTEXT_TOKENS_PER_URL,
            **_common_params(query),
        }
        response, call = await self._http.request(
            "GET", LLM_CONTEXT_URL, endpoint="llm_context", headers=_headers(self._api_key), params=params
        )
        return SearchResult(
            provider=self.name, hits=_context_hits(_payload(response, call)), call=call, requested=requested
        )
