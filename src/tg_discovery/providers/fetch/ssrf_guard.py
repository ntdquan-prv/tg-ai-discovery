"""The SSRF guard: which URLs the service may connect to.

A URL passes only when it is plain http(s) without credentials and every address its host resolves to is public:
loopback, private, link-local (the cloud metadata address), carrier-grade NAT and other reserved ranges are blocked.
``page_fetch`` checks every URL and every redirect target here, then connects to the checked address.
"""

import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable, Sequence
from urllib.parse import urlsplit

Resolver = Callable[[str], Awaitable[Sequence[str]]]


class UnsafeUrlError(ValueError):
    """The URL points somewhere the service must never connect to."""


class UnresolvableHostError(ValueError):
    """The host name does not resolve."""


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


async def public_addresses(url: str, resolver: Resolver = system_resolver) -> list[str]:
    """The addresses of the host of ``url``, after checking that the URL is plain http(s) and every address is public.

    Raises ``UnsafeUrlError`` otherwise and ``UnresolvableHostError`` when the name does not resolve. A caller that
    connects to one of the returned addresses (see ``page_fetch``) cannot be steered elsewhere by a second DNS answer.
    """
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
    return list(addresses)
