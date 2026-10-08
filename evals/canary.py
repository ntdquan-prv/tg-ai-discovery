"""Freshness probe: did a provider's results contain an event announced in the last few days?

The probe runs the event queries at fixed checkpoints (+0 h, +6 h, +24 h, +48 h after the owner supplies the list) and
records, per provider and canary event, whether it was seen. A canary counts as seen when a result is the canary's own
page, links to it, or carries its title.
"""

from collections.abc import Sequence

from evals.models import CanaryEvent
from tg_discovery.domain.fingerprint import core_title
from tg_discovery.domain.urls import url_identity
from tg_discovery.providers.base import SearchHit

CANARY_FRESHNESS_DAYS = 7  # the canaries were announced within 3 days; the window leaves room for provider rounding
# The probe is repeated at four checkpoints, so it asks only the queries that look for what was announced lately
# ("this month", "this weekend", "this month and year", "calendar") instead of the whole plan.
CANARY_QUERY_IDS = ("e01", "e02", "e05", "e09")
MIN_TITLE_WORDS = 2  # a one-word title ("Pride") would match half the internet


def canary_match(canary: CanaryEvent, hit: SearchHit) -> str | None:
    """The URL that shows the canary was seen in this hit, or ``None`` when the hit is about something else."""
    wanted = url_identity(canary.url)
    if wanted is not None:
        if url_identity(hit.url) == wanted:
            return hit.url
        if wanted in " ".join(hit.content.split()).replace("https://", "").replace("http://", ""):
            return hit.url
    title_words = core_title(canary.name).split()
    if len(title_words) >= MIN_TITLE_WORDS:
        haystack = f" {core_title(f'{hit.title or ""} {hit.content}')} "
        if f" {' '.join(title_words)} " in haystack:
            return hit.url
    return None


def first_match(canary: CanaryEvent, hits: Sequence[SearchHit]) -> str | None:
    return next((url for hit in hits if (url := canary_match(canary, hit)) is not None), None)
