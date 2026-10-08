"""Tavily Search API: ``POST https://api.tavily.com/search`` with ``Authorization: Bearer <key>``.

Freshness: ``freshness_days`` becomes ``start_date`` (today in UTC minus the days), exact to the day; ``time_range``
would round the window up to a day, week, month or year. Pages without a detectable date are kept.
Cost: 1 credit per basic, fast or ultra-fast search and 2 per advanced one, 0.008 USD each pay-as-you-go; the response
reports the credits it used (``include_usage``).
Rate limit: 100 requests a minute on a development key (1,000 on a production key), so starts are 0.7 s apart.

Storage terms: the ToS (https://www.tavily.com/terms, updated 2026-05-04) has no clause on storing or caching Output;
section 1.6 only defines it. Read through a summarising fetch tool, not by counsel.
"""

from datetime import UTC, date, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Literal

import httpx
from pydantic import BaseModel, ValidationError

from tg_discovery.domain.enums import Flow
from tg_discovery.domain.urls import clean_url
from tg_discovery.providers.base import PriceSpec, ProviderCall, ProviderError, SearchHit, SearchQuery, SearchResult
from tg_discovery.providers.metering import MeteredHttp

SEARCH_URL = "https://api.tavily.com/search"
MAX_RESULTS = 20  # the API rejects a larger ``max_results``
MAX_CONTENT_CHARS = 8_000

SearchDepth = Literal["basic", "advanced", "fast", "ultra-fast"]
Topic = Literal["general", "news"]

PRICE = PriceSpec(
    unit="credit",
    usd_per_unit=0.008,
    verified_on=date(2026, 10, 8),
    source_url="https://docs.tavily.com/documentation/api-credits",
    notes=(
        "Pay-as-you-go list price; monthly plans cost 0.0075 to 0.005 USD per credit and the free plan gives 1,000 "
        "credits a month. basic, fast and ultra-fast searches use 1 credit, advanced 2."
    ),
)

_CREDITS: dict[SearchDepth, int] = {"basic": 1, "advanced": 2, "fast": 1, "ultra-fast": 1}

# ISO 3166-1 alpha-2 code to the lower-case name that ``country`` accepts. The names are the 166 in Tavily's OpenAPI
# enum (read on 2026-10-08); it has no Kosovo, Hong Kong or DR Congo ("congo" is taken as the Republic), so those get
# no country bias.
_COUNTRY_LIST = """
    AF afghanistan, AL albania, DZ algeria, AD andorra, AO angola, AR argentina, AM armenia, AU australia, AT austria,
    AZ azerbaijan, BS bahamas, BH bahrain, BD bangladesh, BB barbados, BY belarus, BE belgium, BZ belize, BJ benin,
    BT bhutan, BO bolivia, BA bosnia and herzegovina, BW botswana, BR brazil, BN brunei, BG bulgaria,
    BF burkina faso, BI burundi, KH cambodia, CM cameroon, CA canada, CV cape verde, CF central african republic,
    TD chad, CL chile, CN china, CO colombia, KM comoros, CG congo, CR costa rica, HR croatia, CU cuba, CY cyprus,
    CZ czech republic, DK denmark, DJ djibouti, DO dominican republic, EC ecuador, EG egypt, SV el salvador,
    GQ equatorial guinea, ER eritrea, EE estonia, ET ethiopia, FJ fiji, FI finland, FR france, GA gabon, GM gambia,
    GE georgia, DE germany, GH ghana, GR greece, GT guatemala, GN guinea, HT haiti, HN honduras, HU hungary,
    IS iceland, IN india, ID indonesia, IR iran, IQ iraq, IE ireland, IL israel, IT italy, JM jamaica, JP japan,
    JO jordan, KZ kazakhstan, KE kenya, KW kuwait, KG kyrgyzstan, LV latvia, LB lebanon, LS lesotho, LR liberia,
    LY libya, LI liechtenstein, LT lithuania, LU luxembourg, MG madagascar, MW malawi, MY malaysia, MV maldives,
    ML mali, MT malta, MR mauritania, MU mauritius, MX mexico, MD moldova, MC monaco, MN mongolia, ME montenegro,
    MA morocco, MZ mozambique, MM myanmar, NA namibia, NP nepal, NL netherlands, NZ new zealand, NI nicaragua,
    NE niger, NG nigeria, KP north korea, MK north macedonia, NO norway, OM oman, PK pakistan, PA panama,
    PG papua new guinea, PY paraguay, PE peru, PH philippines, PL poland, PT portugal, QA qatar, RO romania,
    RU russia, RW rwanda, SA saudi arabia, SN senegal, RS serbia, SG singapore, SK slovakia, SI slovenia,
    SO somalia, ZA south africa, KR south korea, SS south sudan, ES spain, LK sri lanka, SD sudan, SE sweden,
    CH switzerland, SY syria, TW taiwan, TJ tajikistan, TZ tanzania, TH thailand, TG togo, TT trinidad and tobago,
    TN tunisia, TR turkey, TM turkmenistan, UG uganda, UA ukraine, AE united arab emirates, GB united kingdom,
    US united states, UY uruguay, UZ uzbekistan, VE venezuela, VN vietnam, YE yemen, ZM zambia, ZW zimbabwe
    """


