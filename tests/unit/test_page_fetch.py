from collections.abc import AsyncIterator, Sequence

import httpx
import pytest
import respx

from tg_discovery.providers.fetch.page_fetch import PAGE_USER_AGENT, HttpxPageFetcher
from tg_discovery.providers.fetch.ssrf_guard import UnresolvableHostError

PUBLIC = "93.184.216.34"
PUBLIC_V6 = "2606:2800:220:1:248:1893:25c8:1946"
HTML = {"content-type": "text/html; charset=utf-8"}


async def _resolver(host: str) -> Sequence[str]:
    table: dict[str, Sequence[str]] = {
        "site.example": [PUBLIC],
        "other.example": ["93.184.216.35"],
        "v6.example": [PUBLIC_V6],
        "internal.example": ["10.1.2.3"],
    }
    if host not in table:
        raise UnresolvableHostError(host)
    return table[host]


@pytest.fixture
async def fetcher() -> AsyncIterator[HttpxPageFetcher]:
    async with httpx.AsyncClient() as client:
        yield HttpxPageFetcher(client, resolver=_resolver, max_redirects=3, max_bytes=1_000)


@respx.mock
async def test_the_request_goes_to_the_checked_address_and_names_the_real_host(fetcher: HttpxPageFetcher) -> None:
    route = respx.get(f"https://{PUBLIC}/e/night").mock(
        return_value=httpx.Response(200, headers=HTML, text="<p>hi</p>")
    )

    page = await fetcher.fetch("https://site.example/e/night")

    assert page is not None
    assert (page.url, page.status, page.text) == ("https://site.example/e/night", 200, "<p>hi</p>")
    request = route.calls.last.request
    assert request.headers["host"] == "site.example"
    assert request.extensions["sni_hostname"] == "site.example"  # the certificate is checked against the real name
    assert request.headers["user-agent"] == PAGE_USER_AGENT
    assert "text/html" in request.headers["accept"]


@respx.mock
async def test_a_plain_http_request_keeps_its_port_and_query_and_sends_no_tls_name(fetcher: HttpxPageFetcher) -> None:
    route = respx.get(f"http://{PUBLIC}:8080/p").mock(return_value=httpx.Response(200, headers=HTML, text="ok"))

    page = await fetcher.fetch("http://site.example:8080/p?x=1")

    assert page is not None
    assert route.calls.last.request.url.query == b"x=1"
    assert route.calls.last.request.headers["host"] == "site.example:8080"
    assert "sni_hostname" not in route.calls.last.request.extensions


@respx.mock
async def test_an_ipv6_address_is_put_in_brackets(fetcher: HttpxPageFetcher) -> None:
    route = respx.get(f"https://[{PUBLIC_V6}]/p").mock(return_value=httpx.Response(200, headers=HTML, text="v6"))

    page = await fetcher.fetch("https://v6.example/p")

    assert page is not None
    assert route.called


@respx.mock
async def test_redirects_are_followed_by_hand_and_each_hop_is_checked_and_pinned(fetcher: HttpxPageFetcher) -> None:
    respx.get(f"https://{PUBLIC}/old").mock(return_value=httpx.Response(301, headers={"location": "/new"}))
    respx.get(f"https://{PUBLIC}/new").mock(
        return_value=httpx.Response(302, headers={"location": "https://other.example/final"})
    )
    last = respx.get("https://93.184.216.35/final").mock(return_value=httpx.Response(200, headers=HTML, text="done"))

    page = await fetcher.fetch("https://site.example/old")

    assert page is not None
    assert (page.url, page.text) == ("https://other.example/final", "done")
    assert last.calls.last.request.headers["host"] == "other.example"


