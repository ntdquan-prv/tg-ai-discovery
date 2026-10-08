"""Probing URLs over plain HTTP behind an SSRF guard.

Every URL, including each redirect target, is resolved first and refused unless every address it resolves to is public:
loopback, private, link-local (the cloud metadata address), carrier-grade NAT and other reserved ranges are blocked.
Redirects are followed by hand, capped at ``max_redirects``, and only response headers are ever read.

Known limit: the name is resolved once for the check and again by the HTTP client, so a hostile DNS server could
answer differently the second time (DNS rebinding). That is acceptable for the offline bake-off, which only probes
URLs the providers returned; pin the resolved address in the connection before using this for page fetches in M2.
"""

import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable, Sequence
from typing import Literal, Protocol
from urllib.parse import urljoin, urlsplit

import httpx
from pydantic import BaseModel

DEFAULT_USER_AGENT = "tg-discovery-url-check/0.1 (link liveness probe for a venue and event review tool)"
_BLOCKED_STATUS = frozenset({401, 403, 429, 451, 503, 999})  # bot walls: the page may well exist
_RETRY_WITH_GET = frozenset({400, 403, 405, 501})
_REDIRECTS = frozenset({301, 302, 303, 307, 308})

Resolver = Callable[[str], Awaitable[Sequence[str]]]


class UnsafeUrlError(ValueError):
    """The URL points somewhere the service must never connect to."""


class UnresolvableHostError(ValueError):
    """The host name does not resolve."""


class UrlProbe(BaseModel):
    status: Literal["alive", "dead", "blocked", "unsafe"]
    http_status: int | None = None
    reason: str | None = None


class UrlProber(Protocol):
    async def probe(self, url: str) -> UrlProbe: ...


async def system_resolver(host: str) -> list[str]:
    """Resolve a host name with the operating system's resolver."""
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror as error:
        raise UnresolvableHostError(host) from error
    return sorted({str(info[4][0]) for info in infos})


def is_public_address(address: str) -> bool:
    ip = ipaddress.ip_address(address.split("%", 1)[0])
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


async def assert_public_url(url: str, resolver: Resolver = system_resolver) -> None:
    """Raise ``UnsafeUrlError`` unless ``url`` is plain http(s) to a host whose every address is public."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        msg = f"scheme '{parts.scheme}' is not allowed"
        raise UnsafeUrlError(msg)
    if parts.username or parts.password:
        msg = "credentials in the URL are not allowed"
        raise UnsafeUrlError(msg)
    host = parts.hostname
    if not host:
        msg = "the URL has no host"
        raise UnsafeUrlError(msg)
    try:
        addresses: Sequence[str] = [str(ipaddress.ip_address(host))]
    except ValueError:
        addresses = await resolver(host)
    for address in addresses:
        if not is_public_address(address):
            msg = f"{host} resolves to the non-public address {address}"
            raise UnsafeUrlError(msg)


def _classify(status: int) -> UrlProbe:
    if 200 <= status < 300:
        return UrlProbe(status="alive", http_status=status)
    if status in _BLOCKED_STATUS:
        return UrlProbe(status="blocked", http_status=status, reason="the site refuses automated requests")
    return UrlProbe(status="dead", http_status=status, reason=f"HTTP {status}")


class HttpxUrlProber:
    """Checks whether a URL answers, without downloading the page."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        resolver: Resolver = system_resolver,
        timeout_s: float = 10.0,
        max_redirects: int = 3,
        user_agent: str = DEFAULT_USER_AGENT,
    ) -> None:
        self._client = client
        self._resolver = resolver
        self._timeout_s = timeout_s
        self._max_redirects = max_redirects
        self._headers = {"User-Agent": user_agent, "Accept": "text/html,*/*;q=0.5"}

    async def probe(self, url: str) -> UrlProbe:
        """Follow at most ``max_redirects`` redirects and report whether the final page answers."""
        current = url
        for _ in range(self._max_redirects + 1):
            try:
                await assert_public_url(current, self._resolver)
            except UnsafeUrlError as error:
                return UrlProbe(status="unsafe", reason=str(error))
            except UnresolvableHostError:
                return UrlProbe(status="dead", reason="the host name does not resolve")
            try:
                async with asyncio.timeout(self._timeout_s):
                    status, location = await self._headers_only(current)
            except (TimeoutError, httpx.TransportError) as error:
                return UrlProbe(status="dead", reason=type(error).__name__)
            if status in _REDIRECTS and location:
                current = urljoin(current, location)
                continue
            return _classify(status)
        return UrlProbe(status="dead", reason="too many redirects")

    async def _headers_only(self, url: str) -> tuple[int, str | None]:
        """HEAD first; servers that refuse it get a GET whose body is never read."""
        response = await self._client.request("HEAD", url, headers=self._headers, follow_redirects=False)
        if response.status_code in _RETRY_WITH_GET:
            async with self._client.stream(
                "GET", url, headers={**self._headers, "Range": "bytes=0-1023"}, follow_redirects=False
            ) as streamed:
                return streamed.status_code, streamed.headers.get("location")
        return response.status_code, response.headers.get("location")
