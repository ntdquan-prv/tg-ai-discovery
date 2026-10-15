import asyncio
from collections import defaultdict
from collections.abc import Mapping
from urllib.parse import urlsplit

from tests.pipeline_builders import NOW, event
from tg_discovery.domain.models import EventDraft, EventImage, SourceRef
from tg_discovery.pipelines.common.pictures import PictureFinder
from tg_discovery.providers.fetch.page_fetch import FetchedPage

PAGE = "https://events.example/e/drag-night"
OTHER = "https://other.example/e/pride-picnic"


def _page(url: str, *images: str) -> FetchedPage:
    body = "".join(f'<img src="{image}" width="800" alt="alt of {image[-5:]}">' for image in images)
    return FetchedPage(url=url, status=200, content_type="text/html", text=f"<main>{body}</main>")


class FakeFetcher:
    """Answers from a table; anything unknown is a 404. Records every call and how many were in flight together."""

    def __init__(
        self, pages: Mapping[str, FetchedPage | Exception | None] | None = None, *, delay_s: float = 0
    ) -> None:
        self.pages = dict(pages or {})
        self.delay_s = delay_s
        self.calls: list[str] = []
        self._in_flight: dict[str, int] = defaultdict(int)
        self.peak_per_host: dict[str, int] = defaultdict(int)
        self.peak_total = 0

    async def fetch(self, url: str) -> FetchedPage | None:
        self.calls.append(url)
        host = urlsplit(url).netloc
        self._in_flight[host] += 1
        self.peak_per_host[host] = max(self.peak_per_host[host], self._in_flight[host])
        self.peak_total = max(self.peak_total, sum(self._in_flight.values()))
        try:
            await asyncio.sleep(self.delay_s)
            answer = self.pages.get(url, FetchedPage(url=url, status=404))
            if isinstance(answer, Exception):
                raise answer
            return answer
        finally:
            self._in_flight[host] -= 1

    def pages_read(self) -> list[str]:
        return [url for url in self.calls if not url.endswith("/robots.txt")]


def _draft(name: str, url: str, *, images: list[str] | None = None) -> EventDraft:
    draft = event(name, images=images)
    return draft.model_copy(update={"sources": [SourceRef(url=url, provider="exa", retrieved_at=NOW)]})


def _finder(fetcher: FakeFetcher, **options: float) -> PictureFinder:
    return PictureFinder(fetcher, host_pause_s=0, **options)  # type: ignore[arg-type]


async def test_an_event_gets_the_pictures_of_the_page_that_is_about_it() -> None:
    fetcher = FakeFetcher({PAGE: _page(PAGE, "https://cdn.example/a.jpg", "https://cdn.example/b.jpg")})

    (result,) = await _finder(fetcher).attach([_draft("Drag Night", PAGE)])

    assert [(image.url, image.source_url) for image in result.images] == [
        ("https://cdn.example/a.jpg", PAGE),
        ("https://cdn.example/b.jpg", PAGE),
    ]
    assert result.images[0].alt == "alt of a.jpg"


async def test_a_page_that_names_two_events_is_not_read_and_adds_no_picture() -> None:
    fetcher = FakeFetcher({PAGE: _page(PAGE, "https://cdn.example/a.jpg")})

    first, second = await _finder(fetcher).attach([_draft("Drag Night", PAGE), _draft("Pride Picnic", PAGE)])

    assert (first.images, second.images) == ([], [])
    assert fetcher.calls == []  # not even its robots.txt: nothing here needs the page


async def test_each_event_gets_only_the_pages_that_are_about_it_alone() -> None:
    fetcher = FakeFetcher(
        {OTHER: _page(OTHER, "https://cdn.example/mine.jpg"), PAGE: _page(PAGE, "https://cdn.example/x.jpg")}
    )
    drafts = [_draft("Drag Night", PAGE), _draft("Pride Picnic", PAGE), _draft("Drag Night", PAGE)]
    mixed = drafts[0].model_copy(
        update={
            "sources": [
                SourceRef(url=PAGE, provider="exa", retrieved_at=NOW),
                SourceRef(url=OTHER, provider="exa", retrieved_at=NOW),
            ]
        }
    )

    first, second, third = await _finder(fetcher).attach([mixed, drafts[1], drafts[2]])

    assert [image.url for image in first.images] == ["https://cdn.example/mine.jpg"]  # PAGE is shared, OTHER is its own
    assert (second.images, third.images) == ([], [])


async def test_pictures_the_event_already_has_stay_first_and_a_picture_is_not_repeated_in_another_size() -> None:
    fetcher = FakeFetcher({PAGE: _page(PAGE, "https://cdn.example/a-1200x800.jpg", "https://cdn.example/b.jpg")})
    draft = _draft("Drag Night", PAGE, images=["https://cdn.example/a.jpg"])

    (result,) = await _finder(fetcher).attach([draft])

    assert [image.url for image in result.images] == ["https://cdn.example/a.jpg", "https://cdn.example/b.jpg"]


async def test_a_draft_that_gains_nothing_is_returned_as_it_was() -> None:
    fetcher = FakeFetcher({PAGE: _page(PAGE)})
    draft = _draft("Drag Night", PAGE)

    (result,) = await _finder(fetcher).attach([draft])

    assert result is draft


async def test_a_page_that_robots_txt_forbids_is_not_read() -> None:
    robots = FetchedPage(url="https://events.example/robots.txt", status=200, text="User-agent: *\nDisallow: /e/\n")
    fetcher = FakeFetcher({"https://events.example/robots.txt": robots, PAGE: _page(PAGE, "https://cdn.example/a.jpg")})

    (result,) = await _finder(fetcher).attach([_draft("Drag Night", PAGE)])

    assert result.images == []
    assert fetcher.pages_read() == []


