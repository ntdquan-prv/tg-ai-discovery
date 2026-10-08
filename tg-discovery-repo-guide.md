# TG Discovery — Repo Bootstrap Guide

> **Version** 2026-10-07 · written in a session on the `tg-datawarehouse` repo · **Audience**: a fresh Claude Code session with no prior context, and the project owner.
> **Status**: design only, no code yet. Vendor prices and features change quickly — re-verify before relying on them.
> **Confidence labels**: **[V]** read from official docs/pricing or vendor documentation · **[T]** third-party or vendor self-reported (biased) · **[?]** unverified.

**TL;DR** — Build a standalone FastAPI + Postgres service, `tg-discovery`, that lets ~5–6 TravelGay admins press "Discovery" to find new LGBTQ+ **venues** or **events** in a chosen area (country + cities), score the results, drop the junk, and queue the 50/50 ones as review packets for human approval. Venues and events are **two separate flows**. Pick search providers by a **measured bake-off** (Milestone 1) on four criteria: **realtime, price, coverage, completeness**. No Airflow, no writes to the TravelGay MySQL, no auto-publish.

## Table of contents

0. How to use this document · 1. Goals & scope · 2. Decisions already made · 3. AI search technologies (four criteria) · 4. VENUE flow · 5. EVENT flow · 6. Bake-off (Milestone 1) · 7. Architecture & stack · 8. Repo layout · 9. Data model · 10. API · 11. Configuration · 12. Milestones · 13. Reference material · 14. Open questions & risks · 15. Kick-off prompt · 16. Sources

---

## 0. How to use this document

