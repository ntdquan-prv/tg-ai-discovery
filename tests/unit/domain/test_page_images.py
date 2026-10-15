import pytest

from tg_discovery.domain.page_images import MAX_PICTURES, find_pictures

PAGE = "https://events.example/e/drag-night"


def _urls(html: str, base: str = PAGE, **options: int) -> list[str]:
    return [picture.url for picture in find_pictures(html, base, **options)]


def test_the_social_preview_comes_first_and_the_page_images_follow_in_page_order() -> None:
    html = """
    <html><head>
      <meta property="og:image" content="https://cdn.example/cover.jpg">
      <meta property="og:image:width" content="1200"><meta property="og:image:height" content="630">
      <meta name="twitter:image" content="https://cdn.example/cover.jpg">
    </head><body><main>
      <img src="https://cdn.example/one.jpg" width="800" alt="The stage">
      <img src="https://cdn.example/two.jpg" width="800">
    </main></body></html>
    """

    pictures = find_pictures(html, PAGE)

    assert [(item.url, item.alt) for item in pictures] == [
        ("https://cdn.example/cover.jpg", None),
        ("https://cdn.example/one.jpg", "The stage"),
        ("https://cdn.example/two.jpg", None),
    ]


def test_json_ld_images_are_read_in_every_shape() -> None:
    html = """
    <script type="application/ld+json">
    {"@graph": [
      {"@type": "Event", "image": "https://cdn.example/a.jpg"},
      {"@type": "Event", "image": ["https://cdn.example/b.jpg", {"url": "https://cdn.example/c.jpg"}]},
      {"@type": "Event", "organizer": {"image": {"@type": "ImageObject", "contentUrl": "https://cdn.example/d.jpg"}}}
    ]}
    </script>
    <script type="application/ld+json">{ this is not json</script>
    """

    assert _urls(html) == [f"https://cdn.example/{name}.jpg" for name in "abcd"]


def test_the_widest_spelling_of_a_picture_is_kept_and_blurred_stand_ins_are_dropped() -> None:
    # The way Wix writes a picture: a blurred stand-in in src, the full picture in Pinterest's attribute.
    full = "https://static.wixstatic.com/media/aa_1~mv2.png/v1/fill/w_888,h_508,al_c,q_90/aa_1~mv2.png"
    blurred = "https://static.wixstatic.com/media/aa_1~mv2.png/v1/fill/w_74,h_42,blur_2/aa_1~mv2.png"
    html = f'<main><img src="{blurred}" alt="A drag queen" data-pin-media="{full}"></main>'

    pictures = find_pictures(html, PAGE)

    assert [(item.url, item.alt) for item in pictures] == [(full, "A drag queen")]


def test_srcset_candidates_are_read_even_when_the_addresses_contain_commas() -> None:
    small = "https://static.wixstatic.com/media/b~mv2.jpg/v1/fill/w_480,h_320/b~mv2.jpg"
    large = "https://static.wixstatic.com/media/b~mv2.jpg/v1/fill/w_1600,h_1067/b~mv2.jpg"
    html = f'<main><img src="{small}" srcset="{small} 480w, {large} 1600w"></main>'

    assert _urls(html) == [large]


def test_a_picture_element_counts_as_one_picture_in_its_widest_spelling() -> None:
    html = """
    <main><picture>
      <source srcset="https://cdn.example/p-640x427.webp 640w, https://cdn.example/p-1280x853.webp 1280w">
      <img src="https://cdn.example/p-640x427.jpg" alt="Crowd">
    </picture></main>
    """

    assert _urls(html) == ["https://cdn.example/p-1280x853.webp"]


def test_a_spelling_wider_than_2048_is_only_chosen_when_nothing_smaller_exists() -> None:
    def resized(width: int, name: str) -> str:
        return f"https://cdn.example/cdn-cgi/image/width={width},quality=75/https://cdn.example/{name}.jpg"

    sizes = f"{resized(640, 'x')} 640w, {resized(3840, 'x')} 3840w"
    assert _urls(f'<main><img srcset="{sizes}"></main>') == [resized(640, "x")]

    only_huge = f"{resized(5000, 'y')} 5000w, {resized(3840, 'y')} 3840w"
    assert _urls(f'<main><img srcset="{only_huge}"></main>') == [resized(3840, "y")]


def test_icons_the_page_declares_and_every_crop_of_them_are_dropped() -> None:
    html = """
    <head>
      <link rel="icon" href="https://static.wixstatic.com/media/logo_1~mv2.png/v1/fill/w_32,h_32/logo_1~mv2.png">
      <link rel="apple-touch-icon" href="https://static.wixstatic.com/media/logo_1~mv2.png/v1/fill/w_180,h_180/x.png">
    </head>
    <body><main>
      <img src="https://static.wixstatic.com/media/logo_1~mv2.png/v1/crop/x_0,w_900,h_300/fill/w_900,h_300/logo_1~mv2.png">
      <img src="https://static.wixstatic.com/media/photo_9~mv2.jpg/v1/fill/w_900,h_600/photo_9~mv2.jpg">
    </main></body>
    """

    assert _urls(html) == ["https://static.wixstatic.com/media/photo_9~mv2.jpg/v1/fill/w_900,h_600/photo_9~mv2.jpg"]


