# Bake-off harness (milestone M1)

Chooses the search stack by measurement instead of by price list: the same query plan goes to every shortlisted
provider, each result is turned into a draft by one shared extractor, and every provider is scored on **realtime,
price, coverage and completeness** (guide sections 3.4 and 6). The output is `report.md`, a pick per flow and a draft of
`docs/decisions/0001-search-stack.md`.

Nothing here publishes anything or writes to the TravelGay MySQL. Raw provider responses are never kept: a run stores
only call logs (counts, URLs, costs), derived drafts (facts, URLs, quotes of at most 300 characters) and verdicts.

## See it work first (free, no keys)

```
uv run python -m evals.run run --dry-run
```

Simulated providers, a simulated extractor and simulated admins run the whole pipeline on the synthetic city files in
[examples/](examples/) and write `results/dry-run/report.md`. The report says **SIMULATED DATA** at the top: it proves
the harness works end to end and shows what the real report looks like; it says nothing about any real vendor.

## What you provide

Put these in `evals/golden/` (git-ignored; the folder is created by you). UTF-8 CSV with a header row; extra columns are
ignored. [examples/](examples/) has a complete synthetic set.

| File | Columns | Notes |
|---|---|---|
| `cities.csv` | `slug, name, country, country_name, timezone` and optionally `languages, aliases, tg_city_id, lat, lng, south, west, north, east` | `country` is ISO alpha-2, `timezone` an IANA name, `languages` and `aliases` are `\|`-separated (`en\|th`). Give a centre (`lat, lng`) or a box, or set `NOMINATIM_USER_AGENT` and the city is looked up once and cached. |
| `venues_golden.csv` | `id, name, city`, optionally `latitude, longitude, website_url, venue_type, address` | The TravelGay venues per city, i.e. the output of the venue SQL in guide section 13.1 as is. `city` must be a slug, a name or an alias from `cities.csv`. |
| `events_golden.csv` | `id, name, city, start_date`, optionally `end_date, venue_name, website_url, booking_url` | The upcoming events: the event SQL of section 13.1 plus a city column (see below), one row per `recurring_dates` entry. |
| `canary.csv` | `id, city, name, url, announced_at` | About ten events announced in the last 72 hours (promoter URLs), for the freshness probe. |

If TravelGay lists fewer than 15 venues or 10 upcoming events for a city, ask the admins to add their own: recall on a
small known set is noisy (the report warns about it).

**Exporting from MySQL.** The venue query of guide section 13.1 exports as is. The guide's event query does not return
the city, which the harness needs: add `c.name AS city` and a join on `cities` (this version has not been run against the
real database):

```sql
SELECT e.id, e.name, e.slug, c.name AS city, e.event_type, e.recurring_type, e.venue_name, e.website_url,
       e.booking_url, rd.start_date, rd.end_date
FROM events e
JOIN cities c ON c.id = e.city_id
JOIN recurring_dates rd ON rd.dateable_id = e.id AND rd.dateable_type = 'Domain\\Events\\Models\\Event'
WHERE e.status = 1 AND e.deleted_at IS NULL AND rd.start_date >= CURDATE() AND e.city_id IN (:city_ids);
```

The `city` of both files must be a slug, a name or an alias from `cities.csv`.

Keys go in `.env` (copy [../.env.sample](../.env.sample)); a provider without its key is skipped and the report says so.

## Providers

| Name | Flows | Needs | Price per call | Before it runs |
|---|---|---|---|---|
| `osm_overpass` | venue | `NOMINATIM_USER_AGENT` | free | the public Overpass server is meant for light use; commercial use should self-host |
| `brave_web`, `brave_llm_context`, `brave_place` | both, both, venue | `BRAVE_API_KEY` | 0.005 | **needs `--accept-terms`**: Brave's terms forbid storing results and benchmarking AI services |
| `exa` | both | `EXA_API_KEY` | 0.007 to 0.017 | read Exa's terms (4.2a) |
| `tavily` | both | `TAVILY_API_KEY` | 0.008 | none found |
| `perplexity_search` | both | `PERPLEXITY_API_KEY` | 0.005 | terms not verified |
| `parallel` | both | `PARALLEL_API_KEY` | 0.001 | **needs `--accept-terms`**: Parallel's terms forbid caching, building databases and sharing benchmarks |
| `serpapi_events`, `serpapi_maps` | event, venue | `SERPAPI_API_KEY` | 0.010 (plans: free 250 a month) | **needs `--accept-terms`**: Google-derived data, guide question 8 |

Extraction needs a model: `AWS_BEDROCK_API_TOKEN` and `AWS_BEDROCK_OPENAI_URL` (see `.env.sample`). Serper and DataForSEO
adapters are not written: SerpApi covers both flows and they would only be cheaper alternatives.

