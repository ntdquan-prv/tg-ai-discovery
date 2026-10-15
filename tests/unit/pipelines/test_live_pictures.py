"""The live collector hands the events an extraction found to the picture finder, and only those."""

from collections.abc import Sequence
from typing import cast

from tests.pipeline_builders import CITY, NOW, TODAY, event
from tests.unit.pipelines.test_pictures import FakeFetcher, _page  # pyright: ignore[reportPrivateUsage]
from tg_discovery.domain.enums import Flow
from tg_discovery.domain.models import SourceRef
from tg_discovery.pipelines.common.extractor import Extraction, ExtractionContext, ExtractionStats
from tg_discovery.pipelines.common.pictures import PictureFinder
from tg_discovery.pipelines.live import LiveCollector
from tg_discovery.providers.base import ProviderCall, SearchHit, SearchProvider, SearchQuery, SearchResult

PAGE = "https://events.example/e/drag-night"


class FakeSearch:
    """A search provider that answers every query with one page."""

    flows = frozenset({Flow.EVENT})

    def __init__(self, name: str, *, google_derived: bool = False) -> None:
        self.name = name
        self.google_derived = google_derived

    async def search(self, query: SearchQuery) -> SearchResult:
        call = ProviderCall(
            provider=self.name, endpoint="search", units=1, cost_usd=0, latency_ms=1, status="ok", at=NOW
        )
        return SearchResult(
            provider=self.name, hits=[SearchHit(url=PAGE, content="Drag Night")], call=call, requested=10
        )


class FakeExtractor:
    """Finds the same event on the page every time."""

    model = "fake"

    async def extract(self, hits: Sequence[SearchHit], ctx: ExtractionContext) -> Extraction:
        draft = event("Drag Night").model_copy(
            update={"sources": [SourceRef(url=PAGE, provider=ctx.provider, retrieved_at=NOW)]}
        )
        return Extraction(events=[draft], stats=ExtractionStats())


def _collector(provider: FakeSearch, finder: PictureFinder | None) -> LiveCollector:
    return LiveCollector(
        search_providers=[cast(SearchProvider, provider)],
        venue_providers=[],
        extractor=FakeExtractor(),
        clock=lambda: NOW,
        pictures=finder,
    )


def _finder(fetcher: FakeFetcher) -> PictureFinder:
    return PictureFinder(fetcher, host_pause_s=0)


async def test_the_events_a_provider_found_get_the_pictures_of_their_page() -> None:
    fetcher = FakeFetcher({PAGE: _page(PAGE, "https://cdn.example/a.jpg", "https://cdn.example/b.jpg")})

    batch = await _collector(FakeSearch("exa"), _finder(fetcher)).collect(Flow.EVENT, CITY, window_days=60, today=TODAY)

    assert batch.events
    assert all(
        [image.url for image in draft.images] == ["https://cdn.example/a.jpg", "https://cdn.example/b.jpg"]
        for _, draft in batch.events
    )
    assert fetcher.pages_read() == [PAGE]  # twelve searches found it, one read


async def test_the_collector_notes_how_long_each_kind_of_call_took() -> None:
    fetcher = FakeFetcher({PAGE: _page(PAGE, "https://cdn.example/a.jpg")}, delay_s=0.01)

    batch = await _collector(FakeSearch("exa"), _finder(fetcher)).collect(Flow.EVENT, CITY, window_days=60, today=TODAY)

    timings = batch.stats.timings
    assert (timings["search:exa"].calls, timings["extract"].calls) == (12, 12)  # one search and one reading per query
    assert timings["pictures"].calls == 12  # asked after every reading, answered from memory after the first
    assert all(timing.total_s >= timing.slowest_s >= 0 for timing in timings.values())
    assert list(batch.stats.busy()) == sorted(timings, key=lambda kind: -timings[kind].total_s)


async def test_a_failing_search_is_timed_too() -> None:
    class Failing(FakeSearch):
        async def search(self, query: SearchQuery) -> SearchResult:
            raise RuntimeError("the vendor is down")

    batch = await _collector(Failing("exa"), None).collect(Flow.EVENT, CITY, window_days=60, today=TODAY)

    assert batch.stats.failed_calls == 12
    assert batch.stats.timings["search:exa"].calls == 12


async def test_google_derived_events_never_cause_a_page_to_be_read() -> None:
    fetcher = FakeFetcher({PAGE: _page(PAGE, "https://cdn.example/a.jpg")})

    batch = await _collector(FakeSearch("serpapi_events", google_derived=True), _finder(fetcher)).collect(
        Flow.EVENT, CITY, window_days=60, today=TODAY
    )

    assert batch.events
    assert all(draft.images == [] for _, draft in batch.events)
    assert fetcher.calls == []


async def test_without_a_picture_finder_events_keep_what_the_search_gave() -> None:
    batch = await _collector(FakeSearch("exa"), None).collect(Flow.EVENT, CITY, window_days=60, today=TODAY)

    assert batch.events
    assert all(draft.images == [] for _, draft in batch.events)
