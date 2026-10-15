"""Downloading a web page over HTTP, connected to the address that passed the SSRF check.

Every URL, including each redirect target, is resolved first and refused unless every address it resolves to is public.
The request then goes to that address (the host name travels in the ``Host`` header and the TLS server name, so
certificates are still checked against the real name), which closes the DNS-rebinding gap of ``httpx_fetch``: a second
DNS answer can no longer send the connection somewhere else. Redirects are followed by hand and capped, the body is read
only up to ``max_bytes``, and only text is read at all.
"""

import asyncio
from typing import Protocol
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx
from pydantic import BaseModel

from tg_discovery.providers.fetch.httpx_fetch import (
    Resolver,
    UnresolvableHostError,
    UnsafeUrlError,
    public_addresses,
    system_resolver,
)

PAGE_USER_AGENT = "tg-discovery/0.1 (event picture finder; honours robots.txt)"
_REDIRECTS = frozenset({301, 302, 303, 307, 308})
_TEXT_TYPES = ("text/", "application/xhtml")


class FetchedPage(BaseModel):
    """What a server answered: ``text`` is empty unless the answer was a text document."""

    url: str  # the address that finally answered, after redirects
    status: int
    content_type: str | None = None
    text: str = ""


class PageFetcher(Protocol):
    async def fetch(self, url: str) -> FetchedPage | None:
        """The server's answer, or ``None`` when nothing could be fetched safely (unsafe, unreachable, too slow)."""
        ...


def _pinned(url: str, address: str) -> tuple[str, dict[str, str], dict[str, str]]:
    """The URL with the host replaced by ``address``, the ``Host`` header to send and the TLS server name to check."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    literal = f"[{address}]" if ":" in address else address
    netloc = f"{literal}:{parts.port}" if parts.port else literal
    pinned = urlunsplit((parts.scheme, netloc, parts.path or "/", parts.query, ""))
    extensions = {"sni_hostname": host} if parts.scheme == "https" else {}
    return pinned, {"Host": parts.netloc}, extensions


class HttpxPageFetcher:
    """Fetches text pages, one request per redirect hop, each pinned to a checked address."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        resolver: Resolver = system_resolver,
        timeout_s: float = 15.0,
        max_redirects: int = 3,
        max_bytes: int = 3_000_000,
        user_agent: str = PAGE_USER_AGENT,
    ) -> None:
        self._client = client
        self._resolver = resolver
        self._timeout_s = timeout_s
        self._max_redirects = max_redirects
        self._max_bytes = max_bytes
        self._headers = {
            "User-Agent": user_agent,
            "Accept": "text/html,application/xhtml+xml,text/plain;q=0.8,*/*;q=0.1",
        }

    async def fetch(self, url: str) -> FetchedPage | None:
        current = url
        for _ in range(self._max_redirects + 1):
            try:
                addresses = await public_addresses(current, self._resolver)
                async with asyncio.timeout(self._timeout_s):
                    outcome = await self._get(current, addresses[0])
            except (UnsafeUrlError, UnresolvableHostError, TimeoutError, httpx.HTTPError):
                return None
            if isinstance(outcome, str):
                current = urljoin(current, outcome)
                continue
            return outcome
        return None  # too many redirects

    async def _get(self, url: str, address: str) -> FetchedPage | str:
        """One request: the page, or the address it redirects to."""
        pinned, host_header, extensions = _pinned(url, address)
        headers = {**self._headers, **host_header}
        async with self._client.stream(
            "GET", pinned, headers=headers, extensions=extensions, follow_redirects=False
        ) as response:
            location = response.headers.get("location")
            if response.status_code in _REDIRECTS and location:
                return location
            content_type = response.headers.get("content-type")
            if not (content_type or "").lower().startswith(_TEXT_TYPES):
                return FetchedPage(url=url, status=response.status_code, content_type=content_type)
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body += chunk
                if len(body) >= self._max_bytes:
                    break
            return FetchedPage(
                url=url,
                status=response.status_code,
                content_type=content_type,
                text=_decode(bytes(body[: self._max_bytes]), response.charset_encoding),
            )


def _decode(body: bytes, charset: str | None) -> str:
    """Decode with the charset the server named; an unknown name falls back to UTF-8. Bad bytes never raise."""
    try:
        return body.decode(charset or "utf-8", errors="replace")
    except LookupError:
        return body.decode("utf-8", errors="replace")
