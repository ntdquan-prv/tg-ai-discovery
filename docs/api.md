# API guide

Written for the people who build the admin screens on top of this service. Swagger (`/docs`) is the exact contract; this
page says how the pieces fit and which fields are not filled in yet.

## The flow

1. **Start**: `POST /api/v1/events/runs` or `/venues/runs` with a country and cities. It answers at once with a `run_id`
   and a readable `code` such as `DSC-20261008-001`. The same request made again while it runs, or within a few hours of
   finishing (events 3 h, venues 7 days), returns that run with `deduplicated: true` and costs nothing; send
   `force: true` to search again.
2. **Follow**: `GET /api/v1/runs/{run_id}` until `status` is `succeeded`, `partial` (some providers or cities failed, the
   rest is there) or `failed`. `stats` holds the progress, the calls made, what was found and dropped, and the errors.
   Poll every few seconds; a run takes one to three minutes per city.
3. **List**: `GET /api/v1/runs/{run_id}/results` gives the rows of the review table, best score first.
   `GET /api/v1/events/candidates?status=needs_review&city=...&q=...` is the same table across runs.
4. **Review**: `GET /api/v1/events/candidates/{id}?run_id=...` is the review packet; `run_id` adds `position`
   ("Candidate 3 of 76") with the ids of the previous and next candidate.
5. **Decide**: `POST .../approve` or `.../reject` with the `version` of the packet. A `409` means somebody decided first
   (`version_conflict`) or it was decided already (`already_decided`); reload the packet. Approving only queues the
   candidate in an outbox: **nothing is published and nothing is written to TravelGay**.

Venues have the same endpoints under `/venues/...`; the packet has a `venue` block instead of an `event` block.

## Request body

```json
{
  "country": "US",
  "country_name": "United States",
  "cities": [{"name": "New York", "timezone": "America/New_York", "tg_city_id": 123, "lat": 40.7128, "lng": -74.006}],
  "window_days": 60,
  "force": false
}
```

`timezone` is required (TravelGay knows it per city). `lat` and `lng` are optional: without them the city is looked up
once on Nominatim. `window_days` only matters for events. At most 10 cities.

## Authentication

With `SERVICE_TOKEN` set, every call needs `Authorization: Bearer <token>`, `X-Admin-Id` and `X-Admin-Role`
(`viewer` reads, `reviewer` also decides, `runner` also starts runs). The id is written to the audit log. Without a
token, outside production, the API is open and acts as a runner called `dev`, so Swagger works as it is.

## What a candidate holds, and what it does not yet

Filled from what the sources state, never guessed: names, dates and times (the year is inferred when a page omits it, and
`date_inferred` says so), venue, address, links, price text, the LGBTQ+ level with the quotes that support it, and the
sources. Venues also carry coordinates, type, website, phone and opening hours where a source gave them.

Often empty, so the screens need an empty state: the organiser (events: a name only, and only when a source states it;
phone and email are not collected), the description (an AI draft from the source), `tickets.kind` (read from the price
text), `lat`/`lng` of events, and for Google-derived data everything except names, addresses, coordinates, links and ids.

Not collected yet: images, topics, expected attendance, the finer event category, the AI verdict, the match with a
TravelGay record (`dedupe`), SSE progress and edit-before-approve. The fields exist in the packet (empty) so the shape
will not change when they are filled.

`ai_review.cons` is a rule-based list of what to look at twice (one source only, no official page, inferred year...).
`fast_approve` is only a label for an easy approve.

## Costs and limits

A run reads `BUDGET_USD_PER_RUN` and stops searching before it would spend more (`stats.stopped_by_budget`). Providers
whose terms need the owner's review (Brave, Parallel, SerpApi) run only when named in `ACCEPTED_TERMS`.
