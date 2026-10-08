from collections.abc import AsyncIterator, Sequence

import httpx
import pytest
import respx

from tg_discovery.providers.fetch.httpx_fetch import (
    HttpxUrlProber,
    UnresolvableHostError,
    UnsafeUrlError,
    assert_public_url,
    is_public_address,
)

PUBLIC = "93.184.216.34"


async def _resolver(host: str) -> Sequence[str]:
    table: dict[str, Sequence[str]] = {
        "venue.example": [PUBLIC],
        "other.example": [PUBLIC],
        "internal.example": ["10.1.2.3"],
        "mixed.example": [PUBLIC, "192.168.0.9"],
    }
    if host not in table:
        raise UnresolvableHostError(host)
    return table[host]


@pytest.fixture
async def prober() -> AsyncIterator[HttpxUrlProber]:
    async with httpx.AsyncClient() as client:
        yield HttpxUrlProber(client, resolver=_resolver)


@pytest.mark.parametrize(
    ("address", "expected"),
    [
        ("93.184.216.34", True),
        ("2606:2800:220:1:248:1893:25c8:1946", True),
        ("10.0.0.1", False),
        ("127.0.0.1", False),
        ("169.254.169.254", False),
        ("100.64.0.1", False),
        ("192.168.1.1", False),
        ("172.16.5.5", False),
        ("0.0.0.0", False),  # noqa: S104 - the address under test, not a bind
        ("224.0.0.1", False),
        ("::1", False),
        ("fd00::1", False),
        ("fe80::1%eth0", False),
        ("::ffff:10.0.0.1", False),
    ],
)
def test_is_public_address(address: str, expected: bool) -> None:
    assert is_public_address(address) is expected


@pytest.mark.parametrize(
    "url",
    [
        "ftp://venue.example/file",
        "file:///etc/passwd",
        "http://user:secret@venue.example/",
        "http://127.0.0.1:8080/admin",
        "http://169.254.169.254/latest/meta-data/",
        "http://[::1]/",
        "http://internal.example/",
        "http://mixed.example/",
        "http:///nohost",
    ],
)
async def test_unsafe_urls_are_refused(url: str) -> None:
    with pytest.raises(UnsafeUrlError):
        await assert_public_url(url, _resolver)


async def test_a_public_url_passes_the_guard() -> None:
    await assert_public_url("https://venue.example/events", _resolver)
    await assert_public_url(f"http://{PUBLIC}/", _resolver)


@respx.mock
async def test_a_live_page_is_alive(prober: HttpxUrlProber) -> None:
    respx.head("https://venue.example/").mock(return_value=httpx.Response(200))

    probe = await prober.probe("https://venue.example/")

    assert (probe.status, probe.http_status) == ("alive", 200)


@respx.mock
async def test_a_missing_page_is_dead(prober: HttpxUrlProber) -> None:
    respx.head("https://venue.example/gone").mock(return_value=httpx.Response(404))

    probe = await prober.probe("https://venue.example/gone")

    assert (probe.status, probe.http_status) == ("dead", 404)


@respx.mock
async def test_a_server_that_refuses_head_gets_a_get_instead(prober: HttpxUrlProber) -> None:
    respx.head("https://venue.example/").mock(return_value=httpx.Response(405))
    get = respx.get("https://venue.example/").mock(return_value=httpx.Response(200))

    probe = await prober.probe("https://venue.example/")

    assert probe.status == "alive"
    assert get.calls.last.request.headers["range"] == "bytes=0-1023"


@respx.mock
async def test_a_bot_wall_is_blocked_not_dead(prober: HttpxUrlProber) -> None:
    respx.head("https://venue.example/").mock(return_value=httpx.Response(403))
    respx.get("https://venue.example/").mock(return_value=httpx.Response(403))

    probe = await prober.probe("https://venue.example/")

    assert (probe.status, probe.http_status) == ("blocked", 403)


@respx.mock
async def test_redirects_are_followed_and_checked_at_every_hop(prober: HttpxUrlProber) -> None:
    respx.head("https://venue.example/old").mock(
        return_value=httpx.Response(301, headers={"Location": "https://other.example/new"})
    )
    respx.head("https://other.example/new").mock(return_value=httpx.Response(200))

    assert (await prober.probe("https://venue.example/old")).status == "alive"


@respx.mock
async def test_a_redirect_to_an_internal_host_is_unsafe(prober: HttpxUrlProber) -> None:
    respx.head("https://venue.example/").mock(
        return_value=httpx.Response(302, headers={"Location": "http://internal.example/secret"})
    )
    internal = respx.head("http://internal.example/secret").mock(return_value=httpx.Response(200))

    probe = await prober.probe("https://venue.example/")

    assert probe.status == "unsafe"
    assert internal.call_count == 0


@respx.mock
async def test_a_redirect_loop_is_dead(prober: HttpxUrlProber) -> None:
    respx.head("https://venue.example/loop").mock(
        return_value=httpx.Response(302, headers={"Location": "https://venue.example/loop"})
    )

    probe = await prober.probe("https://venue.example/loop")

    assert (probe.status, probe.reason) == ("dead", "too many redirects")


@respx.mock
async def test_a_connection_failure_is_dead(prober: HttpxUrlProber) -> None:
    respx.head("https://venue.example/").mock(side_effect=httpx.ConnectError("refused"))

    probe = await prober.probe("https://venue.example/")

    assert (probe.status, probe.reason) == ("dead", "ConnectError")


async def test_an_unresolvable_host_is_dead(prober: HttpxUrlProber) -> None:
    probe = await prober.probe("https://no-such-host.example/")

    assert (probe.status, probe.reason) == ("dead", "the host name does not resolve")