def test_pictures_in_the_header_navigation_and_footer_are_dropped_unless_they_are_big() -> None:
    html = """
    <header>
      <img src="https://cdn.example/brand.jpg" width="200">
      <img src="https://cdn.example/hero.jpg" width="1600">
    </header>
    <nav><img src="https://cdn.example/menu.jpg" width="500"></nav>
    <div><img src="https://cdn.example/body.jpg" width="800"></div>
    <footer><img src="https://cdn.example/partner.jpg" width="500"></footer>
    """

    assert _urls(html) == ["https://cdn.example/hero.jpg", "https://cdn.example/body.jpg"]


def test_when_the_page_marks_its_article_only_the_pictures_inside_it_count() -> None:
    html = """
    <img src="https://cdn.example/outside.jpg" width="800">
    <article><img src="https://cdn.example/inside.jpg" width="800"></article>
    <div class="sidebar"><img src="https://cdn.example/sidebar.jpg" width="800"></div>
    """

    assert _urls(html) == ["https://cdn.example/inside.jpg"]


def test_without_an_article_every_body_picture_counts() -> None:
    html = (
        '<div><img src="https://cdn.example/a.jpg" width="800"></div>'
        '<div><img src="https://cdn.example/b.jpg" width="800"></div>'
    )

    assert _urls(html) == ["https://cdn.example/a.jpg", "https://cdn.example/b.jpg"]


def test_a_picture_inside_a_link_to_another_page_is_a_teaser_for_it_and_is_dropped() -> None:
    html = f"""
    <main>
      <img src="https://cdn.example/mine.jpg" width="800">
      <a href="/e/another-night"><img src="https://cdn.example/teaser.jpg" width="800"></a>
      <a href="{PAGE}#top"><img src="https://cdn.example/self.jpg" width="800"></a>
      <a href="https://cdn.example/big/zoom.jpg"><img src="https://cdn.example/thumb-zoomable.jpg" width="800"></a>
      <a href="#gallery"><img src="https://cdn.example/anchor.jpg" width="800"></a>
    </main>
    """

    assert _urls(html) == [
        "https://cdn.example/mine.jpg",
        "https://cdn.example/self.jpg",
        "https://cdn.example/thumb-zoomable.jpg",
        "https://cdn.example/anchor.jpg",
    ]


@pytest.mark.parametrize(
    "markup",
    [
        '<img src="https://cdn.example/a.jpg" alt="Company logo" width="800">',
        '<img src="https://cdn.example/a.jpg" class="site-logo" width="800">',
        '<img src="https://cdn.example/a.jpg" id="avatar" width="800">',
        '<img src="https://cdn.example/a.jpg" class="author-photo" width="800">',
        '<img src="https://cdn.example/a.jpg" width="120">',
        '<img src="https://cdn.example/a.jpg" width="900" height="100">',
        '<img src="https://cdn.example/a.jpg" width="100" height="900">',
        '<img src="https://cdn.example/logo.png" width="800">',
        '<img src="data:image/png;base64,AAAA" width="800">',
        '<img src="https://cdn.example/a.svg" width="800">',
        '<img src="https://track.example/p/1x1.png">',
        '<img src="https://sb.scorecardresearch.com/p?c1=2&c2=6035310">',
        '<img src="https://cdn.example/img-default.jpg" width="800">',
        '<img src="https://cdn.example/a-w_300.jpg" width="800" srcset="https://cdn.example/a-w_300.jpg 300w">',
    ],
)
def test_what_is_not_a_picture_of_the_subject_is_dropped(markup: str) -> None:
    result = _urls(f"<main>{markup}</main>")
    assert "https://cdn.example/a.jpg" not in result
    assert not any("logo" in url or "default" in url or "1x1" in url or "scorecard" in url for url in result)
    assert not any(url.endswith(".svg") or url.startswith("data:") for url in result)


def test_relative_and_protocol_relative_addresses_are_resolved_and_a_base_element_is_honoured() -> None:
    html = """
    <head><base href="https://media.example/assets/"></head>
    <main>
      <img src="a.jpg" width="800">
      <img src="/root/b.jpg" width="800">
      <img src="//cdn.example/c.jpg" width="800">
    </main>
    """

    assert _urls(html) == [
        "https://media.example/assets/a.jpg",
        "https://media.example/root/b.jpg",
        "https://cdn.example/c.jpg",
    ]


def test_lazy_loading_attributes_are_read() -> None:
    html = """
    <main>
      <img src="data:image/gif;base64,R0lGOD" data-src="https://cdn.example/lazy.jpg" width="800">
      <img data-lazy-src="https://cdn.example/lazier.jpg" width="800">
    </main>
    """

    assert _urls(html) == ["https://cdn.example/lazy.jpg", "https://cdn.example/lazier.jpg"]


def test_the_number_of_pictures_is_capped() -> None:
    html = "<main>" + "".join(f'<img src="https://cdn.example/{n}.jpg" width="800">' for n in range(30)) + "</main>"

    assert len(_urls(html)) == MAX_PICTURES
    assert len(_urls(html, limit=3)) == 3


@pytest.mark.parametrize("html", ["", "   ", "<<<>>>", "<img", "<html><body><img src=", "plain text with no markup"])
def test_empty_and_broken_markup_give_no_pictures_instead_of_errors(html: str) -> None:
    assert _urls(html) == []


def test_the_pictures_read_before_a_break_in_the_markup_are_kept() -> None:
    html = '<main><img src="https://cdn.example/a.jpg" width="800"><img src="https://cdn.example/b.jpg" width='

    assert _urls(html)[:1] == ["https://cdn.example/a.jpg"]
