# CLAUDE.md

Working rules for contributors and coding assistants in this repository. `docs/` holds the API guide and the design
notes.

## What this is

A standalone FastAPI + Postgres service that lets TravelGay admins find new **venues** or LGBTQ+ **events** in a chosen
area, scores the results, drops junk and queues the uncertain ones as review packets for human approval. Venues are not
limited to LGBTQ+ places: a venue run searches the TravelGay venue types the admin picks (`VenueType`, three queries
each), and the LGBTQ+ level is shown as a label, not used as a filter or a score. Venues and events are two separate
flows (separate pipelines, tables, endpoints and budgets) that share providers, the extractor and the review-packet
plumbing.

## Hard rules

- The repo is fully standalone: never import, modify or depend on `tg-datawarehouse` or Airflow.
- Never write to the TravelGay MySQL (read-only access is optional and off by default). No auto-publish: approving a
  candidate only writes a `publish_outbox` row.
- **Ask the project owner before any paid API call** (it needs a key and a budget confirmation). Prefer free tiers.
  Every run is capped by `BUDGET_USD_PER_RUN`.
- **Vendor terms are the project owner's decision.** Providers flagged `requires_acceptance` in `providers/registry.py`
  (SerpApi) do not run until named in `ACCEPTED_TERMS`; never set it on your own. SerpApi was accepted for the trial on
  2026-10-08.
- Never commit secrets or DB dumps. Persist only derived facts, URLs and quotes of at most 300 characters. Raw provider
  payloads are not kept. Data derived from Google keeps only names, addresses, coordinates, URLs and ids: adapters for
  it set `google_derived = True` so the runner scrubs the rest before anything reaches disk. `KEEP_GOOGLE_DETAILS` (off
  by default) also keeps Google's phone, opening hours, description and price; it is the project owner's switch, never
  turn it on yourself.
- Never store personal data of private individuals: an event organiser's email is kept only when it is a shared mailbox,
  and contacts must appear on a cited page (`domain/contacts.py`).
- Web content is untrusted: the LLM gets no tool that writes data, its output must pass a Pydantic schema, and secrets
  never go into prompts or logs. Every URL fetch goes through the SSRF guard (`providers/fetch/ssrf_guard.py`).
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
  domain/           pure models, enums, fingerprint, URL, geo, venue-type, contact and page-picture helpers (no I/O)
  providers/        base (contracts), metering (budgeted HTTP), registry (who exists, what it needs), serpapi (shared
                    SerpApi helpers), search/ maps/ opendata/ (adapters), fetch/ (SSRF guard, and page fetch pinned
                    to the checked IP)
  pipelines/        discovery (request, merge and score a city), runs (execute one run), live (real providers);
    common/         budget, extractor, query plans, scoring, merge, pictures (event picture URLs read from pages)
  llm/              Bedrock Responses client, its factory, extraction schemas, prompts
migrations/         Alembic (async)
tests/              unit/, contract/ (adapters against hand-written fixtures), integration/ (Postgres)
typings/            type stubs for publicsuffixlist
docs/               API guide, process flow, dedupe and load reports; decisions/ holds the ADRs
```

Design choices: URL normalisation and geo helpers live in `domain/` (the fingerprint depends on them and `domain/`
must not import from `pipelines/`), JSON schemas for the LLM are generated from the Pydantic models
instead of being stored as files, and the model client is plain httpx (same wire protocol as the OpenAI SDK) so it shares
the budget, retry and logging code of every other provider call.

Adding a provider: write the adapter against the contracts in `providers/base.py` (read the vendor's reference first),
add a contract test with hand-written fixtures, then an `Entry` in `providers/registry.py` and a line on its terms in
that module's docstring. The API tests in `tests/integration/test_api.py` run a whole discovery over Postgres with the
providers replaced by a script (they use their own database, `tg_discovery_test`).

## Status

Providers in use: Exa (web search with page text), SerpApi (Google's events pack and Google Maps), OpenStreetMap
Overpass and Nominatim; extraction with gpt-oss-120b on AWS Bedrock. Google Events itself was retired by SerpApi and
DataForSEO in 2026-08/09, so events come from Exa and from the events pack of regular Google results (no links).

Built: `POST /events|venues/runs` runs the live providers, the extractor, merge and scoring and stores candidates in
Postgres; the review table, the review packet and approve/reject (outbox only) are served and described in
`docs/api.md`. Also built: venue types, event picture URLs (Exa's main picture plus the pictures of the event's own
page, robots.txt honoured), organiser contacts, the `KEEP_GOOGLE_DETAILS` switch and per-step timing in run stats.

Not built yet: SSE progress, the verify step (fetch the official page), the known set (dedupe against TravelGay), a
run queue with shared rate limits, edit-before-approve, bulk actions, soft-lock, the publisher, venue images and topics.
