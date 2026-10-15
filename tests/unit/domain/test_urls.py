import pytest

from tg_discovery.domain.urls import (
    clean_image_url,
    clean_url,
    is_aggregator,
    is_placeholder,
    picture_key,
    picture_width,
    registrable_domain,
    url_identity,
    website_key,
)


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


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://cdn.example/posters/drag-night.jpg", "https://cdn.example/posters/drag-night.jpg"),
        ("  https://cdn.example/a.png#frag  ", "https://cdn.example/a.png"),
        # the query is kept as written: signed image URLs break when the parameters are reordered
        ("https://img.example/p.jpg?w=940&s=ab12&auto=format", "https://img.example/p.jpg?w=940&s=ab12&auto=format"),
        (
            "https://img.example/https%3A%2F%2Fcdn.example%2Fp.jpg?h=2000",
            "https://img.example/https%3A%2F%2Fcdn.example%2Fp.jpg?h=2000",
        ),
    ],
)
def test_clean_image_url_keeps_a_photo_address_as_written(raw: str, expected: str) -> None:
    assert clean_image_url(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "data:image/png;base64,AAAA",
        "/images/poster.jpg",
        "//cdn.example/poster.jpg",
        "poster.jpg",
        "javascript:void(0)",
        "https://cdn.example/site/logo.png",
        "https://cdn.example/img/Logo-dark.webp",
        "https://cdn.example/favicon.png",
        "https://cdn.example/icons/pin.png",
        "https://cdn.example/u/avatar/42.jpg",
        "https://cdn.example/banner.svg",
        "https://cdn.example/favicon.ico?v=2",
        "https://cdn.example/loading.gif",
        "https://track.example/p/1x1.png",
        "https://cdn.example/sprite.png",
    ],
)
def test_clean_image_url_refuses_what_is_not_a_photo(raw: str) -> None:
    assert clean_image_url(raw) is None


@pytest.mark.parametrize(
    "raw",
    [
        "https://sb.scorecardresearch.com/p?c1=2&c2=6035310",
        "https://stats.g.doubleclick.net/pixel.gif",
        "https://www.googletagmanager.com/ns.html?id=GTM-1",
        "https://cdn.example/people/headshot-jane.jpg",
        "https://cdn.example/assets/img-default.jpg",
        "https://cdn.example/no-image.png",
    ],
)
def test_clean_image_url_refuses_trackers_headshots_and_default_pictures(raw: str) -> None:
    assert clean_image_url(raw) is None


@pytest.mark.parametrize(
    ("first", "second"),
    [
        # Wix: sizes, crops and the encoded form of the same media id
        (
            "https://static.wixstatic.com/media/aa_1~mv2.png/v1/fill/w_888,h_508,al_c,q_90/aa_1~mv2.png",
            "https://static.wixstatic.com/media/aa_1%7Emv2.png/v1/crop/x_0,w_100/fill/w_74,h_42,blur_2/x.png",
        ),
        # WordPress and Shopify size suffixes, and another file format
        ("https://s.example/wp-content/uploads/a-300x200.jpg", "https://s.example/wp-content/uploads/a.jpg"),
        ("https://s.example/files/poster_800x.png", "https://s.example/files/poster.png"),
        ("https://s.example/uploads/poster.jpg", "https://s.example/uploads/poster.webp"),
        # size folders, the way Time Out writes them
        ("https://m.example/images/1064/400/225/image.webp", "https://m.example/images/1064/image.jpg"),
        # Cloudflare image resizing
        (
            "https://s.example/cdn-cgi/image/width=640,quality=75/https://s.example/i/c.webp",
            "https://s.example/i/c.webp",
        ),
        # Next.js image optimiser and WordPress's Photon proxy
        ("https://s.example/_next/image?url=%2Fimg%2Fc.webp&w=1920&q=75", "https://s.example/img/c.webp"),
        (
            "https://i0.wp.com/s.example/wp-content/uploads/a.png?fit=1200%2C600&ssl=1",
            "https://s.example/wp-content/uploads/a.png",
        ),
        # a named file: only an id can tell pictures apart, so sizing and crop parameters never do
        (
            "https://m.example/lib/tom.jpg?id=7&width=1200&height=600&coordinates=0%2C166",
            "https://m.example/lib/tom.jpg?id=7&width=2000",
        ),
        ("https://m.example/up/s.png?quality=51&w=758&p=q", "https://m.example/up/s.png?quality=51&w=758"),
        # tracking parameters and www
        ("https://www.s.example/a.jpg?utm_source=x&fbclid=1", "https://s.example/a.jpg"),
        # imgix-style sizing parameters on an address with no file extension
        (
            "https://img.example/e/original.20240101?w=512&auto=format&s=abc",
            "https://img.example/e/original.20240101?w=1024&s=def",
        ),
    ],
)
def test_picture_key_is_the_same_for_spellings_of_one_picture(first: str, second: str) -> None:
    assert picture_key(first) == picture_key(second)


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("https://s.example/uploads/2024/10/a.jpg", "https://s.example/uploads/2025/01/a.jpg"),  # a date is not a size
        ("https://s.example/a.jpg", "https://s.example/b.jpg"),
        ("https://s.example/a.jpg", "https://other.example/a.jpg"),
        ("https://m.example/lib/tom.jpg?id=7", "https://m.example/lib/tom.jpg?id=8"),
        (
            "https://static.wixstatic.com/media/aa_1~mv2.png/v1/fill/w_9/x.png",
            "https://static.wixstatic.com/media/aa_2~mv2.png/v1/fill/w_9/x.png",
        ),
        ("https://s.example/get?image=1", "https://s.example/get?image=2"),
    ],
)
def test_picture_key_never_merges_different_pictures(first: str, second: str) -> None:
    assert picture_key(first) != picture_key(second)


@pytest.mark.parametrize(
    ("url", "width"),
    [
        ("https://static.wixstatic.com/media/a~mv2.png/v1/fill/w_888,h_508,al_c/a.png", 888),
        ("https://static.wixstatic.com/media/a~mv2.png/v1/fill/w_888%2Ch_508/a.png", 888),
        ("https://s.example/cdn-cgi/image/width=3840,quality=75/https://s.example/a.webp", 3840),
        ("https://s.example/a.jpg?w=640&q=70", 640),
        ("https://s.example/a.jpg?width=1200", 1200),
        ("https://s.example/uploads/a-300x200.jpg", 300),
        ("https://s.example/files/a_800x.png", 800),
        ("https://m.example/images/1064/1920/1080/image.webp", 1920),
        ("https://s.example/a.jpg", None),
        ("https://s.example/uploads/2024/10/a.jpg", None),
    ],
)
def test_picture_width_reads_the_size_an_address_asks_for(url: str, width: int | None) -> None:
    assert picture_width(url) == width


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://static.wixstatic.com/media/a~mv2.png/v1/fill/w_74,h_42,blur_2,enc_avif/a.png", True),
        ("https://s.example/a.jpg?blur=20", True),
        ("https://s.example/lqip/a.jpg", True),
        ("https://s.example/placeholder.png", True),
        ("https://s.example/a.jpg", False),
    ],
)
def test_is_placeholder_spots_the_blurred_stand_ins(url: str, expected: bool) -> None:
    assert is_placeholder(url) is expected


def test_clean_image_url_does_not_mistake_longer_words_for_icons() -> None:
    assert clean_image_url("https://cdn.example/catalogue/lipstick-party.jpg") is not None
    assert clean_image_url("https://cdn.example/dialogue/poster.jpg") is not None


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
