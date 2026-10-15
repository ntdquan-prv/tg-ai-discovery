"""Contracts between the bake-off harness (and later the pipelines) and the provider adapters.

An adapter makes one vendor call per ``search`` or ``find_venues`` and reports what the call cost. It never retries on
its own, never writes anything to disk and never keeps a raw payload: shared HTTP behaviour lives in ``metering``.
"""

from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from tg_discovery.domain.enums import Flow
from tg_discovery.domain.geo import BoundingBox
from tg_discovery.domain.models import VenueDraft
from tg_discovery.domain.page_images import PageImage


class BudgetExceededError(RuntimeError):
    """Raised before a call that could push the run over its budget; nothing was sent."""


@dataclass(frozen=True, slots=True)
class Reservation:
    """Money set aside for one in-flight call."""

    provider: str
    amount_usd: float


class BudgetGuard(Protocol):
    """What the HTTP layer needs from a budget: reserve before a call, settle with the real cost afterwards."""

    def reserve(self, provider: str, amount_usd: float) -> Reservation: ...

    def settle(self, reservation: Reservation, actual_usd: float) -> None: ...


@dataclass(frozen=True, slots=True)
class PriceSpec:
    """List price of one provider endpoint, with the date it was last checked against the vendor's own page."""

    unit: str  # what is billed: "request", "credit", "page"...
    usd_per_unit: float
    verified_on: date | None  # None: taken from the guide or a secondary source, not read on the vendor's own page
    source_url: str
    notes: str = ""

    def cost(self, units: float = 1.0) -> float:
        return units * self.usd_per_unit


CallStatus = Literal["ok", "error", "timeout"]


class ProviderCall(BaseModel):
    """What one provider call cost and how it went; the harness logs one of these per call."""

    model_config = ConfigDict(frozen=True)

    provider: str
    endpoint: str
    units: float
    cost_usd: float
    latency_ms: int
    status: CallStatus
    http_status: int | None = None
    attempts: int = 1
    error: str | None = None
    at: datetime


class ProviderError(Exception):
    """A provider call failed. ``call`` records the attempt so its cost and latency are still logged."""

    def __init__(self, message: str, call: ProviderCall) -> None:
        super().__init__(message)
        self.call = call


class SearchQuery(BaseModel):
    """One web or AI search for a flow in one city."""

    model_config = ConfigDict(frozen=True)

    text: str
    flow: Flow
    country: str  # ISO 3166-1 alpha-2, upper case
    city: str
    language: str = "en"
    max_results: int = Field(default=10, ge=1, le=100)
    # Only results published or updated within this many days (events). Adapters round up to the nearest window the
    # vendor supports, so the vendor may return slightly older pages than asked for.
    freshness_days: int | None = Field(default=None, ge=1)


class SearchHit(BaseModel):
    """One result as the provider returned it, using only the text the provider itself supplied."""

    model_config = ConfigDict(frozen=True)

    url: str
    title: str | None = None
    content: str = ""  # snippet, highlights, summary or page text, whatever the provider returns
    published_at: datetime | None = None  # the provider's page date (published, page age or last modified)
    image: str | None = None  # the provider's main picture of the page (its social preview), when it has one
    images: list[PageImage] = Field(default_factory=list[PageImage])  # more pictures from the page, when asked for


class SearchResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    provider: str
    hits: list[SearchHit]
    call: ProviderCall
    requested: int | None  # the result cap actually sent to the vendor; ``None`` when the vendor has no cap


class SearchProvider(Protocol):
    """A web or AI search API whose results still need extraction into venue or event drafts."""

    @property
    def name(self) -> str: ...

    @property
    def flows(self) -> frozenset[Flow]: ...

    @property
    def google_derived(self) -> bool:
        """Whether results come from Google; such drafts are scrubbed before they are stored."""
        ...

    @property
    def price(self) -> PriceSpec: ...

    async def search(self, query: SearchQuery) -> SearchResult:
        """Run one search. Raises ``ProviderError`` on failure and ``BudgetExceededError`` when nothing may be spent."""
        ...


class VenueQuery(BaseModel):
    """A structured place lookup inside a bounding box."""

    model_config = ConfigDict(frozen=True)

    text: str
    country: str
    city: str
    area: BoundingBox
    language: str = "en"
    max_results: int = Field(default=20, ge=1, le=200)


class VenueResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    provider: str
    venues: list[VenueDraft]
    call: ProviderCall
    requested: int | None


class VenueProvider(Protocol):
    """A map or open-data API that returns structured venues directly, with no extraction step."""

    @property
    def name(self) -> str: ...

    @property
    def queryless(self) -> bool:
        """Whether the provider ignores ``VenueQuery.text``; the harness then calls it once per city."""
        ...

    @property
    def google_derived(self) -> bool: ...

    @property
    def price(self) -> PriceSpec: ...

    async def find_venues(self, query: VenueQuery) -> VenueResult:
        """Look places up. Raises ``ProviderError`` on failure and ``BudgetExceededError`` when nothing may be spent."""
        ...
