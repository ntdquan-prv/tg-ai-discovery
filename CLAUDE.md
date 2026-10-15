# CLAUDE.md

Guidance for Claude Code sessions in this repository. The full design is in
[tg-discovery-repo-guide.md](tg-discovery-repo-guide.md); read its sections 0 to 6 before changing anything.

## What this is

A standalone FastAPI + Postgres service that lets TravelGay admins find new **venues** or LGBTQ+ **events** in a chosen
area, scores the results, drops junk and queues the uncertain ones as review packets for human approval. Since
2026-10-13 venues are not limited to LGBTQ+ places (the owner's call): a venue run searches the TravelGay venue types
the admin picks (`VenueType`, three queries each), and the LGBTQ+ level is assessed as a label, not a filter or a score. Venues and
events are two separate flows (separate pipelines, tables, endpoints and budgets) that share providers, the extractor
and the review-packet plumbing.

## Hard rules

- The repo is fully standalone: never import, modify or depend on `tg-datawarehouse` or Airflow.
- Never write to the TravelGay MySQL (read-only access is optional and off by default). No auto-publish: approving a
  candidate only writes a `publish_outbox` row.
- **Ask the owner before any paid API call** (it needs a key and a budget confirmation). Prefer free tiers. The
  bake-off CLI refuses to spend without a budget and `--yes`.
- **Vendor terms are the owner's call.** Providers flagged `requires_acceptance` in `providers/registry.py` (Brave,
  Parallel, SerpApi) do not run until the owner names them (`--accept-terms` for the bake-off, `ACCEPTED_TERMS` for the
  service); never pass or set either on your own. The owner accepted SerpApi in chat on 2026-10-08, for the trial.
- Never commit secrets, DB dumps or `results/`. Persist only derived facts, URLs and quotes of at most 300 characters.
  Raw provider payloads are not kept (an optional debug cache with a TTL of 24 hours at most, off by default). Data
  derived from Google keeps only `place_id`/`data_id`: adapters for it must set `google_derived = True` so the runner
  scrubs ratings, reviews, photos and hours before anything reaches disk. The one exception is the owner's
  `KEEP_GOOGLE_DETAILS` switch (asked for on 2026-10-13, off by default): when the owner turns it on, Google's phone,
  opening hours, description and price are kept for the POC; never turn it on yourself.
- Web content is untrusted: the LLM gets no tool that writes data, its output must pass a Pydantic schema, and secrets
  never go into prompts or logs. Every URL fetch goes through the SSRF guard.
- Prices and terms of third-party APIs change quickly: read the provider's current official reference before writing or
  changing an adapter, and record the verification date next to the price (`PriceSpec.verified_on`; `None` means it was
  not read on the vendor's page).

## Commands

```
uv sync                                   # create .venv with the pinned dependencies
uv run poe check                          # ruff + format check + pyright (strict) + pytest
uv run poe format                         # apply ruff formatting
docker compose up -d --wait db            # Postgres 18 on localhost:5433 (integration tests use it)
uv run alembic upgrade head               # apply migrations
uv run uvicorn tg_discovery.main:create_app --factory --reload   # Swagger at http://localhost:8000/docs
uv run python -m evals.run run --dry-run  # the whole bake-off, simulated (see evals/README.md)
```

PowerShell 5.1 has no `&&`; use `uv run poe check` instead of chaining commands. It also writes a UTF-8 BOM with
`Set-Content -Encoding utf8`: edit files with the editor tools, or `ruff format` strips the BOM again.

## Code standards

Python 3.12, ruff at 120 columns, pyright strict, Google-style docstrings, KISS and single responsibility, rule of three
before abstracting. Match the surrounding code (naming, comment density). Tests use pytest, pytest-asyncio (auto mode)
and respx; tests that need Postgres carry the `integration` marker and skip when it is unreachable.

## Layout

```
src/tg_discovery/   config, logging, main (app factory), deps
  api/              routers (health, runs, candidates), schemas (the contract), presenters, auth (service token + roles)
  store/            engine, ORM tables, repository (every query and decision)
  domain/           pure models, enums, fingerprint, URL, geo and page-picture helpers (no I/O)
  providers/        base (contracts), metering (budgeted HTTP), registry (who exists, what it needs), search/ maps/
                    opendata/ (adapters), fetch/ (SSRF-guarded: URL probe, and page fetch pinned to the checked IP)
  pipelines/        discovery (request, merge and score a city), runs (execute one run), live (real providers);
    common/         budget, extractor, query plan, scoring, merge, pictures (event picture URLs read from pages)
  llm/              Bedrock Responses client, its factory, extraction schemas, prompts
evals/              bake-off harness: inputs, registry, runner, matching, metrics, decide, labels, report, CLI (run.py)
  examples/         synthetic inputs for the dry run;  golden/ (git-ignored) holds the owner's real inputs
migrations/         Alembic (async)
tests/              unit/, contract/ (adapters and the pilot rehearsal against fixtures), integration/ (Postgres)
docs/decisions/     ADRs; 0001-search-stack.md is written after the pilot (the report drafts it)
```

Deviations from the guide's layout: URL normalisation and geo helpers live in `domain/` (the fingerprint depends on
them and `domain/` must not import from `pipelines/`), JSON schemas for the LLM are generated from the Pydantic models
instead of being stored as files, and the model client is plain httpx (same wire protocol as the OpenAI SDK) so it shares
the budget, retry and logging code of every other provider call.

Adding a provider: write the adapter against the contracts in `providers/base.py` (read the vendor's reference first),
add a contract test with hand-written fixtures, then an `Entry` in `providers/registry.py` with its terms note. The pilot
rehearsal in `tests/contract/test_pilot_rehearsal.py` shows the whole path over a mocked network; the API tests in
`tests/integration/test_api.py` run a whole discovery over Postgres with the providers replaced by a script (they use
their own database, `tg_discovery_test`).

## Status

M0 (scaffold) is done. M1 is built and was exercised for real on 2026-10-08: the harness, seven search adapters (Brave
web and LLM context, Exa, Tavily, Perplexity, Parallel, SerpApi's Google events pack), three structured venue sources
(OSM Overpass, Brave Place, SerpApi Google Maps), the Nominatim geocoder, the shared extractor, labelling, URL checks,
the canary probe and the report. OSM Overpass, Exa, SerpApi and the Bedrock endpoint have answered; Google Events is gone
(SerpApi and DataForSEO retired it in 2026-08/09, the guide still lists it), so events come from the events pack of
regular Google results (no links) and from AI search. The owner asked for the API before the pilot, so a first slice of
M2/M3 exists: `POST /events|venues/runs` runs the live providers, the extractor, merge and scoring and stores candidates
in Postgres; the review table, the review packet and approve/reject (outbox only) are served and described in
`docs/api.md`. Not built yet: SSE progress, the verify step (fetch the official page), the known set (dedupe against
TravelGay), edit-before-approve, bulk, soft-lock, watches and the scheduler, the publisher, venue images and topics
(event picture URLs are collected: Exa's main picture plus the pictures read from the event's own page, robots.txt
honoured). Still
needed from the owner: golden sets and admin labels for the pilot, and the `.env` fixes (Bedrock URL `/v1`, a real
contact in `NOMINATIM_USER_AGENT`, `ACCEPTED_TERMS`). Serper and DataForSEO adapters are not written.
