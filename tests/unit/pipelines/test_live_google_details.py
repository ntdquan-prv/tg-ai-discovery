"""The live collector scrubs Google-derived drafts, and keeps their details only when the owner's switch says so."""

from typing import cast

import pytest

from tests.pipeline_builders import CITY, NOW, TODAY
from tg_discovery.domain.enums import Flow
from tg_discovery.domain.models import VenueDraft
from tg_discovery.pipelines.live import LiveCollector
from tg_discovery.providers.base import ProviderCall, VenueProvider, VenueQuery, VenueResult

PLACE = VenueDraft(
    name="Bar Example",
    address="Soi 4",
    phone="+66 2 000 0001",
    opening_hours="Monday 8 PM-2 AM",
    description="Small gay bar.",
    price_text="$$",
    socials={"instagram": "https://instagram.com/bar"},
)


class FakePlaces:
    """A map provider that answers every query with the same place."""

    queryless = False

    def __init__(self, name: str, *, google_derived: bool) -> None:
        self.name = name
        self.google_derived = google_derived

    async def find_venues(self, query: VenueQuery) -> VenueResult:
        call = ProviderCall(
            provider=self.name, endpoint="places", units=1, cost_usd=0, latency_ms=1, status="ok", at=NOW
        )
        return VenueResult(provider=self.name, venues=[PLACE], call=call, requested=20)


async def _venues(provider: FakePlaces, *, keep_google_details: bool) -> list[VenueDraft]:
    collector = LiveCollector(
        search_providers=[],
        venue_providers=[cast(VenueProvider, provider)],
        extractor=None,
        clock=lambda: NOW,
        keep_google_details=keep_google_details,
    )
    batch = await collector.collect(Flow.VENUE, CITY, window_days=60, today=TODAY)
    assert batch.venues
    return [draft for _, draft in batch.venues]


def _details(draft: VenueDraft) -> tuple[str | None, ...]:
    return draft.phone, draft.opening_hours, draft.description, draft.price_text


async def test_google_details_are_dropped_by_default() -> None:
    venues = await _venues(FakePlaces("serpapi_maps", google_derived=True), keep_google_details=False)

    assert all(_details(draft) == (None,) * 4 for draft in venues)
    assert all(draft.name == "Bar Example" and draft.address == "Soi 4" for draft in venues)


async def test_the_owners_switch_keeps_the_google_details_but_not_the_socials() -> None:
    venues = await _venues(FakePlaces("serpapi_maps", google_derived=True), keep_google_details=True)

    assert all(_details(draft) == _details(PLACE) for draft in venues)
    assert all(draft.socials == {} for draft in venues)


@pytest.mark.parametrize("keep", [False, True])
async def test_other_sources_are_never_scrubbed(keep: bool) -> None:
    venues = await _venues(FakePlaces("osm_overpass", google_derived=False), keep_google_details=keep)

    assert all(draft == PLACE for draft in venues)
