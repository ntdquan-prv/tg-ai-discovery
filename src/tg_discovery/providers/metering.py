"""Shared HTTP plumbing for provider adapters.

``MeteredHttp`` wraps an ``httpx.AsyncClient`` for one provider. Every call reserves its worst-case cost in the budget
first, runs under the provider's own timeout (sent with each request, so the shared client's default never cuts it
shorter) plus a concurrency and rate limit, is retried (once by default) on transport errors, 429 and 5xx (and on
timeouts, unless the provider turns that off), and comes back with a ``ProviderCall``
holding the real cost and latency. A failed call raises ``ProviderError`` with the same record, so cost and latency are
logged for failures too.

Billing assumptions: a response is billed at the endpoint price unless the adapter reads the real amount from it;
errors are not billed; a timeout is charged at the estimate because the vendor may still have processed (and billed)
it.
"""

import asyncio
import re
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import cast

import httpx
from tenacity import AsyncRetrying, retry_if_exception, stop_after_attempt, wait_exponential

from tg_discovery.providers.base import BudgetGuard, CallStatus, PriceSpec, ProviderCall, ProviderError

DEFAULT_TIMEOUT_S = 30.0
_ERROR_TEXT_CHARS = 200
_SECRET = re.compile(r"(?i)((?:api[_-]?key|token|secret|password|authorization|bearer)[\"'=:\s]+)([^\s&\"',]+)")

QueryParams = Mapping[str, str | int | float | bool | Sequence[str | int | float | bool] | None]
Send = Callable[[], Awaitable[httpx.Response]]


def _scrub(text: str) -> str:
    """Hide anything that looks like a credential and keep the text short enough for a log line."""
    return _SECRET.sub(r"\1***", text)[:_ERROR_TEXT_CHARS]


