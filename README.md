# tg-discovery

Standalone FastAPI + Postgres service that helps TravelGay admins find new **venues** (by TravelGay venue type) and
LGBTQ+ **events** in a chosen area, score them, drop the junk and queue the uncertain ones as review packets for human
approval. Nothing is published automatically and nothing is written to the TravelGay MySQL. The working rules are in
[CLAUDE.md](CLAUDE.md) and the API guide and design notes in [docs/](#documentation).

## How a run works

1. An admin (or the CMS on a schedule) posts a country and up to 10 cities to `POST /api/v1/events/runs` or
   `/venues/runs`. The answer comes at once with a `run_id`; the run continues in the background.
2. For each city the service asks its providers: Exa (web search with page text) for both flows, SerpApi for Google's
   events pack (events) and Google Maps (venues), and OpenStreetMap Overpass for places tagged `lgbtq` (venues).
   Cities without coordinates are located once with Nominatim.
3. gpt-oss-120b on AWS Bedrock reads the pages Exa returned and extracts events or venues into a fixed schema; the
   LGBTQ+ level must be backed by a quote that is checked against the page.
4. The results of all sources are merged (one row per real event or venue), scored, and stored in Postgres as
   candidates: junk is dropped, the rest waits for review.
5. Admins read the review table and the review packet of each candidate and approve or reject it. Approving only writes
   a row to `publish_outbox`.

## Requirements

- [uv](https://docs.astral.sh/uv/) (it downloads CPython 3.12 on demand)
- Docker with Compose, for Postgres and the container image

## Quick start

```
uv sync
copy .env.sample .env                    # then adjust; every provider key is optional
docker compose up -d --wait db           # Postgres 18 on localhost:5433
uv run alembic upgrade head
uv run uvicorn tg_discovery.main:create_app --factory --reload
```

Open **http://localhost:8000/docs** for Swagger: `POST /api/v1/events/runs` (or `/venues/runs`) is the "Discovery" button,
`GET /api/v1/runs/{run_id}/results` the table it fills and `GET /api/v1/events/candidates/{id}` the review packet. The
contract and what is not collected yet are in [docs/api.md](docs/api.md). `GET /api/v1/health` reports the version and
whether the database answers (503 when it does not). The whole stack, including migrations, also runs with
`docker compose up -d --build api`.

A run needs the keys of the providers it uses in `.env`, and `ACCEPTED_TERMS` must name the providers whose vendor terms
you accept (SerpApi is Google-derived data). Every run is capped by `BUDGET_USD_PER_RUN`, and an identical request is
answered by the earlier run instead of searching again.

## Checks

```
uv run poe check     # ruff, ruff format --check, pyright (strict), pytest
```

Integration tests need `docker compose up -d --wait db` and are skipped when Postgres is unreachable.

## Status

Built: event and venue runs with the real providers, merge and scoring, the review table, review packets, approve and
reject (outbox only), venue types, event picture URLs, organiser contacts and per-step timing in run stats.

Not built yet: SSE progress, the verify step (fetch the official page), dedupe against TravelGay's own records,
edit-before-approve, bulk actions, a run queue with shared rate limits, the publisher, venue pictures and topics.
Schedules are owned by the CMS, which calls this API.

## Documentation

| File | What it is |
|---|---|
| [docs/api.md](docs/api.md) | The API for the people who build the admin screens: flow, fields, what is not collected yet |
| [docs/diagrams.html](docs/diagrams.html), [docs/process-flow.png](docs/process-flow.png) | The process flow of one discovery run |
| [docs/dedupe-report.html](docs/dedupe-report.html) | How duplicates are recognised, the known gaps and the planned fixes |
| [docs/server-load-risk-report.txt](docs/server-load-risk-report.txt) | Point-in-time review (2026-10-12) of what breaks when many runs start at once |
| [docs/decisions/](docs/decisions/README.md) | Architecture decision records |
