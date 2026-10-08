import pytest

from tg_discovery.domain.urls import clean_url, is_aggregator, registrable_domain, url_identity, website_key


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://Heaven.com/Events/?utm_source=ig&fbclid=abc#top", "https://heaven.com/Events"),
        ("www.heaven.com/events", "https://www.heaven.com/events"),
        ("http://heaven.com:80/a/", "http://heaven.com/a"),
        ("https://heaven.com:8443/a", "https://heaven.com:8443/a"),
        ("https://heaven.com/?b=2&a=1&utm_campaign=x", "https://heaven.com/?a=1&b=2"),
        ("https://ゲイバー.jp/", "https://xn--eck1a5fwh.jp"),
        ("  https://heaven.com  ", "https://heaven.com"),
    ],
)
def test_clean_url(raw: str, expected: str) -> None:
    assert clean_url(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "mailto:info@heaven.com",
        "tel:+6621234567",
        "javascript:void(0)",
        "see our website",
        "heaven",
        "ftp://heaven.com",
    ],
)
def test_clean_url_rejects_what_is_not_a_web_url(raw: str) -> None:
    assert clean_url(raw) is None


def test_url_identity_ignores_scheme_www_tracking_and_trailing_slash() -> None:
    first = url_identity("http://www.Heaven.com/events/?utm_source=x#top")
    second = url_identity("https://heaven.com/events")

    assert first == second == "heaven.com/events"


def test_url_identity_keeps_meaningful_query_parameters() -> None:
    assert url_identity("https://heaven.com/e?id=7&utm_medium=x") == "heaven.com/e?id=7"
    assert url_identity("https://heaven.com/e?id=7") != url_identity("https://heaven.com/e?id=8")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("https://events.heaven.co.uk/x", "heaven.co.uk"),
        ("WWW.Example.COM", "example.com"),
        ("https://foo.blogspot.com/post", "foo.blogspot.com"),
        ("https://192.168.1.1/admin", None),
        ("localhost", None),
    ],
)
def test_registrable_domain(value: str, expected: str | None) -> None:
    assert registrable_domain(value) == expected


def test_unicode_and_punycode_hosts_have_the_same_registrable_domain() -> None:
    assert registrable_domain("https://ゲイバー.jp") == registrable_domain("https://xn--eck1a5fwh.jp")


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://www.heavenbar.co.uk/events", "heavenbar.co.uk"),
        ("https://facebook.com/heaven", None),
        ("https://www.instagram.com/heaven", None),
        ("https://www.google.co.th/maps/place/heaven", None),
        ("https://www.tripadvisor.co.uk/Restaurant_Review-heaven", None),
        ("https://linktr.ee/heaven", None),
        (None, None),
        ("", None),
    ],
)
def test_website_key_only_identifies_a_venues_own_site(url: str | None, expected: str | None) -> None:
    assert website_key(url) == expected


def test_aggregators_are_matched_on_domain_or_brand_label() -> None:
    assert is_aggregator("eventbrite.com")
    assert is_aggregator("booking.com")
    assert is_aggregator("google.com.au")
    assert not is_aggregator("heavenbar.com")