class _SendError(Exception):
    def __init__(
        self, message: str, *, retryable: bool, http_status: int | None = None, timed_out: bool = False
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.http_status = http_status
        self.timed_out = timed_out


class MeteredHttp:
    """Budgeted, timed, rate-limited and retried HTTP for one provider."""

    def __init__(
        self,
        provider: str,
        price: PriceSpec,
        client: httpx.AsyncClient,
        guard: BudgetGuard,
        *,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        max_concurrency: int = 4,
        min_interval_s: float = 0.0,
        retries: int = 1,
        retry_wait_s: float = 1.0,
        max_retry_wait_s: float | None = None,
        retry_timeouts: bool = True,
    ) -> None:
        self.provider = provider
        self.price = price
        self._client = client
        self._guard = guard
        self._timeout_s = timeout_s
        self._retry_timeouts = retry_timeouts
        self._slots = asyncio.Semaphore(max_concurrency)
        self._min_interval_s = min_interval_s
        self._interval_lock = asyncio.Lock()
        self._next_start = 0.0
        self._retries = retries
        self._retry_wait_s = retry_wait_s
        self._max_retry_wait_s = max_retry_wait_s if max_retry_wait_s is not None else 4 * retry_wait_s

    async def request(
        self,
        method: str,
        url: str,
        *,
        endpoint: str,
        headers: Mapping[str, str] | None = None,
        params: QueryParams | None = None,
        json: object = None,
        data: Mapping[str, str] | None = None,
        estimated_units: float = 1.0,
        units_from: Callable[[httpx.Response], float] | None = None,
        cost_from: Callable[[httpx.Response], float | None] | None = None,
        estimated_cost_usd: float | None = None,
    ) -> tuple[httpx.Response, ProviderCall]:
        """Send one HTTP request through ``call``."""
        # The shared client's default timeout (30 s) would otherwise cut a provider that is allowed longer, such as the
        # model (minutes) or SerpApi (90 s), and the error would name the wrong limit.
        timeout = httpx.Timeout(self._timeout_s)
        return await self.call(
            endpoint,
            lambda: self._client.request(
                method, url, headers=headers, params=params, json=json, data=data, timeout=timeout
            ),
            estimated_units=estimated_units,
            units_from=units_from,
            cost_from=cost_from,
            estimated_cost_usd=estimated_cost_usd,
        )

    async def call(
        self,
        endpoint: str,
        send: Send,
        *,
        estimated_units: float = 1.0,
        units_from: Callable[[httpx.Response], float] | None = None,
        cost_from: Callable[[httpx.Response], float | None] | None = None,
        estimated_cost_usd: float | None = None,
    ) -> tuple[httpx.Response, ProviderCall]:
        """Run ``send`` within the budget and return the response with its call record.

        ``estimated_units`` is the worst case reserved before sending (``estimated_cost_usd`` states it in dollars for
        endpoints that are not priced per unit, such as token-priced LLM calls). ``units_from`` and ``cost_from`` read
        the real usage or dollar amount from a successful response; without them the estimate is what gets billed.
        """
        estimated_cost = estimated_cost_usd if estimated_cost_usd is not None else self.price.cost(estimated_units)
        # Raises BudgetExceededError before anything is sent.
        reservation = self._guard.reserve(self.provider, estimated_cost)
        started = time.perf_counter()
        retrying = AsyncRetrying(
            stop=stop_after_attempt(1 + self._retries),
            wait=wait_exponential(multiplier=self._retry_wait_s, min=self._retry_wait_s, max=self._max_retry_wait_s),
            retry=retry_if_exception(lambda error: isinstance(error, _SendError) and error.retryable),
            reraise=True,
        )
        try:
            async with self._slots:
                # tenacity does not tie the return type of ``retrying(fn)`` to ``fn``, hence the cast.
                response, latency_ms = cast(tuple[httpx.Response, int], await retrying(self._attempt, send))
        except _SendError as failure:
            billed = estimated_cost if failure.timed_out else 0.0
            self._guard.settle(reservation, billed)
            record = self._record(
                endpoint,
                status="timeout" if failure.timed_out else "error",
                units=estimated_units if billed else 0.0,
                cost_usd=billed,
                latency_ms=round((time.perf_counter() - started) * 1000),
                http_status=failure.http_status,
                attempts=retrying.statistics.get("attempt_number", 1),
                error=_scrub(str(failure)),
            )
            raise ProviderError(f"{self.provider} {endpoint}: {record.error}", record) from None
        except BaseException:
            self._guard.settle(reservation, 0.0)  # cancelled or crashed: give the reservation back
            raise

        units = estimated_units
        reported_units = False
        cost: float | None = None
        try:
            if units_from is not None:
                units = units_from(response)
                reported_units = True
            if cost_from is not None:
                cost = cost_from(response)
        except (ValueError, KeyError, IndexError, TypeError):
            pass  # the response does not report usage; keep the estimate
        if cost is not None:
            actual = cost
        elif reported_units:
            actual = self.price.cost(units)
        else:
            actual = estimated_cost  # nothing was reported: the estimate is what gets billed
        self._guard.settle(reservation, actual)
        record = self._record(
            endpoint,
            status="ok",
            units=units,
            cost_usd=actual,
            latency_ms=latency_ms,
            http_status=response.status_code,
            attempts=retrying.statistics.get("attempt_number", 1),
        )
        return response, record

    async def _attempt(self, send: Send) -> tuple[httpx.Response, int]:
        """One try: throttle, send under the timeout and classify the outcome."""
        await self._throttle()
        sent = time.perf_counter()
        try:
            async with asyncio.timeout(self._timeout_s):
                response = await send()
        except (TimeoutError, httpx.TimeoutException) as error:
            raise _SendError(
                f"timed out after {self._timeout_s:g}s", retryable=self._retry_timeouts, timed_out=True
            ) from error
        except httpx.TransportError as error:
            raise _SendError(f"{type(error).__name__}: {error}", retryable=True) from error
        latency_ms = round((time.perf_counter() - sent) * 1000)
        if response.is_success:
            return response, latency_ms
        retryable = response.status_code == 429 or response.status_code >= 500
        raise _SendError(
            f"HTTP {response.status_code}: {response.text[:_ERROR_TEXT_CHARS]}",
            retryable=retryable,
            http_status=response.status_code,
        )

    async def _throttle(self) -> None:
        """Space request starts at least ``min_interval_s`` apart."""
        if self._min_interval_s <= 0:
            return
        async with self._interval_lock:
            now = time.monotonic()
            wait = self._next_start - now
            if wait > 0:
                await asyncio.sleep(wait)
            self._next_start = max(now, self._next_start) + self._min_interval_s

    def _record(
        self,
        endpoint: str,
        *,
        status: CallStatus,
        units: float,
        cost_usd: float,
        latency_ms: int,
        http_status: int | None,
        attempts: int,
        error: str | None = None,
    ) -> ProviderCall:
        return ProviderCall(
            provider=self.provider,
            endpoint=endpoint,
            units=units,
            cost_usd=cost_usd,
            latency_ms=latency_ms,
            status=status,
            http_status=http_status,
            attempts=attempts,
            error=error,
            at=datetime.now(UTC),
        )