async def test_robots_txt_that_names_us_decides_for_us_and_other_paths_stay_open() -> None:
    text = "User-agent: tg-discovery\nDisallow: /private/\n\nUser-agent: *\nDisallow: /\n"
    fetcher = FakeFetcher(
        {
            "https://events.example/robots.txt": FetchedPage(url="x", status=200, text=text),
            PAGE: _page(PAGE, "https://cdn.example/a.jpg"),
        }
    )

    (result,) = await _finder(fetcher).attach([_draft("Drag Night", PAGE)])

    assert [image.url for image in result.images] == ["https://cdn.example/a.jpg"]


async def test_a_site_without_a_robots_file_is_open_and_one_that_cannot_be_asked_is_left_alone() -> None:
    open_site = FakeFetcher({PAGE: _page(PAGE, "https://cdn.example/a.jpg")})  # robots.txt answers 404
    (opened,) = await _finder(open_site).attach([_draft("Drag Night", PAGE)])
    assert len(opened.images) == 1

    for broken in (FetchedPage(url="x", status=503), FetchedPage(url="x", status=500), None):
        fetcher = FakeFetcher(
            {"https://events.example/robots.txt": broken, PAGE: _page(PAGE, "https://cdn.example/a.jpg")}
        )
        (result,) = await _finder(fetcher).attach([_draft("Drag Night", PAGE)])
        assert result.images == []
        assert fetcher.pages_read() == []


async def test_robots_txt_is_read_once_per_site() -> None:
    first, second = "https://events.example/e/one", "https://events.example/e/two"
    fetcher = FakeFetcher(
        {first: _page(first, "https://cdn.example/1.jpg"), second: _page(second, "https://cdn.example/2.jpg")}
    )

    await _finder(fetcher).attach([_draft("One", first), _draft("Two", second)])

    assert fetcher.calls.count("https://events.example/robots.txt") == 1
    assert sorted(fetcher.pages_read()) == [first, second]


async def test_social_networks_and_google_are_never_visited() -> None:
    urls = [
        "https://www.instagram.com/p/abc/",
        "https://www.facebook.com/events/123",
        "https://x.com/someone/status/1",
        "https://www.google.com/search?q=drag+shows",
        "https://youtu.be/abc",
    ]
    fetcher = FakeFetcher()

    results = await _finder(fetcher).attach([_draft(f"Event {n}", url) for n, url in enumerate(urls)])

    assert all(result.images == [] for result in results)
    assert fetcher.calls == []


async def test_a_page_asked_for_twice_is_read_once_even_when_the_questions_overlap() -> None:
    fetcher = FakeFetcher({PAGE: _page(PAGE, "https://cdn.example/a.jpg")}, delay_s=0.05)
    finder = _finder(fetcher)

    first, second = await asyncio.gather(finder.pictures_of(PAGE), finder.pictures_of(PAGE))
    third = await finder.pictures_of(PAGE)

    assert first == second == third
    assert fetcher.pages_read() == [PAGE]


async def test_a_page_that_cannot_be_read_costs_the_event_nothing_and_never_raises() -> None:
    cases: list[FetchedPage | Exception | None] = [
        None,
        FetchedPage(url=PAGE, status=404),
        FetchedPage(url=PAGE, status=500, content_type="text/html", text="<img src='https://cdn.example/a.jpg'>"),
        FetchedPage(url=PAGE, status=200, content_type="text/html", text=""),
        FetchedPage(url=PAGE, status=200, content_type="image/png"),
        RuntimeError("the parser blew up"),
    ]
    for answer in cases:
        fetcher = FakeFetcher({PAGE: answer})
        draft = _draft("Drag Night", PAGE, images=["https://cdn.example/kept.jpg"])

        (result,) = await _finder(fetcher).attach([draft])

        assert [image.url for image in result.images] == ["https://cdn.example/kept.jpg"]


async def test_the_number_of_pages_read_in_a_run_is_capped() -> None:
    urls = [f"https://site{n}.example/e/{n}" for n in range(5)]
    fetcher = FakeFetcher({url: _page(url, f"https://cdn.example/{n}.jpg") for n, url in enumerate(urls)})

    results = await _finder(fetcher, max_pages=2).attach([_draft(f"Event {n}", url) for n, url in enumerate(urls)])

    assert sum(1 for result in results if result.images) == 2
    assert len(fetcher.pages_read()) == 2


async def test_one_request_at_a_time_per_site_and_a_few_sites_at_once() -> None:
    same = [f"https://busy.example/e/{n}" for n in range(4)]
    others = [f"https://site{n}.example/e/{n}" for n in range(6)]
    pages = {url: _page(url, f"https://cdn.example/{n}.jpg") for n, url in enumerate([*same, *others])}
    fetcher = FakeFetcher(pages, delay_s=0.02)

    await _finder(fetcher, concurrency=3).attach([_draft(f"Event {n}", url) for n, url in enumerate([*same, *others])])

    assert fetcher.peak_per_host["busy.example"] == 1
    assert fetcher.peak_total <= 3
    assert fetcher.peak_total >= 2  # different sites really do run side by side


async def test_the_images_are_plain_event_images_with_a_source_and_an_alt_text() -> None:
    fetcher = FakeFetcher({PAGE: _page(PAGE, "https://cdn.example/a.jpg")})

    (result,) = await _finder(fetcher).attach([_draft("Drag Night", PAGE)])

    assert result.images == [EventImage(url="https://cdn.example/a.jpg", source_url=PAGE, alt="alt of a.jpg")]
