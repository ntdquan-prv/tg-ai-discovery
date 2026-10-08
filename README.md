# tg-discovery

Standalone FastAPI + Postgres service that helps TravelGay admins find new LGBTQ+ **venues** and **events** in a chosen
area, score them, drop the junk and queue the uncertain ones as review packets for human approval. Nothing is published
automatically and nothing is written to the TravelGay MySQL.

The design lives in [tg-discovery-repo-guide.md](tg-discovery-repo-guide.md); working rules for contributors and
Claude Code sessions are in [CLAUDE.md](CLAUDE.md).

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

## Bake-off (milestone M1)

Provider selection is a measured bake-off, not a guess. The harness in [evals/](evals/README.md) sends the same query
plan to every shortlisted provider and scores them on realtime, price, coverage and completeness.

```
uv run python -m evals.run run --dry-run      # simulated end to end, free: writes results/dry-run/report.md
uv run python -m evals.run plan               # what a real run would cost; sends nothing
```

A real run never starts without a budget and `--yes`, and providers whose terms need the owner's review do not run until
they are named in `--accept-terms`. [evals/README.md](evals/README.md) is the step-by-step guide to the pilot.

## Status

| Milestone | State |
|---|---|
| M0 scaffold | done |
| M1 bake-off harness | built and tested; real runs for New York answered on 2026-10-08 (see `evals/README.md`); the full pilot still waits for golden sets and admin labels |
| M2/M3 discovery API (first slice) | runs, candidates, review packets, approve and reject over Postgres, with the real providers; not yet: SSE progress, verify step, known-set dedupe, edit-before-approve |
| M4, M5 | daily watches, review ergonomics and the publisher come next |
