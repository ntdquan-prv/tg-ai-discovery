from datetime import UTC, datetime

import pytest

from evals.canary import canary_match, first_match
from evals.models import CanaryEvent
from tg_discovery.providers.base import SearchHit

CANARY = CanaryEvent(
    id="c1",
    city="testville",
    name="Neon Jungle Pride Party 2026",
    url="https://promoter.test/events/neon-jungle?utm_source=ig",
    announced_at=datetime(2026, 10, 6, tzinfo=UTC),
)


@pytest.mark.parametrize(
    ("hit", "expected"),
    [
        (
            SearchHit(url="https://www.promoter.test/events/neon-jungle/"),
            "https://www.promoter.test/events/neon-jungle/",
        ),
        (
            SearchHit(
                url="https://listing.test/x", content="Tickets at https://promoter.test/events/neon-jungle today"
            ),
            "https://listing.test/x",
        ),
        (SearchHit(url="https://listing.test/y", title="NEON JUNGLE Pride Party - tickets"), "https://listing.test/y"),
        (
            SearchHit(url="https://listing.test/z", content="Join the neon jungle pride party this weekend"),
            "https://listing.test/z",
        ),
        (SearchHit(url="https://promoter.test/events/other", title="Another night"), None),
        (SearchHit(url="https://listing.test/w", content="A jungle party with neon lights"), None),
    ],
)
def test_canary_match(hit: SearchHit, expected: str | None) -> None:
    assert canary_match(CANARY, hit) == expected


def test_a_one_word_title_is_too_weak_to_match_on() -> None:
    canary = CanaryEvent(
        id="c2", city="x", name="Pride", url="https://p.test/e", announced_at=datetime(2026, 10, 6, tzinfo=UTC)
    )

    assert canary_match(canary, SearchHit(url="https://other.test", title="Pride month")) is None


def test_first_match_returns_the_first_hit_that_shows_the_canary() -> None:
    hits = [
        SearchHit(url="https://a.test", title="Unrelated"),
        SearchHit(url="https://b.test", title="Neon Jungle Pride Party"),
        SearchHit(url="https://c.test", title="Neon Jungle Pride Party again"),
    ]

    assert first_match(CANARY, hits) == "https://b.test"
    assert first_match(CANARY, hits[:1]) is None