@respx.mock
async def test_a_redirect_to_a_private_address_is_refused_before_anything_is_sent(fetcher: HttpxPageFetcher) -> None:
    respx.get(f"https://{PUBLIC}/go").mock(
        return_value=httpx.Response(302, headers={"location": "https://internal.example/x"})
    )
    private = respx.get("https://10.1.2.3/x").mock(return_value=httpx.Response(200, headers=HTML, text="secret"))
    metadata = respx.get("http://169.254.169.254/latest/meta-data/").mock(return_value=httpx.Response(200, text="keys"))

    assert await fetcher.fetch("https://site.example/go") is None
    respx.get(f"https://{PUBLIC}/meta").mock(
        return_value=httpx.Response(302, headers={"location": "http://169.254.169.254/latest/meta-data/"})
    )
    assert await fetcher.fetch("https://site.example/meta") is None
    assert not private.called
    assert not metadata.called


@respx.mock
async def test_too_many_redirects_give_nothing(fetcher: HttpxPageFetcher) -> None:
    respx.get(f"https://{PUBLIC}/loop").mock(return_value=httpx.Response(302, headers={"location": "/loop"}))

    assert await fetcher.fetch("https://site.example/loop") is None


@respx.mock
async def test_a_document_that_is_not_text_is_answered_without_reading_its_body(fetcher: HttpxPageFetcher) -> None:
    respx.get(f"https://{PUBLIC}/big.png").mock(
        return_value=httpx.Response(200, headers={"content-type": "image/png"}, content=b"\x89PNG" * 10_000)
    )

    page = await fetcher.fetch("https://site.example/big.png")

    assert page is not None
    assert (page.status, page.content_type, page.text) == (200, "image/png", "")


@respx.mock
async def test_the_body_is_read_only_up_to_the_cap(fetcher: HttpxPageFetcher) -> None:
    respx.get(f"https://{PUBLIC}/long").mock(return_value=httpx.Response(200, headers=HTML, text="a" * 50_000))

    page = await fetcher.fetch("https://site.example/long")

    assert page is not None
    assert len(page.text) == 1_000


@respx.mock
async def test_the_charset_the_server_names_is_used_and_an_unknown_one_falls_back_to_utf8(
    fetcher: HttpxPageFetcher,
) -> None:
    respx.get(f"https://{PUBLIC}/latin").mock(
        return_value=httpx.Response(
            200, headers={"content-type": "text/html; charset=iso-8859-1"}, content="café".encode("latin-1")
        )
    )
    respx.get(f"https://{PUBLIC}/odd").mock(
        return_value=httpx.Response(
            200, headers={"content-type": "text/html; charset=nonsense"}, content="café".encode()
        )
    )

    latin = await fetcher.fetch("https://site.example/latin")
    odd = await fetcher.fetch("https://site.example/odd")

    assert latin is not None
    assert odd is not None
    assert (latin.text, odd.text) == ("café", "café")


@respx.mock
async def test_an_error_status_is_reported_with_its_text(fetcher: HttpxPageFetcher) -> None:
    respx.get(f"https://{PUBLIC}/gone").mock(return_value=httpx.Response(404, headers=HTML, text="not found"))

    page = await fetcher.fetch("https://site.example/gone")

    assert page is not None
    assert (page.status, page.text) == (404, "not found")


@pytest.mark.parametrize(
    "url",
    [
        "ftp://site.example/file",
        "http://user:secret@site.example/",
        "http://127.0.0.1:8080/admin",
        "http://169.254.169.254/latest/meta-data/",
        "http://[::1]/",
        "https://internal.example/",
        "https://unknown.example/",
        "file:///etc/passwd",
    ],
)
@respx.mock
async def test_unsafe_and_unresolvable_addresses_give_nothing_and_send_nothing(
    url: str, fetcher: HttpxPageFetcher
) -> None:
    route = respx.route().mock(return_value=httpx.Response(200, headers=HTML, text="x"))

    assert await fetcher.fetch(url) is None
    assert not route.called


@pytest.mark.parametrize(
    "failure", [httpx.ReadTimeout("slow"), httpx.ConnectError("down"), httpx.RemoteProtocolError("bad")]
)
@respx.mock
async def test_network_failures_give_nothing(failure: Exception, fetcher: HttpxPageFetcher) -> None:
    respx.get(f"https://{PUBLIC}/p").mock(side_effect=failure)

    assert await fetcher.fetch("https://site.example/p") is None
