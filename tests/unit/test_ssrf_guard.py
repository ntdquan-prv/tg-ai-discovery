from collections.abc import Sequence

import pytest

from tg_discovery.providers.fetch.ssrf_guard import (
    UnresolvableHostError,
    UnsafeUrlError,
    is_public_address,
    public_addresses,
)

PUBLIC = "93.184.216.34"


async def _resolver(host: str) -> Sequence[str]:
    table: dict[str, Sequence[str]] = {
        "venue.example": [PUBLIC],
        "internal.example": ["10.1.2.3"],
        "mixed.example": [PUBLIC, "192.168.0.9"],
    }
    if host not in table:
        raise UnresolvableHostError(host)
    return table[host]


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
        await public_addresses(url, _resolver)


async def test_a_public_url_passes_the_guard_with_its_addresses() -> None:
    assert await public_addresses("https://venue.example/events", _resolver) == [PUBLIC]
    assert await public_addresses(f"http://{PUBLIC}/", _resolver) == [PUBLIC]


async def test_an_unresolvable_host_is_reported_as_such() -> None:
    with pytest.raises(UnresolvableHostError):
        await public_addresses("https://no-such-host.example/", _resolver)
