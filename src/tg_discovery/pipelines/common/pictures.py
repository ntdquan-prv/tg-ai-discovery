"""Finding pictures for events by reading the pages they were found on.

An event gets the pictures of the pages that are about it alone (a page that names several events says nothing about
whose picture is whose). Each such page is fetched once per run, politely: ``robots.txt`` is honoured (a site that
cannot be asked is left alone), one request at a time per site, a few sites at once, and social networks and Google are
never visited. Only the picture addresses are kept; nothing is downloaded or stored. A page that cannot be read costs
the event nothing: it keeps the pictures it already had, and a run never fails over a picture.
"""

import asyncio
from collections import Counter
from collections.abc import Callable, Sequence
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import structlog

from tg_discovery.domain.models import EventDraft, EventImage, union_images
from tg_discovery.domain.page_images import PageImage, find_pictures
from tg_discovery.domain.urls import registrable_domain
from tg_discovery.providers.fetch.page_fetch import FetchedPage, PageFetcher

log = structlog.get_logger()

ROBOTS_AGENT = "tg-discovery"  # the name a site's robots.txt addresses us by
Rule = Callable[[str], bool]  # whether a site's robots.txt lets us read a URL
# Social networks are out of scope for v1 and their pages need a login; Google pages are never a source of pictures.
SKIPPED_DOMAINS = frozenset(
    {
        "facebook.com",
        "instagram.com",
        "x.com",
        "twitter.com",
        "tiktok.com",
        "threads.net",
        "youtube.com",
        "youtu.be",
        "google.com",
    }
)


class PictureFinder:
    """Reads event pages for pictures. One instance serves one run: it remembers what it already read."""

    def __init__(
        self, fetcher: PageFetcher, *, concurrency: int = 4, host_pause_s: float = 0.5, max_pages: int = 300
    ) -> None:
        self._fetcher = fetcher
        self._slots = asyncio.Semaphore(concurrency)
        self._host_pause_s = host_pause_s
        self._max_pages = max_pages
        self._fetched = 0
        self._hosts: dict[str, asyncio.Lock] = {}
        self._robots: dict[str, asyncio.Task[Rule]] = {}
        self._pages: dict[str, asyncio.Task[list[PageImage]]] = {}

    async def attach(self, drafts: Sequence[EventDraft]) -> list[EventDraft]:
        """The drafts with the pictures of the pages that are about each one alone added to what they have."""
        cited = Counter(source.url for draft in drafts for source in draft.sources)
        alone = sorted(url for url, count in cited.items() if count == 1)
        found = dict(zip(alone, await asyncio.gather(*(self.pictures_of(url) for url in alone)), strict=True))
        result: list[EventDraft] = []
        for draft in drafts:
            added = [
                EventImage(url=image.url, source_url=source.url, alt=image.alt)
                for source in draft.sources
                for image in found.get(source.url, [])
            ]
            images = union_images(draft.images, added)
            result.append(draft.model_copy(update={"images": images}) if images != draft.images else draft)
        return result

    async def pictures_of(self, url: str) -> list[PageImage]:
        """The pictures of one page; the same page asked for twice is read once."""
        task = self._pages.get(url)
        if task is None:
            task = asyncio.ensure_future(self._read(url))
            self._pages[url] = task
        return await asyncio.shield(task)  # a caller that gives up must not cancel the read the others wait for

    async def _read(self, url: str) -> list[PageImage]:
        if self._fetched >= self._max_pages or registrable_domain(url) in SKIPPED_DOMAINS:
            return []
        try:
            if not await self._allowed(url) or self._fetched >= self._max_pages:
                return []
            self._fetched += 1  # checked and counted with no wait in between, so pages read together cannot overshoot
            page = await self._fetch(url)
            if page is None or not 200 <= page.status < 300 or not page.text:
                return []
            return await asyncio.to_thread(find_pictures, page.text, page.url)
        except Exception as error:  # a picture is never worth failing a run for
            log.warning("picture lookup failed", url=url[:200], error=str(error)[:200])
            return []

    async def _fetch(self, url: str) -> FetchedPage | None:
        """One page, politely: one request at a time per site, a pause after it, a few sites at once."""
        lock = self._hosts.setdefault(urlsplit(url).netloc, asyncio.Lock())
        async with lock, self._slots:
            page = await self._fetcher.fetch(url)
            await asyncio.sleep(self._host_pause_s)
        return page

    async def _allowed(self, url: str) -> bool:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        task = self._robots.get(origin)
        if task is None:
            task = asyncio.ensure_future(self._read_robots(origin))
            self._robots[origin] = task
        rule = await asyncio.shield(task)
        return rule(url)

    async def _read_robots(self, origin: str) -> Rule:
        """The site's ``robots.txt``: no file means no rules, and a site that cannot be asked is left alone."""
        page = await self._fetch(f"{origin}/robots.txt")
        if page is None or page.status >= 500:
            return lambda _url: False
        if page.status >= 400:
            return lambda _url: True
        parser = RobotFileParser()
        parser.parse(page.text.splitlines())
        return lambda url: parser.can_fetch(ROBOTS_AGENT, url)