SerpApi's free plan allows 50 searches an hour (250 a month): a full city is 24 searches, so run one city at a time, or
continue a stopped run with `--resume` an hour later.

Google closed its separate Events tab in August 2026, and SerpApi's Google Events API and DataForSEO's Google Events
endpoint were retired with it (the guide still lists both). `serpapi_events` therefore reads the events pack that regular
Google results show for an events query: up to ten events with title, type, date (no year), time and venue, but no link,
price or description, and no date filter. Not every query brings the pack up.

Prices and terms were read on 2026-10-08 and change; the adapter modules say what was checked and what was taken from the
guide. The adapters were written against the vendors' documentation and tested against hand-written fixtures, never
against the live APIs: expect the first live calls to reveal small mismatches, which is why step 2 below is a smoke test.

## Running the pilot

Every command takes `--help`. Nothing is sent by `plan`; a real `run` refuses to start without `--yes`.

1. **Decide first**: the open questions of guide section 14 (keys and budget, cities, who labels, Google-derived data),
   and whether the vendor terms above are acceptable. `--accept-terms brave_web,parallel,...` is how you say so.
2. **Plan and smoke-test**
   ```
   uv run python -m evals.run plan --cities bangkok
   uv run python -m evals.run run --run-id smoke --cities bangkok --providers exa --max-queries 2 --budget-usd 0.5 --yes
   ```
   Read `results/smoke/report.md` (and `calls.jsonl` if a call failed: the error text is there, secrets scrubbed).
3. **Pilot on one city** (guide: free tiers, at most about 50 USD in total; the default budget is 5, the hard cap 50):
   ```
   uv run python -m evals.run run --run-id pilot-001 --cities bangkok --budget-usd 5 --yes
   ```
   If the budget runs out the run says so; continue it with `--resume --budget-usd N` (finished calls are skipped).
4. **Check URLs**: `uv run python -m evals.run check-urls --run results/pilot-001` (HEAD requests behind an SSRF guard).
5. **Admins label** a blind sample: `label-export --run results/pilot-001` writes `labeling_sheet.csv` (no provider
   names) and `labeling_instructions.txt` for the admins and `label_key.csv` for you. When the sheet comes back,
   `label-import --run results/pilot-001 --file filled.csv`. Verdicts: `valid, not_lgbtq, wrong_place,
   past_or_cancelled, duplicate, spam`.
6. **Freshness canary**, at +0 h, +6 h, +24 h and +48 h after you receive the list:
   `canary --run results/pilot-001 --checkpoint-hours 6 --yes` (add `--inputs evals/golden` the first time so the run
   picks up `canary.csv`). It asks four event queries per provider and city and records which canaries each one saw.
7. **Read the report** and copy `adr_draft.md` to `docs/decisions/0001-search-stack.md` after editing it.

`run --replay results/pilot-001` rebuilds the report from what the run stored, with no provider called, whenever labels,
URL checks or canary probes were added.

## How the pick is made

1. Each metric is min-max scaled across the providers measured on it and averaged into its criterion's score; a metric on
   which every provider is equal is left out. Missing measurements (no labels yet, no canary) are skipped, not scored 0.
2. A provider's weighted score uses the per-flow weights of guide section 3.4 (venue: coverage 35, completeness 30, price
   25, realtime 10; event: realtime 35, coverage 30, completeness 20, price 15).
3. Combinations of up to four providers must reach the floors: union recall of the known set (venue 0.75, event 0.60) and
   precision of the novel items 0.60.
4. **The pick is the cheapest combination that clears the floors.** Within 10% of the cheapest, fewer vendors wins, then
   not relying on Google data, then the higher mean weighted score. The report also lists the cost and recall frontier, so
   you can overrule the rule.

Without labels the precision floor cannot be checked and the pick is marked **provisional**.

## What a run folder holds

`run.json` (cities, providers with the prices they ran at, budget), `calls.jsonl`, `llm_calls.jsonl`, `drafts.jsonl`,
`canary.jsonl`, `url_checks.jsonl`, `labels.jsonl`, `labeling_sheet.csv`, `label_key.csv`, `inputs/` (a copy of your CSVs,
so a replay works after you change them), `report.md`, `adr_draft.md`. `results/` is git-ignored.

## Safety rules the harness enforces

- No paid call without `--yes` and a budget; every call reserves its worst-case cost first, so the limit holds even when
  calls run in parallel. The hard cap is 50 USD.
- Providers whose terms need the owner's review do not run until named in `--accept-terms`.
- Adapters for Google-derived data must set `google_derived = True`; the runner then strips ratings, reviews, phone, hours
  and quotes before anything reaches disk.
- URL probes refuse private, loopback and link-local addresses at every redirect.
- The labelling sheet never names a provider and hides source pages that would give one away.
- Credentials are scrubbed from error text and are never written to a run folder (a test checks this).