def _country_names(entries: str) -> dict[str, str]:
    """Read comma-separated ``CODE name`` entries into a code to name mapping."""
    return dict(entry.strip().split(" ", 1) for entry in entries.split(","))


_COUNTRY_NAMES = _country_names(_COUNTRY_LIST)


class _Result(BaseModel):
    """The parts of one result that the adapter reads; every one may be missing."""

    url: str | None = None
    title: str | None = None
    content: str | None = None
    raw_content: str | None = None
    published_date: str | None = None  # RFC 2822 ("Tue, 11 Mar 2025 17:00:00 GMT"), unlike the other vendors


class _Response(BaseModel):
    results: list[_Result] | None = None


def _utc_today() -> date:
    return datetime.now(UTC).date()


def _language_code(tag: str) -> str:
    """Reduce a language tag such as ``pt-BR`` to the ISO 639-1 code that ``language`` accepts."""
    return tag.replace("_", "-").split("-")[0].lower()


def _parse_datetime(value: str | None) -> datetime | None:
    """Read an ISO 8601 date or datetime, or an RFC 2822 date, as UTC; naive values are taken as UTC."""
    if not value or not value.strip():
        return None
    text = value.strip()
    try:
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            parsed = parsedate_to_datetime(text)
        return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
    except (TypeError, ValueError, OverflowError):
        return None


def _credits_used(response: httpx.Response) -> float:
    return float(response.json()["usage"]["credits"])


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
    # Snippet chunks come first: they are the passages ranked for the query, ahead of the start of the page.
    texts = [text.strip() for text in (result.content, result.raw_content) if text and text.strip()]
    return SearchHit(
        url=clean_url(url) or url,
        title=(result.title or "").strip() or None,
        content="\n\n".join(texts)[:MAX_CONTENT_CHARS],
        published_at=_parse_datetime(result.published_date),
    )


class TavilySearch:
    """Tavily web search: one ``/search`` call per query.

    Args:
        api_key: Tavily API key (``tvly-...``).
        http: Metered HTTP layer for this provider, built with ``PRICE``, ``max_concurrency`` and ``min_interval_s``.
        search_depth: ``basic`` (default), ``fast`` or ``ultra-fast`` at 1 credit, or ``advanced`` at 2 credits. basic,
            advanced and fast return up to three 500-character chunks per page; ultra-fast one summary.
        topic: ``general`` (default) or ``news``. ``country`` is only accepted for ``general`` and is left out for news.
        include_raw_content: Also ask for each page's text as markdown and append it to the chunks. Off by default
            because it makes responses large; the content is capped at ``MAX_CONTENT_CHARS`` either way.
    """

    name = "tavily"
    flows = frozenset({Flow.VENUE, Flow.EVENT})
    google_derived = False
    price = PRICE
    max_concurrency = 4
    min_interval_s = 0.7

    def __init__(
        self,
        api_key: str,
        http: MeteredHttp,
        *,
        search_depth: SearchDepth = "basic",
        topic: Topic = "general",
        include_raw_content: bool = False,
    ) -> None:
        self._api_key = api_key
        self._http = http
        self._search_depth = search_depth
        self._credits = _CREDITS[search_depth]
        self._topic = topic
        self._include_raw_content = include_raw_content

    async def search(self, query: SearchQuery) -> SearchResult:
        requested = min(query.max_results, MAX_RESULTS)
        response, call = await self._http.request(
            "POST",
            SEARCH_URL,
            endpoint="search",
            headers={"Authorization": f"Bearer {self._api_key}"},
            json=self._body(query, requested),
            estimated_units=self._credits,
            units_from=_credits_used,
        )
        results = _parse(response, call).results or []
        hits = [hit for hit in map(_hit, results) if hit is not None]
        return SearchResult(provider=self.name, hits=hits, call=call, requested=requested)

    def _body(self, query: SearchQuery, requested: int) -> dict[str, object]:
        body: dict[str, object] = {
            "query": query.text,
            "search_depth": self._search_depth,
            "topic": self._topic,
            "max_results": requested,
            "language": _language_code(query.language),  # boosts the language; ``filter_by_language`` would filter
            "include_published_date": True,
            "include_usage": True,
        }
        if self._include_raw_content:
            body["include_raw_content"] = "markdown"
        country = _COUNTRY_NAMES.get(query.country.upper())
        if country is not None and self._topic == "general":
            body["country"] = country
        if query.freshness_days is not None:
            body["start_date"] = (_utc_today() - timedelta(days=query.freshness_days)).isoformat()
        return body
