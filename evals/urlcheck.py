"""URL liveness for the ``url_alive_rate`` metric: probe a sample of each provider's own URLs once."""

import asyncio
import random
from collections.abc import Callable
from datetime import UTC, datetime

from evals import store
from evals.data import RunData
from evals.metrics import own_urls
from evals.models import UrlCheck
from evals.store import RunDir
from tg_discovery.providers.fetch.httpx_fetch import UrlProber

DEFAULT_PER_PROVIDER = 100


def urls_to_check(data: RunData, *, per_provider: int, seed: int) -> list[str]:
    """A seeded sample of each provider's own URLs that has not been probed yet, without duplicates."""
    rng = random.Random(seed)
    done = set(data.url_checks)
    by_provider: dict[tuple[str, str], set[str]] = {}
    for row in data.drafts:
        draft = row.venue or row.event
        if draft is not None:
            by_provider.setdefault((row.flow.value, row.provider), set()).update(
                url for url in own_urls(draft) if url not in done
            )
    chosen: dict[str, None] = {}
    for key in sorted(by_provider):
        pool = sorted(by_provider[key])
        for url in rng.sample(pool, min(per_provider, len(pool))):
            chosen[url] = None
    return list(chosen)


async def check_urls(
    data: RunData,
    run_dir: RunDir,
    prober: UrlProber,
    *,
    per_provider: int = DEFAULT_PER_PROVIDER,
    seed: int = 7,
    concurrency: int = 8,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> int:
    """Probe the sample and append the verdicts to the run; returns how many URLs were probed."""
    urls = urls_to_check(data, per_provider=per_provider, seed=seed)
    slots = asyncio.Semaphore(concurrency)

    async def probe(url: str) -> UrlCheck:
        async with slots:
            result = await prober.probe(url)
        return UrlCheck(
            url=url, status=result.status, http_status=result.http_status, reason=result.reason, checked_at=clock()
        )

    for check in await asyncio.gather(*(probe(url) for url in urls)):
        run_dir.append(store.URL_CHECKS, check)
    return len(urls)