**For the new session**, in this order:
1. Read §1–§6 (mandatory). §7–§11 are the technical spec. §13 is reference material (TravelGay MySQL schema, how to call Bedrock, etc.).
2. Before writing any code: summarize your understanding, raise the open questions (§14), write a plan for M0–M1 and **wait for the owner's approval**.
3. Hard rules:
   - The repo is **fully standalone**: do not import, modify or depend on `tg-datawarehouse` or Airflow.
   - **Never write** to the TravelGay MySQL. Read-only access is optional.
   - **Ask before making any paid API call** (needs a key + the owner's budget confirmation). Prefer free tiers.
   - Never commit secrets, DB dumps or `results/`. Do not keep raw provider payloads long-term (§3.5).
   - Code standards: Python 3.12, ruff at 120 columns, strict typing (pyright), Google-style docstrings, KISS/SRP, rule-of-three before abstracting.
4. A suggested kick-off prompt is in §15.

## 1. Goals & scope

**Problem.** TravelGay admins (5–6 people) press "Discovery" → AI finds new LGBTQ+ **venues** or **events** inside the selected area (country + cities). Results are scored; junk is dropped; the 50/50 ones become a "review packet" (JSON) stored in Postgres for admin review; only after approval would anything go to the TravelGay MySQL (publishing is a later phase — stub only). Runs happen on demand and daily via "watches" (≤10 cities).

**Two separate flows.** Venues and events differ in nature, sources, cadence and lifecycle. They share infrastructure (providers, extractor, review packet) but have **separate pipelines, tables, endpoints and budgets**.

| | Venue | Event |
|---|---|---|
| Nature | place entity, changes slowly | time-bound, changes fast |
| Criteria priority | coverage > completeness > price > realtime | realtime > coverage > completeness > price |
| Main sources | OSM, map APIs, bulk open data, guides/listicles | Google Events, promoter/venue/org pages, ticket pages, LGBTQ+ media |
| Cadence | first city sweep, then monthly refresh; on-demand runs | daily + on-demand; weekly "source discovery" loop |
| Identity key | `place_id` / OSM / FSQ id › website domain › name + geohash | core title + venue or city (date is not part of the key) |
| Lifecycle | new → review → approved; stale / closed | new → review → approved; expired / cancelled / stale |
| Link | receives "venue hints" from events | references a venue |

**Out of scope for v1**: Airflow/warehouse (not used, not integrated); writing to the TravelGay MySQL (outbox stub only); auto-publish; admin UI; Facebook/Instagram Events (no public API; third-party scrapers violate ToS).

**Load**: 5–6 concurrent admins; ≤10 cities watched daily; target cost ≈ $30–150/month (rough estimate, §3.6).

## 2. Decisions already made

1. New standalone repo `tg-discovery` (runtime, deployment, dependencies and security differ from the data-pipeline repo).
2. Python 3.12 + FastAPI + Postgres (own database) + Alembic.
3. **No Airflow**; enrichment and verification happen inside the service.
4. Two separate pipelines: Venue and Event.
5. Human-in-the-loop: **no auto-publish**; "fast-approve" is only a UI label.
6. Provider-agnostic (thin adapters); providers are chosen by data in M1.
7. Four search-selection criteria: **realtime, price, coverage, completeness**, with default per-flow weights (§3.4).
8. Storage: persist only derived facts + URLs + short quotes (≤300 chars); raw payloads only in a debug cache with TTL ≤24h (off in prod); for Google-derived data keep only `place_id`/`data_id`.
9. "Known set" (dedupe, golden set, country/city vocabulary) = TravelGay MySQL read-only — **optional, off by default** (a `KnownSet` port with a no-op implementation).
10. Single-flight per filter + budget caps per run/day.
11. Rejected → tombstone (no payload kept); keep a 10% payload sample for 14 days to audit false negatives.

## 3. AI search technologies — evaluated on the four criteria

### 3.1 How to read this

- The only independent benchmark found: the **Artificial Analysis Search Index** (data as of 2026-09-28) [V]. It measures general answer quality + cost (tokens + search fees) + latency on three question sets (DeepSearchQA 900, BrowseComp 200, AA-Omniscience 600) with one fixed agent (Stirrup + GPT-5.6 Luna medium, ≤25 turns, ≤10 results/query). It does **not** measure realtime freshness nor LGBTQ+ venue/event discovery → treat it as a prior only.
- Vendor self-reported figures (FreshQA, "61% recall", "Brave vs Google 7.2 vs 6.8") are biased → labelled [T]; do not use them as a basis for the choice.
- **Conclusion: measure it yourself (M1, §6).** The tables below are hypotheses to test.

### 3.2 Search / AI APIs

| Technology | Realtime | Price (list) | Coverage | Completeness & notes |
|---|---|---|---|---|
| **SerpApi** (Google Events / Maps) | live from Google at call time; cached searches are free, `no_cache` forces fresh [V] | Free 250/mo; $25/1k; $75/5k; $150/15k (≈ $10–25/1k); no pay-as-you-go [V] | Google — the broadest; dedicated **events** and **maps** verticals [V] | Events 10/page (`start`); Maps 20/page, ~100 max [V]; structured JSON (title, date, address, link, ticket_info, venue / place_id, gps, rating, hours); event dates **have no year** |
| **DataForSEO** Google Events | live ≤6 s or queue 1–5 min [V] | $0.0006–0.002/query (= $0.6–2/1k) [V] | Google Events | English only [V] |
| **Serper.dev** | live Google | ≈ $1/1k [T]; 2,500 free queries [V] | Google Search/Maps/Places/News…; **no Events endpoint listed** on the official page [V] | structured JSON |
| **Brave** (Search / LLM Context / Place Search) | `freshness` pd/pw/pm/py or a date range (based on the page's date); own index [V] | Search $5/1k (+$5 credit/mo); Place Search flat $5/1k [V] | own index ~30B pages [T]; Place Search ~200M POIs [V] | LLM Context returns pre-extracted snippets (≤32K tokens, `count` ≤50); Place Search returns all fields (rating, hours, phone, photos); 50 QPS [V]; **storing results requires a plan with storage rights** [V] |
| **Exa** | `startPublishedDate`; `maxAgeHours` (0 = fresh crawl); Monitors [V] | instant $4 · auto $7 · deep $12 /1k; contents $1/1k pages; Websets 10 credits/item [V] | own neural index; allow-list ≤1,200 domains [V] | `numResults` ≤100; contents/summary with schema in one call; Websets = entity + verification + enrichment [V]; AA: auto 74 |
| **Tavily** | `time_range` / `start_date`; `topic=news` [V] | 1 credit/search ($0.008 PAYG; $5–7.5/1k on plans) [V] | aggregated; has `country`; domains ≤300 [V] | ≤20 results; `include_raw_content`; Extract/Crawl/Research [V]; AA: basic ≈ 66 [T] |
| **Parallel** | `after_date`; Monitor API [V]; `fetch_policy.max_age_seconds` ≥ 600 [T] | Search $1/1k (fast) · $5/1k (basic/advanced) [V]; FindAll Base $0.25 + $0.03/match … Pro $10 + $1/match [V] | own index; FindAll recall is vendor-reported: Base 30% · Core 52.5% · Pro 61% [T] | 10 results (+$1/1k extra); FindAll (beta) returns entities + citations + enrichment [V]; AA: advanced 75 |
| **Perplexity** (Search API / Sonar) | `search_recency_filter` (Sonar) [V] | Search $5/1k; Fast $1/1k; Sonar $1–3/M tokens + $5–14/1k requests [V] | own index | AA **rank 1**: medium 80 (≈ $70 per 1k tasks, 28 s) [V] |
| **Firecrawl** Search | `tbs` qdr:h/d/w/m/y; live scrape inside the call [V] | 2 credits/10 results + scrape (Hobby $16/5k credits [T]) | web/news/images | returns markdown/JSON per page in one call [V]; AA 73 but slow (62 s) |
| **Octen** | "new content indexed within 5 minutes" [T] | $1/1k [T] | not disclosed | highlights; AA **rank 3**: 77 (≈ $24 per 1k tasks, 16 s) [V]; official docs not reachable [?] |
| **TinyFish** Search/Fetch | — | free (30 req/min, 500/h) [V] | — | AA 71, slow (59 s) |
| **Bedrock Web Search** (GPT-5.x) | Amazon index, **no live-web yet** [V] | per query, price not found [?] | "tens of billions of documents" [V] | citations; no domain allow-list or recency filter [V]; GPT-5.4/5.5/5.6 only, Responses API, us-east-1/us-east-2/us-west-2 |
| **Claude** web_search | has `page_age` | $10/1k + tokens [V] | — | `allowed_domains`, `max_uses`; **not available on Bedrock** [V] |
| **Gemini** grounding (Search/Maps) | Google index | Gemini 3: 5,000 free/mo then $14/1k [V] | Google; Maps ~250M places | **Maps: only `placeId`/`reviewId` may be stored** [V] |

**AA Search Index (2026-09-28)** [V] — score / cost per 1k tasks (includes model tokens; no-search baseline $2.92) / latency: Perplexity medium 80 / $70 / 28 s · Perplexity high 79 / $67 / 29 s · Octen 77 / $24 / 16 s · Parallel advanced 75 / $59 / 42 s · Brave LLM Context 75 / $79 / 23 s · OpenAI web search 74 / $50 / 31 s · You.com 74 / $67 / 20 s · Exa auto 74 / $83 / 32 s · Firecrawl 73 / $43 / 62 s · Parallel basic 73 / $67 / 27 s · TinyFish 71 / $9 / 59 s. (Exa instant ≈ 68 and Tavily basic ≈ 66 per an earlier snapshot.)

### 3.3 Structured place & event data

| Source | Used for | Realtime | Price | Coverage / notes |
|---|---|---|---|---|
| **OSM Overpass** `lgbtq=primary/yes/welcome` | venue | continuously updated by the community | free; ODbL (attribution; share-alike if a derived DB is published) [V] | uneven coverage; must query node + way + relation |
| **FSQ OS Places** | venue (bulk) | refreshed monthly [V] | free, Apache-2.0 [V] | 100M+ POIs; LGBTQ+ category (e.g. "Gay Bar") [?] |
| **Overture Maps Places** | venue (bulk) | per release | free, CDLA-Permissive [V] | 53M+ places; LGBTQ+ category [?] |
| **Google Places API (New)** | venue | live | Text Search Pro $32/1k (5k free/mo) [V] | store `place_id` only [V] |
| **Ticketmaster Discovery** | event | live | free, 5,000 calls/day [V] | mainstream-leaning |
| **PredictHQ** | event | live | free up to 3 locations; paid from ~$500/yr [T] | mainstream-leaning; LGBTQ+ [?] |
| **Eventbrite** | — | — | **public event search removed in 2020** [V] | only by organization/venue/event id |
| **Meetup** (GraphQL) | event | live | access/plan requirements to confirm [?] | — |

### 3.4 Weights & shortlist per flow

Default weights (adjustable): **Venue** coverage 35 · completeness 30 · price 25 · realtime 10. **Event** realtime 35 · coverage 30 · completeness 20 · price 15.

- **Venue — Tier 1**: `osm_overpass`; `brave_place`; `serpapi_maps` (or `serper_maps`); `exa` + `brave` (LLM Context) + `tavily` + `perplexity_search` for guides/listicles/official pages. **Tier 2**: `parallel_findall` (Base/Core), `exa_websets` (Free: 25 items/webset), `fsq_os_places` (bulk), `google_places`, `gemini_grounding` (Maps), `firecrawl_search`.
- **Event — Tier 1**: `serpapi_events` (or `dataforseo_events`); `exa` (`startPublishedDate` + `maxAgeHours`); `brave` (`freshness=pw|pm`); `tavily` (`start_date`/`time_range`); `perplexity_search` (recency); `parallel_search` (`after_date`); plus the **Source Watchlist** (§5). **Tier 2**: `parallel_findall` (entity=event), `ticketmaster`, `predicthq` (free), Exa/Parallel Monitors, `octen`, `bedrock_web_search`, `claude_web_search`.

### 3.5 Licensing & storage (mandatory reading)

- **Google** (Places, Maps, Gemini-Maps): store only `place_id` (+`reviewId`); displaying requires attribution [V].
- **SerpApi / Serper / DataForSEO**: data derived from Google → grey area. Google v. SerpApi is not over: on 2026-07-20 the court dismissed two DMCA claims (one with prejudice, one with leave to amend) [V]. SerpApi offers a "Legal Shield" of up to $2M from the Production plan [V]. Repo policy: store facts (name, address, coordinates, URL) + `place_id`/`data_id`; **do not** store Google ratings/reviews/photos/hours.
- **Brave**: "to store API results in part or whole you must subscribe to a plan that explicitly grants storage rights" [V]. By default use Brave only to *find*, and persist derived facts; ask Brave before storing content.
- **Claude / Gemini**: keep citations/attribution when displaying output [V].
- **OSM** ODbL; **FSQ** Apache-2.0 (keep notices); **Overture** CDLA-Permissive [V].
- **Exa / Tavily / Parallel / Perplexity / Firecrawl / Octen / TinyFish**: caching/storage ToS not verified [?] → read the ToS before enabling in prod.
- **Repo rule**: persist = structured facts + `sources[{url,title,provider,retrieved_at}]` + short quotes ≤300 chars. Raw responses only in a debug cache with TTL ≤24h (`DEBUG_RAW_CACHE=false` by default).

### 3.6 Rough cost (≤10 cities; estimate, re-measure in M1)

- **Events, daily** (per city-day: 12 Google Events searches = 6 queries × 2 pages, 6 AI-search queries, ~20 watchlist pages): Google Events 3,600 searches/mo → SerpApi Developer $75 (or DataForSEO ≈ $2–7); AI-search 1,800 queries → Exa ≈ $13 / Tavily ≈ $14 / Brave ≈ $9 / Parallel fast ≈ $2; watchlist fetch ~6,000 URLs → Tavily Extract ≈ $10 or self-hosted httpx ≈ 0; LLM `gpt-oss-120b` (≈ $0.15/$0.60 per 1M tokens) ~12M in + 2M out ≈ $3. **Events ≈ $20–110/month** (low: DataForSEO + Exa/Brave + httpx; high: SerpApi + Tavily Extract).
- **Venues** (monthly refresh + first sweep): ~300 map calls + ~100 AI-search + ~600 fetches + ~5M tokens ⇒ **< $10/month** steady state; first sweep ≈ $30–40.
- **Total ≈ $30–150/month.** LLM-native search (the agent decides how many searches) can cost more and is harder to predict.

## 4. VENUE flow

**Traits**: slow-changing, place-centric identity, needs an LGBTQ+ level and closure detection. Cadence: first sweep per city, then monthly refresh; admins can run on demand at any time.

**Pipeline**
1. **Plan**: filter (country + city_ids) → city center/bbox (Nominatim or Brave Place `location`) → query set (templates + local-language synonyms generated once per city by a cheap model, cached).
2. **Recall** (parallel): (a) OSM `lgbtq=*`; (b) map APIs (Brave Place, Google Maps via SerpApi/Serper) with many queries ("gay bar", "gay club", "lesbian bar", "gay sauna", "LGBTQ+ friendly café"…) paginated to the ceiling; (c) AI-search for guides/listicles/official pages → extractor pulls venue names; (d) optionally FSQ OS Places filtered by category/bbox.
3. **Normalize** → `VenueDraft` (schema below). A listicle page is never an entity.
4. **Dedupe/Merge**: fingerprint (below), merge multi-source, keep per-field provenance; compare with the known set if enabled.
5. **Verify** (NEW/CHANGED only): fetch the official website → LLM judge: `lgbtq.level` (`primary|friendly|unclear|no`) + evidence quote + still open? + map to a `venue_type` (§13.1); check coordinates inside the bbox, URL liveness.
6. **Score & triage** → persist candidate + sighting (+ revision if changed).

**Fingerprint**: `google_place_id` › `osm:{type}/{id}` › `fsq_id` › website domain (excluding aggregators) › (normalized name + geohash7). Keep all external ids in `external_ids`; two records merge when **any** id matches. Normalized name: casefold, strip diacritics, drop generic words (bar, club, the, sauna…), keep CJK as is. Always paired with `(country, city_id)` to avoid same-name cities.

**`VenueDraft`**
```json
{
  "name": "Example Bar", "venue_type": "Bar",
  "address": "…", "city": "Bangkok", "country": "TH", "lat": 13.74, "lng": 100.53,
  "website": "https://…", "phone": null, "socials": {"instagram": "https://…"},
  "opening_hours": "Daily 20:00–02:00",
  "description": "≤600 chars, our own words",
  "lgbtq": {"level": "primary", "evidence": [{"quote": "≤300 chars", "url": "https://…"}]},
  "operating_status": "open",
  "external_ids": {"google_place_id": "ChIJ…", "osm": "node/123", "fsq": "…"},
  "sources": [{"url": "…", "title": "…", "provider": "brave_place", "retrieved_at": "2026-10-07T00:00:00Z"}],
  "missing_fields": ["phone"]
}
```

**Scoring** (0–1): lgbtq_relevance 0.30 · existence/open 0.20 · location_match 0.15 · completeness 0.15 · source_trust 0.10 · verifiability (≥2 independent sources) 0.10. **Hard gates** → reject: outside the area; `lgbtq.level=no`; not an entity (listicle/spam); permanently closed (tombstone reason `closed`). Thresholds: <0.35 reject (tombstone, TTL 180 days); 0.35–0.75 `needs_review`; ≥0.75 `needs_review` + `fast_approve`. Countries with high legal risk (Equaldex, §14) → `safety_flag` and always `needs_review`.

**Lifecycle**: `new → rejected | needs_review → approved | rejected → (published, later)`; `stale` after 2 consecutive refreshes without sighting; `closed` when sources say it closed. **Cross-flow**: an event emits a `venue_hint` (name, address, city, URL) → upserted into the venue flow (`source_kind=event_hint`, status `new`); the event keeps `venue_candidate_id`.

## 5. EVENT flow

**Traits**: time-bound, fast-changing, many sources, recurring, and realtime is priority #1. Two loops:

- **Source loop (weekly)**: use AI-search to find *sources* (promoter/venue/Pride-org pages, ticket pages, local LGBTQ+ media, ICS/RSS, sitemaps) per city; add URLs of approved candidates; admins can add sources by hand. Stored in `event_sources` (trust, feed type, cadence, `content_hash`, `last_fetch_at`).
- **Event loop (daily + on-demand)**:
  1. **Structured pulls**: Google Events (SerpApi/DataForSEO) × queries × date windows (`htichips=date:week|month|next_month`), paginated; Ticketmaster (optional).
  2. **Watchlist refresh**: fetch due sources; **if `content_hash` is unchanged skip it (zero LLM calls)**, otherwise extract. This is the main lever for realtime + price + completeness.
  3. **AI-search with date filters**: Exa (`startPublishedDate`, `maxAgeHours`), Tavily (`start_date`/`time_range`), Brave (`freshness`), Parallel (`after_date`), Perplexity (recency) → new pages/sources/events.
  4. **Normalize** → `EventDraft` · 5. **Dedupe/Merge** · 6. **Verify** (NEW/CHANGED): fetch the official/ticket page; check the date is in the future and inside the window, not cancelled, venue/city match, LGBTQ+ evidence present · 7. **Score & triage** · 8. persist + sighting · 9. emit `venue_hint`.

**Fingerprint**: `(core title normalized without year/episode numbers, venue_key or city)`; **the date is not part of the key** — dates live in `occurrences[]`. Fuzzy merge only when title similarity ≥0.88 **and** the venue matches or dates overlap. Recurring events (weekly/monthly) are one candidate with many occurrences. A date without a year (SerpApi) → infer "next occurrence" and set `date_inferred=true`.

**`EventDraft`**
```json
{
  "name": "…", "event_type": "event", "recurring_type": "oneday",
  "occurrences": [{"start_date": "2026-11-14", "end_date": null, "start_time": "21:00", "end_time": null, "timezone": "Asia/Bangkok", "date_inferred": false}],
  "venue": {"name": "…", "address": "…", "city": "…", "country": "TH", "lat": null, "lng": null, "google_place_id": null, "venue_candidate_id": null},
  "organizer": "…", "audience": "mixed",
  "links": {"official": "https://…", "tickets": "https://…", "facebook": null, "instagram": null},
  "price_text": "…", "description": "≤600 chars, our own words",
  "lgbtq": {"level": "primary", "evidence": [{"quote": "…", "url": "https://…"}]},
  "status": "scheduled",
  "sources": [{"url": "…", "title": "…", "provider": "serpapi_events", "retrieved_at": "2026-10-07T00:00:00Z"}],
  "missing_fields": ["price_text"]
}
```
Enums follow TravelGay: `event_type` ∈ `event|venue_event|pride`; `recurring_type` ∈ `oneday|weekly|custom|monthly`; `audience` ∈ `lesbian|gay|bi|trans|queer|lgbt_only|mixed|unknown`.

**Scoring** (0–1): time_validity 0.25 (explicit date inside the window; `date_inferred` counts 0.5) · lgbtq_relevance 0.25 · location_match 0.15 · verifiability 0.15 (official/ticket page alive + ≥2 sources) · completeness 0.10 · source_trust 0.10. **Hard gates**: cancelled → reject; outside the area → reject; not LGBTQ+ → reject; past → `expired` (not a reject). Same thresholds as venues; tombstone TTL 60 days.

**Lifecycle**: `new → rejected | needs_review → approved → (published, later)`; `expired` when the last occurrence is before today (drop the payload after 30 days); `stale` after 3 consecutive runs without a sighting *while its source is still alive and it is still inside the window*; `cancelled` when sources say so (if already approved → create an "update proposal" for the admin, never overwrite silently). **Cadence**: daily watch per city; weekly `deep` run (more queries + extra providers). **Recall canary**: approved upcoming events must be re-found; alert when below threshold.

## 6. Bake-off (Milestone 1)

Goal: choose providers **per flow** with data, ≤10 cities, ≤~$50 of API spend; the pilot is nearly free thanks to free tiers (SerpApi 250 searches/mo, Tavily 1,000 credits, Exa $10 credit/mo, Brave $5 credit/mo, Serper 2,500 queries, Firecrawl 1,000 credits, TinyFish free, Ticketmaster 5,000/day, PredictHQ 3 locations).

**Golden sets**
- *Venue*: TravelGay's active venues per city (SQL in §13.1) as the recall target. If TravelGay lists <15 venues for a city, admins list their own.
- *Event*: TravelGay's upcoming events (via `recurring_dates`); if <10 per city, admins list 10 events per city.
- *Freshness canary* (event): admins supply ~10 events **announced in the last 72 hours** (promoter URLs) → run discovery at T+0, +6h, +24h, +48h and record which provider sees them and how fast.
- Pick cities from TravelGay's numbers: ~3 dense (e.g. Bangkok, Berlin, London), ~2 medium, ~2 hard/multilingual (e.g. Tokyo). Without MySQL access: use CSVs supplied by admins.

**Shared query plan**: per (city × flow) ~12 queries used for **every** provider (templates + local-language synonyms, cached) → fair comparison. A provider may use only the content it returns itself (snippet / raw / contents / summary) — **no separate fetch step** — so each provider's own capability is measured. Text then goes through one shared extractor (a cheap Bedrock model) → `Draft`; search cost is separated from extraction cost.

**Metrics by criterion** (provider × city × flow)
- **Realtime**: `canary_hit_24h`, `median_source_age_days` (published / `page_age` / Last-Modified), `stale_result_rate` (past events returned), `latency_s`.
- **Price**: `cost_usd_per_run`, `$ per 1k calls`, `cost_per_known_found`, `cost_per_valid_novel`.
- **Coverage**: `recall_known`, `novel_valid_count`, `unique_domains`, `language_coverage`, and the **union recall** of every provider combination (greedy by cost).
- **Completeness**: `field_completeness` (share of candidates with all required fields *from the provider alone*: venue = name + address + coordinates + website; event = name + date + venue/address + URL), `results_per_call`, `pagination_ceiling_hit_rate` (hit the cap while still returning new items), `detail_depth`.
- **Quality** (gating): `precision_novel` (human labels), `in_area_rate`, `url_alive_rate`, `lgbtq_evidence_rate`, `dup_rate`.

**Labeling**: export ~30 novel items per provider, shuffled, provider names hidden (CSV: `valid / not_lgbtq / wrong_place / past_or_cancelled / duplicate / spam`) → 5–6 admins label ~15–20 minutes each → import to compute precision.

**Decision rule** (per flow): normalize each metric to [0,1] (min–max across providers), score with the §3.4 weights, **subject to hard floors**: union `recall_known` ≥0.75 (venue) / ≥0.60 (event), `precision_novel` ≥0.60. Pick the **cheapest combination** meeting the floors; on ties prefer fewer vendors and sources that do not rely on scraping Google. Record the outcome in `docs/decisions/0001-search-stack.md`.

**Guardrails**: `--budget-usd` (pilot default 5, hard cap 50), per-call timeouts, at most 1 retry on paid calls, `--dry-run` / `--replay DIR` for offline runs, skip providers without a key. Never write Google payloads to disk (keep only `place_id`/`data_id` + derived metrics). `results/` and `golden/` are in `.gitignore`.

## 7. Architecture & stack

```
Admin UI ──► TravelGay backend (BFF) ──HTTPS + service token──► FastAPI (api)
                                                                  │ enqueue (Postgres queue)
                                                                  ▼
                                                               Worker(s)
        ┌───────────────┬───────────────────┬───────────────────┴──────┬───────────────┐
   Venue pipeline   Event pipeline   Scheduler (daily/weekly)   Publisher (stub)   Verifier
        └── providers (search / maps / open data) ── extractor (Bedrock) ── fetcher
                                  │
                       Postgres (own DB `tg_discovery`)   ◄── optional: TravelGay MySQL read-only (known set)
```

- **Stack**: Python 3.12; FastAPI; Pydantic v2 + pydantic-settings; SQLAlchemy 2 async + asyncpg; Alembic; httpx (async); tenacity; **Procrastinate** (Postgres-native queue with periodic tasks/retries/locks; v2.3 [V], the project is looking for more maintainers) or a hand-written `SELECT … FOR UPDATE SKIP LOCKED` loop; structlog; pytest + pytest-asyncio + respx; ruff (120) + pyright; uv. Postgres 16/18 with `pg_trgm`. Pin references from the source repo: fastapi 0.139.x, pydantic 2.13.x, SQLAlchemy 2.0.5x, uvicorn 0.51.
- **Realtime progress**: SSE (`sse-starlette` or `StreamingResponse`); one replica is enough for 5–6 admins.
- **Provider abstraction**: thin protocols `VenueProvider`, `EventProvider`, `SearchProvider`, `PageFetcher` + `Extractor`/`Judge` on top of an `LLMClient` (Bedrock Responses API, §13.2). Adapters live in `providers/`; enabled per flow via configuration.
- **Cost / concurrency control**: single-flight per `filter_hash` (an active or recently finished run within the TTL is returned; `force=true` overrides; default TTL event 3 h, venue 7 d); budget caps per run (calls, tokens, wall time) and per day + circuit breaker; per-admin rate limit; soft-lock when a review is opened; optimistic `version` on approve.
- **Security**: FastAPI accepts requests only from the TravelGay backend (BFF): `Authorization: Bearer <service token>` + `X-Admin-Id`/`X-Admin-Role`; audit log for every action. Every URL fetch goes through an SSRF guard (block private IPs/metadata endpoints, cap redirects/size/time). Web content is **untrusted** (prompt injection): the LLM has no tools that write data; output must pass a Pydantic schema; never put secrets in prompts or logs.
- **Community safety**: a `safety_flag` based on the country's legal-risk level (Equaldex, optional); never surface private/home events; never store personal data of private individuals; TTLs for rejected/expired data.

## 8. Repo layout

```
tg-discovery/
  README.md  CLAUDE.md  pyproject.toml  uv.lock  .env.sample  .gitignore  Dockerfile  compose.yaml
  alembic.ini  migrations/
  src/tg_discovery/
    main.py  config.py  deps.py  logging.py  worker.py  scheduler.py  cli.py
    api/          venue_runs.py  event_runs.py  runs.py  candidates.py  watches.py  sources.py  meta.py  health.py
    domain/       enums.py  models.py  fingerprint.py  diff.py  lifecycle.py  scoring.py      # pure, no I/O
    pipelines/
      common/     extractor.py  judge.py  urlnorm.py  geo.py  budget.py  queries.py
      venues/     plan.py  recall.py  normalize.py  dedupe.py  verify.py  triage.py  run.py
      events/     plan.py  source_loop.py  recall.py  watchlist.py  normalize.py  dedupe.py  verify.py  triage.py  run.py
    providers/
      base.py                                                    # Protocols
      search/     exa.py  tavily.py  brave.py  parallel.py  perplexity.py  firecrawl.py
                  bedrock_web_search.py  claude_web_search.py  gemini_grounding.py
      serp/       serpapi_events.py  serpapi_maps.py  dataforseo_events.py  serper_maps.py
      maps/       brave_place.py  google_places.py
      opendata/   osm_overpass.py  fsq_os_places.py
      fetch/      tavily_extract.py  firecrawl_scrape.py  httpx_fetch.py
    llm/          bedrock_responses.py  schemas/*.json  prompts/*.md
    store/        db.py  models.py  repos/*.py
    integrations/ known_set.py (port + noop + mysql_readonly)  publisher.py (port + stub outbox)
  evals/          golden/  canaries/  run.py  metrics.py  label.py  report.py  README.md        # M1
  tests/          unit/  contract/  fixtures/
  docs/decisions/ 0001-search-stack.md …
```

## 9. Data model (DDL sketch)

```sql
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TABLE discovery_runs (
  id uuid PRIMARY KEY DEFAULT uuidv7(),           -- or gen_random_uuid() on Postgres < 18
  flow text NOT NULL CHECK (flow IN ('venue','event')),
  trigger text NOT NULL CHECK (trigger IN ('manual','schedule')),
  requested_by text, watch_id uuid,
  filter jsonb NOT NULL, filter_hash text NOT NULL,
  mode text NOT NULL DEFAULT 'quick',             -- quick | deep
  status text NOT NULL DEFAULT 'queued',          -- queued|running|succeeded|partial|failed|cancelled
  budget jsonb, stats jsonb, cost_usd numeric(10,4) DEFAULT 0, error text,
  created_at timestamptz DEFAULT now(), started_at timestamptz, finished_at timestamptz
);
CREATE UNIQUE INDEX uq_runs_active ON discovery_runs (flow, filter_hash) WHERE status IN ('queued','running');  -- single-flight

CREATE TABLE watches (id uuid PRIMARY KEY, flow text NOT NULL, filter jsonb NOT NULL, filter_hash text NOT NULL,
  cron text NOT NULL, enabled boolean DEFAULT true, created_by text, last_run_id uuid);

-- Two separate candidate tables, one per flow
CREATE TABLE venue_candidates (
  id uuid PRIMARY KEY, fingerprint text NOT NULL UNIQUE,
  status text NOT NULL,       -- new|needs_review|approved|rejected|published|stale|closed
  score numeric(4,3), scores jsonb, payload jsonb NOT NULL, content_hash text NOT NULL,
  external_ids jsonb, country_code text, tg_country_id int, tg_city_id int,
  lat double precision, lng double precision, geohash text,
  safety_flag text, rejection_reason text, fast_approve boolean DEFAULT false,
  first_seen_at timestamptz, last_seen_at timestamptz, last_changed_at timestamptz,
  seen_count int DEFAULT 1, miss_count int DEFAULT 0, expires_at timestamptz,
  reviewed_by text, reviewed_at timestamptz, version int DEFAULT 1
);
CREATE TABLE event_candidates (
  id uuid PRIMARY KEY, fingerprint text NOT NULL UNIQUE, status text NOT NULL,   -- also: expired|cancelled
  occurrences jsonb NOT NULL, first_start date, last_end date, series_key text,
  venue_candidate_id uuid REFERENCES venue_candidates(id),
  score numeric(4,3), scores jsonb, payload jsonb NOT NULL, content_hash text NOT NULL,
  country_code text, tg_country_id int, tg_city_id int, safety_flag text, rejection_reason text,
  fast_approve boolean DEFAULT false, first_seen_at timestamptz, last_seen_at timestamptz,
  last_changed_at timestamptz, seen_count int DEFAULT 1, miss_count int DEFAULT 0, expires_at timestamptz,
  reviewed_by text, reviewed_at timestamptz, version int DEFAULT 1
);
CREATE INDEX ON venue_candidates USING gin ((payload->>'name') gin_trgm_ops);
CREATE INDEX ON event_candidates USING gin ((payload->>'name') gin_trgm_ops);

CREATE TABLE sightings (id bigserial PRIMARY KEY, run_id uuid NOT NULL, flow text NOT NULL, candidate_id uuid NOT NULL,
  outcome text NOT NULL,      -- new|unchanged|changed|reappeared
  provider text, seen_at timestamptz DEFAULT now());
CREATE TABLE revisions (id bigserial PRIMARY KEY, flow text, candidate_id uuid, version int, diff jsonb, payload jsonb, created_at timestamptz DEFAULT now());
CREATE TABLE tombstones (flow text, fingerprint text, reason text, scores jsonb, created_at timestamptz DEFAULT now(),
  expires_at timestamptz NOT NULL, PRIMARY KEY (flow, fingerprint));
CREATE TABLE event_sources (id uuid PRIMARY KEY, url_normalized text UNIQUE, domain text, kind text,   -- promoter|venue|media|org|ticketing|feed
  tg_city_ids int[], trust numeric(3,2), feed_type text, cadence_hours int DEFAULT 24,
  last_fetch_at timestamptz, content_hash text, status text DEFAULT 'active');
CREATE TABLE provider_calls (id bigserial PRIMARY KEY, run_id uuid, provider text, endpoint text, units numeric, cost_usd numeric(10,5), latency_ms int, status text, created_at timestamptz DEFAULT now());
CREATE TABLE llm_calls (id bigserial PRIMARY KEY, run_id uuid, stage text, model text, input_tokens int, output_tokens int, cost_usd numeric(10,5), latency_ms int, prompt_version text, created_at timestamptz DEFAULT now());
CREATE TABLE audit_log (id bigserial PRIMARY KEY, actor text, action text, target_type text, target_id text, details jsonb, at timestamptz DEFAULT now());
CREATE TABLE publish_outbox (id bigserial PRIMARY KEY, flow text, candidate_id uuid, target text, payload jsonb,
  status text DEFAULT 'pending', attempts int DEFAULT 0, last_error text, created_at timestamptz DEFAULT now());   -- stub, no worker yet
```

**`content_hash` rule**: hash the normalized *material* fields (venue: name, address, rounded coordinates, website, `operating_status`; event: name, `occurrences`, venue, official URL, `status`); **exclude** summary/description so LLM rephrasing does not create noise.

## 10. API (`/api/v1`)

| Group | Endpoints |
|---|---|
| Start discovery | `POST /venues/runs` `{country, city_ids[], mode, force?}` · `POST /events/runs` `{country, city_ids[], window_days=60, mode, force?}` → `202 {run_id, deduplicated}` |
| Follow a run | `GET /runs/{id}` · `GET /runs/{id}/stream` (SSE) · `GET /runs/{id}/results?outcome=` (new, changed, unchanged) · `GET /runs?flow=&status=` |
| Review (one branch per flow: `/venues/candidates` and `/events/candidates`) | `GET .../candidates?status=&city_id=&q=&cursor=` · `GET .../candidates/{id}` · `POST .../candidates/{id}/approve` · `.../reject` · `.../snooze` · `PATCH .../candidates/{id}` (edit payload before approving) · bulk |
| Watches | `GET/POST/PATCH/DELETE /watches` (filter per flow) |
| Event sources | `GET/POST/PATCH /events/sources` (admins add/disable sources) |
| Reference | `GET /meta/countries` · `GET /meta/cities?country=` (from TravelGay MySQL if enabled, otherwise a local table) |
| Operations | `GET /health` · `GET /costs?from=&to=&flow=` (aggregated from `provider_calls` + `llm_calls`) |

- **Auth**: `Authorization: Bearer <service token>` + `X-Admin-Id`, `X-Admin-Role` (`viewer|reviewer|runner`).
- **SSE events**: `progress {stage, done, total}` · `candidate {id, outcome, name, score}` · `cost {usd}` · `done {stats}`.
- **Approve** (v1): set `approved`, write one `publish_outbox` row (no worker yet) — **no MySQL writes**.
- **Review packet** = candidate + `scores` + `ai_review {verdict, pros, cons, open_questions}` + `dedupe {tg_match, similar[]}` + `history {first_seen_at, seen_count, changes[]}`. For Google data keep only `place_id`/`data_id`; ratings/reviews/hours are fetched live when the admin views it.

## 11. Configuration (`.env.sample`)

- Core: `DATABASE_URL`, `APP_ENV`, `SERVICE_TOKEN`, `BUDGET_USD_PER_RUN`, `BUDGET_USD_PER_DAY`, `RUN_TTL_EVENT_HOURS=3`, `RUN_TTL_VENUE_HOURS=168`, `DEBUG_RAW_CACHE=false`.
- LLM (Bedrock): `AWS_BEDROCK_API_TOKEN`, `AWS_BEDROCK_OPENAI_URL`, `EXTRACTOR_MODEL_ID` (default `openai.gpt-oss-120b`), `JUDGE_MODEL_ID`, `ADJUDICATOR_MODEL_ID`.
- Providers: `SERPAPI_API_KEY`, `DATAFORSEO_LOGIN`, `DATAFORSEO_PASSWORD`, `SERPER_API_KEY`, `EXA_API_KEY`, `TAVILY_API_KEY`, `BRAVE_API_KEY`, `PARALLEL_API_KEY`, `PERPLEXITY_API_KEY`, `FIRECRAWL_API_KEY`, `GOOGLE_PLACES_API_KEY`, `GEMINI_API_KEY`, `ANTHROPIC_API_KEY`, `TICKETMASTER_API_KEY`.
- Optional: `TG_MYSQL_URL` (read-only), `EQUALDEX_API_KEY`, `NOMINATIM_USER_AGENT`, `PROVIDERS_ENABLED_VENUE`, `PROVIDERS_ENABLED_EVENT`.
- Source-repo convention: required variables via `os.environ[...]`, optional via `os.getenv("X", default)` — in the new repo centralize everything in `config.py` (pydantic-settings).

## 12. Milestones

| # | Scope | Acceptance criteria |
|---|---|---|
| M0 | Scaffold: pyproject, ruff/pyright/pytest, compose (Postgres), settings, logging, Alembic baseline, `/health` | check command (ruff + pyright + pytest) is green; `/health` ok |
| M1 | **Bake-off** (§6): harness, golden/canary sets, query plan, Tier-1 adapters in this order: OSM + SerpApi + Brave → Exa/Tavily/Perplexity/Parallel → Tier 2 if needed; metrics, labels, report, budget guardrails | dry-run + replay produce `report.md`; pilot on 1 city using free tiers; `0001-search-stack.md` picks providers per flow. **Safe to stop here and decide.** |
| M2 | Venue flow end-to-end with the chosen providers: run → candidate → review API; fingerprint/dedupe; verify; scoring; tombstones; SSE; single-flight; budgets | run 1 city: candidates with scores + review packets; pressing again within the TTL costs nothing extra |
| M3 | Event flow end-to-end: structured pulls + AI-search + extractor + verify; occurrences; expiry | run 1 city: upcoming events with `occurrences`; past events become `expired` |
| M4 | Daily operation: watches + scheduler; source loop + watchlist; diff lifecycle (stale/expired); recall canary; cost endpoint | run 3 consecutive days: days 2–3 create no duplicates and `unchanged` makes no LLM calls |
| M5 | Review ergonomics + publisher stub: edit-before-approve, bulk, soft-lock, audit; outbox; mapping doc to TravelGay tables; (optional) read-only known-set adapter | 2 admins reviewing concurrently do not collide; outbox holds correctly shaped data |

## 13. Reference material

### 13.1 TravelGay MySQL (Laravel DB `forge`, MySQL 8) — only for golden set / known set / later publishing

- `countries`: id, name, slug (unique), continent_id, cca2, cca3, ccn3, status, timezone, deleted_at (junk rows exist, e.g. "test country"; cca2 is NULL in ~43% of a 165-row sample). `states` (~119 rows): id, name, country_id, slug. `cities` (~39k): id, country_id, state_id (nullable), name, slug (unique, `gay-` prefix), type, source (`cms`/`tg_one`/`ivector_one`), status, timezone, deleted_at. `areas`: id, country_id, city_id, name, lat, lng, radius.
- `venue_types` (45): Sauna=1, Bar=2, Dance Club=3, Shop=4, Eating=5, Cruise Club=6, Beach=7, Gym=13, Massage=14, Bars Lesbian=16, Spa=21, Restaurant=40, Cafe=41, Party=42, Hotels=45…
- `venues` (~10.7k by AUTO_INCREMENT): id, country_id, city_id, state_id, area_id, venue_type_id, external_id (int), name, slug (unique), sub_title, `content` (longtext **NOT NULL**), address, latitude/longitude (varchar), post_code, price, phone, `lgbt_affiliation` (varchar), `source` (default 'cms'), weekday_hours/weekend_hours, facebook_url/instagram_url/twitter_url/youtube_url/booking_url/website_url/whatsapp/line_id/wechat_id, `tg_approved` (int, 0), `status` (1 = active, 0 = inactive), created_by/updated_by, timestamps, `deleted_at` (soft delete).
- `events` (~12k; `source=tg_one` ≈ 99.8%; `event_type` event 8,546 / venue_event 2,093 / pride 506; `city_id` NULL on ~2.7k; `venue_id` NULL/0 on ~8.8k): id, country_id, city_id, state_id, venue_id, hotel_id, venue_type_id, area_id, external_id (bigint), `event_type` (default 'event'), `source`, name, slug (unique), `month` (longtext, almost always NULL), `year` int, sub_title, content, address, latitude/longitude, `gender_type` (default 'male'), `recurring_type` enum('oneday','weekly','custom','monthly'), URL/social columns as in venues, `tg_approved`, venue_name, rating (votes/sum/average_rating), `status`, timestamps, `deleted_at`.
- **Event schedules live in `recurring_dates`**: id, start_date, end_date, start_time, end_time (default 23:59:00), dateable_id, dateable_type (`Domain\Events\Models\Event`).
- The Laravel app has `audits`, `cf_cache_purges`, `media`, `tags/taggables` and generates the unique `slug` → when publishing later, go through the TravelGay app's API/job instead of raw INSERTs; insert a **draft** (`status=0`, `tg_approved=0`, `source='ai_discovery'`).

Golden-set SQL (venues; replace `:city_ids`):
```sql
SELECT v.id, v.name, v.slug, c.name AS city, co.name AS country, co.cca2, v.latitude, v.longitude,
       v.website_url, v.lgbt_affiliation, vt.name AS venue_type
FROM venues v
JOIN cities c ON c.id = v.city_id
JOIN countries co ON co.id = v.country_id
LEFT JOIN venue_types vt ON vt.id = v.venue_type_id
WHERE v.status = 1 AND v.deleted_at IS NULL AND v.city_id IN (:city_ids);
```
Golden-set SQL (upcoming events):
```sql
SELECT e.id, e.name, e.slug, e.event_type, e.recurring_type, e.venue_name, e.website_url, e.booking_url,
       rd.start_date, rd.end_date
FROM events e
JOIN recurring_dates rd ON rd.dateable_id = e.id AND rd.dateable_type = 'Domain\\Events\\Models\\Event'
WHERE e.status = 1 AND e.deleted_at IS NULL AND rd.start_date >= CURDATE() AND e.city_id IN (:city_ids);
```

### 13.2 Calling Bedrock (pattern from the source repo, rewritten without Airflow coupling)

- Use the `openai` SDK pointed at Bedrock's OpenAI-compatible endpoint; authenticate with a Bedrock long-term API key as `api_key`; use the **Responses API** (`/v1/responses`). Some models support only Responses, not Chat Completions.
- Models seen: `openai.gpt-oss-120b` (prefix `/v1`; Responses + Chat Completions + Converse), `openai.gpt-5.5` and `openai.gpt-5.6-luna` (prefix `/openai/v1`, Responses only), `openai.gpt-5.4`, `google.gemma-4-31b`, `xai.grok-4.3`, `zai.glm-5` (Chat Completions at `https://bedrock-mantle.{region}.api.aws/v1`). The prefix differs per model → smoke-test before committing.
- Structured output = forced function call (the Responses tool spec is **flat**, no nested `function` key):
```python
resp = client.responses.create(
    model=model_id, input=prompt, instructions=system, max_output_tokens=4096,
    tools=[{"type": "function", "name": "emit_candidates", "parameters": json_schema}],
    tool_choice={"type": "function", "name": "emit_candidates"},
)
args = next(json.loads(i.arguments) for i in resp.output
            if getattr(i, "type", None) == "function_call" and i.name == "emit_candidates")
```
- Retry with tenacity (4 attempts, 2–20 s backoff) on 429/5xx/connection errors.
- Bedrock Web Search: `client = OpenAI(base_url=f"https://bedrock-mantle.{REGION}.api.aws/openai/v1", …)`, `tools=[{"type": "web_search", "external_web_access": False}]`; citations in `url_citation`; Responses API only; regions us-east-1/us-east-2/us-west-2 [V]. The source repo deploys in AWS region us-west-2.

### 13.3 Extraction rules (keep the spirit from the source repo)

"Extract only what the text explicitly states — **never infer** a missing address/date/field, use empty strings/arrays; dates in ISO 8601 (`YYYY-MM-DD`); every entity carries `source_urls`; the page's routing hint is only a hint, decide from the content." One page may list many events → extract **all** of them.

### 13.4 Source-trust priors (from the source repo's `rubricks.json`, scale 0–100)

LGBTQ+ evidence weights: travel-gay 100, google-map 90, kayak 90, official 90. Source confidence: travel-gay 90, kayak 90, google-map 70, instagram 50, reddit 50, facebook 50, official 50. Use as starting values for `source_trust` (divide by 100) and learn from approve/reject decisions.

### 13.5 Why Airflow is not used (investigated; to avoid reopening)

`venue_crawl` pins one hard-coded queue id and has an `UnboundLocalError` (from reading the code); `event_crawl` only writes markdown; `web_extract` persisted 0 rows in the 2026-09-03 DB snapshot and writes straight into the warehouse/Chroma without review; every DAG has `max_active_runs=1`, one row per run, and a backlog of >2,000 PENDING rows; FAILED carries no reason. If ever needed: trigger `web_crawl` via `POST /api/v2/dags/web_crawl/dagRuns` with `{"conf":{"url":…}}` for approved URLs, and fix four things on the warehouse side (extract-only mode, `correlation_id`, write `entity_ids`/`error`, fix `venue_crawl`).

## 14. Open questions & risks

**Questions for the owner** (the new session asks these first):
1. Can you provide a **read-only** TravelGay MySQL account (golden set, known set, country/city lists)? If not → CSVs supplied by admins.
2. What time of day and time zone for the daily run? Which are the first ≤10 cities?
3. Which keys are available/allowed to buy: SerpApi, Exa, Tavily, Brave, Parallel, Perplexity, Firecrawl, Claude, Gemini, Google Places? Pilot and monthly budget caps?
4. Who labels in M1 (5–6 admins, ~15–20 minutes each)? Who supplies the "canary" event list?
5. Auth: TravelGay backend in front (BFF, service token + `X-Admin-Id`) or direct JWT? Can SSE pass through the proxy?
6. Publishing path later: call the Laravel app's API/job or INSERT into MySQL? Who owns that backend?
7. Policy for high legal-risk countries: flag + stricter review only, or block entirely? Use Equaldex?
8. Licensing stance: is Google-derived data via SerpApi/Serper (grey area) acceptable, or only the official Places API + Brave Place?

**Risks & gotchas**
- **Google/Brave licensing**: see §3.5; Google v. SerpApi is ongoing; Tavily was agreed to be acquired by Nebius in 02/2026 (pricing/terms may change); Exa no longer lists `findSimilar` in current docs.
- **Coverage gap**: Facebook/Instagram Events cannot be crawled legitimately — many small LGBTQ+ events live only there; consider letting admins paste a URL for extraction.
- **Non-determinism**: AI search is not monotonic → use `miss_count`, never delete after a single miss.
- **Dates without a year** (SerpApi Events); time zones: store `timezone` per city and ISO timestamps with offset.
- **Pagination ceilings**: Events 10/page, Maps ~100 results/query → many query variants; track `pagination_ceiling_hit_rate`.
- **Rate limits**: SerpApi caps throughput per plan (≈20% of monthly quota per hour); set concurrency per provider.
- **Same-name cities**: always `(country, city_id)`.
- **Prompt injection** from web pages; **SSRF** when fetching URLs.
- **Equaldex** (optional): separate API, key `EQUALDEX_API_KEY`; not required in v1.
- **[?] to confirm during implementation**: Bedrock Web Search price; LGBTQ+ categories in FSQ OS Places/Overture; LGBTQ+ coverage of PredictHQ; storage ToS of Exa/Tavily/Parallel/Perplexity/Firecrawl/Octen/TinyFish; Octen's official docs; Meetup access; Exa `findSimilar`.

## 15. Kick-off prompt for the new session

```
You are bootstrapping a new repo, `tg-discovery`. Read the attached guide (TG Discovery — Repo Bootstrap Guide) in full.
Process:
1. Summarize your understanding (max 15 lines) and list the missing questions from §14. Do not guess the answers.
2. Before relying on any price/feature in §3, quickly re-check it against the provider's official docs (the guide was written on 2026-10-07).
3. Write a plan for M0–M1 (§12) and WAIT for my approval before creating any file.
4. Never import or modify the tg-datawarehouse repo or Airflow; never write to the TravelGay MySQL; make no paid API call without my key and budget confirmation.
5. Venue and event are two separate pipelines (§4, §5); optimize for the four criteria: realtime, price, coverage, completeness (§3.4, §6).
```

## 16. Sources (verified 2026-10-07)

**Independent benchmark**: [Artificial Analysis Search Index](https://artificialanalysis.ai/agents/search-api) · [methodology](https://artificialanalysis.ai/articles/search-api)
**SerpApi**: [pricing](https://serpapi.com/pricing) · [Google Events API](https://serpapi.com/google-events-api) · [Google Maps API](https://serpapi.com/google-maps-api) · [Google v. SerpApi — ppc.land](https://ppc.land/google-loses-dmca-bid-to-treat-search-scraping-like-dvd-piracy/) · [ruling summary](https://scrapebadger.com/blog/google-sued-a-scraper-under-copyright-law-and-lost-heres-what-the-serpapi-ruling-actually-says)
**DataForSEO / Serper**: [DataForSEO Google Events pricing](https://dataforseo.com/pricing/serp/google-events-serp-api) · [Serper.dev](https://serper.dev)
**Brave**: [Search API & plans](https://brave.com/search/api/) · [LLM Context](https://api-dashboard.search.brave.com/documentation/services/llm-context) · [Place Search](https://brave.com/blog/place-search-improved/)
**Exa**: [pricing](https://exa.ai/pricing) · [Search reference](https://exa.ai/docs/reference/search) · [docs index](https://exa.ai/docs/llms.txt) · [Websets](https://exa.ai/docs/websets/api/how-it-works) · [Websets FAQ/billing](https://docs.exa.ai/websets/faq)
**Tavily**: [credits & pricing](https://docs.tavily.com/documentation/api-credits) · [Search API](https://docs.tavily.com/documentation/api-reference/endpoint/search) · [Research API](https://docs.tavily.com/documentation/api-reference/endpoint/research) · [Nebius to acquire Tavily](https://nebius.com/newsroom/nebius-announces-agreement-to-acquire-tavily-to-add-agentic-search-to-its-ai-cloud-platform)
**Parallel**: [pricing](https://docs.parallel.ai/getting-started/pricing) · [source policy](https://docs.parallel.ai/resources/source-policy) · [FindAll](https://parallel.ai/blog/introducing-findall-api)
**Perplexity**: [pricing](https://docs.perplexity.ai/guides/pricing) · [domain/recency filters](https://docs.perplexity.ai/guides/search-domain-filters)
**Firecrawl / TinyFish / Octen**: [Firecrawl Search](https://docs.firecrawl.dev/features/search) · [TinyFish pricing](https://www.tinyfish.ai/pricing) · [Octen on Product Hunt](https://www.producthunt.com/products/octen-search-infrastructure-for-ai)
**LLM-native search**: [Claude web search](https://platform.claude.com/docs/en/agents-and-tools/tool-use/web-search-tool) · [Claude Platform on AWS](https://platform.claude.com/docs/en/build-with-claude/claude-platform-on-aws) · [Bedrock Web Search](https://aws.amazon.com/blogs/machine-learning/introducing-web-search-on-amazon-bedrock-for-foundation-model-grounding/) · [Gemini Maps grounding](https://ai.google.dev/gemini-api/docs/generate-content/maps-grounding) · [Gemini pricing](https://ai.google.dev/gemini-api/docs/pricing)
**Google Places / open data**: [Places API policies](https://developers.google.com/maps/documentation/places/web-service/policies) · [Google Maps Platform pricing](https://developers.google.com/maps/billing/pricing) · [OSM Key:lgbtq](https://wiki.openstreetmap.org/Key:lgbtq) · [FSQ OS Places](https://docs.foursquare.com/data-products/docs/fsq-places-open-source) · [Overture taxonomy](https://docs.overturemaps.org/guides/places/taxonomy/)
**Events**: [Ticketmaster Discovery](https://developer.ticketmaster.com/products-and-docs/apis/getting-started/) · [Eventbrite search deprecation](https://github.com/Automattic/eventbrite-api/issues/83) · [PredictHQ free plan](https://www.predicthq.com/blog/free-plan-access-predicthq-data-for-free-forever)
**Infrastructure**: [Procrastinate](https://procrastinate.readthedocs.io) · [Bedrock pricing](https://aws.amazon.com/bedrock/pricing/)
