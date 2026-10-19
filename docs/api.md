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
   Poll every few seconds; a city takes about a minute for venues and two to five minutes for events.
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

Venue runs also take `venue_types`, any of `Cafe`, `Restaurant`, `Adults`, `Bars`, `Dance Club`, `Saunas`, `Massage`,
`Gyms`, `Attractions`, `Tours` (left out or empty: all ten; another value is a `422`). Each type is searched with three
queries (a general one, a gay or gay-friendly one and a third wording), so the cost and the time of a run follow the
number of types: about 0.06 USD per type and city, about 0.57 USD for all ten. Venues are not limited to LGBTQ+ places,
and the type only decides what is searched: a venue of another type, or of no clear type, that comes back is kept.
The same request with other types is another run; the same types in another order are the same request.

## Authentication

With `SERVICE_TOKEN` set, every call needs `Authorization: Bearer <token>`, `X-Admin-Id` and `X-Admin-Role`
(`viewer` reads, `reviewer` also decides, `runner` also starts runs). The id is written to the audit log. Without a
token, outside production, the API is open and acts as a runner called `dev`, so Swagger works as it is.

## What a candidate holds, and what it does not yet

Filled from what the sources state, never guessed: names, dates and times (the year is inferred when a page omits it, and
`date_inferred` says so), venue, address, links, price text, the LGBTQ+ level with the quotes that support it, and the
sources. Venues also carry coordinates, type, website, phone and opening hours where a source gave them.

Event organiser (`event.organizer` and the row's `contact`): the name, and the organiser's own `url` (website or social
page), `email` and `phone` when a source publishes them for an organisation. Personal data of private individuals is
never kept: an email must be a shared mailbox (`info@`, `booking@`, `events@`...), a phone number is
kept only for an organiser that has its own website or social page, and both must be written on a page the event was
found on. Expect them to be empty for most events.

Often empty, so the screens need an empty state: the organiser's contacts (above), the description (an AI draft from
the source), `tickets.kind` (read from the price text), `lat`/`lng` of events, a venue's `price_text` (such as `$$`), and
for Google-derived data everything except names, addresses, coordinates, links and ids. With `KEEP_GOOGLE_DETAILS=true`
(off by default, because Google's terms do not allow storing them) Google's phone, opening hours, description and price
are kept as well; Google Maps gives them for most venues, while Google's events pack has none of them. A later run fills
a phone, hours, description or price that a candidate waiting for review lacks, without a new `version`; it never
replaces a stored one.

Event pictures (`event.images`): the URLs of the pictures of the pages that are about that event alone, up to 20, each
with the page it came from (`source_url`) and the page's own `alt` text. Two sources feed them: Exa's main picture of the
page, and the page itself, which the service reads (`FETCH_EVENT_PICTURES`, on by default): the social preview first,
then the pictures of the article, each once in its largest spelling. Blurred stand-ins, logos, icons, avatars, banners,
tracking pixels, pictures in the page header, navigation or footer, and cards that link to other pages are left out.
A page that names several events (a calendar, a list) adds no picture to any of them, because it does not say whose
picture is whose. Pages are fetched politely (`robots.txt` honoured, one request at a time per site, never Facebook,
Instagram or Google, behind the SSRF guard with the connection pinned to the checked address). The pictures are never
downloaded or stored here, so check the right to use one before copying it to your own storage. A later run adds
pictures to a candidate that is still waiting for review, without a new `version`. Google-derived events have none.
Venues have no pictures yet.

Not collected yet: venue pictures, topics, expected attendance, the finer event category, the AI verdict, the match with
a TravelGay record (`dedupe`), SSE progress and edit-before-approve. The fields exist in the packet (empty) so the shape
will not change when they are filled.

`venue.venue_type` is one of the ten types above, or `null` when the sources do not make it clear (a hotel, a shop).
For venues, `lgbtq_level` and `why_relevant` say how far a venue is LGBTQ+ (`primary`, `friendly`, `unclear`, `no`),
read from the sources; it is information for the reviewer and does not change the score. Events are still LGBTQ+
events: one that is not is dropped.

`ai_review.cons` is a rule-based list of what to look at twice (one source only, no official page, inferred year...).
`fast_approve` is only a label for an easy approve.

## Costs and limits

A run reads `BUDGET_USD_PER_RUN` and stops searching before it would spend more (`stats.stopped_by_budget`). Providers
whose terms must be accepted first (SerpApi) run only when named in `ACCEPTED_TERMS`.

## How long a run took

`GET /runs/{id}` gives `duration_s` (start to finish) and `stats.timing`: `total_s`, and for each city its `seconds`
split into `locate_s` (finding the area), `collect_s` (searching, reading and pictures), `merge_s` (merging and scoring)
and `save_s`, plus `busy`: for each kind of call (`search:exa`, `search:serpapi_events`, `places:serpapi_maps`, `extract`,
`pictures`) the number of `calls`, their `total_s` and the `slowest_s`. The calls of a city overlap, so the totals in
`busy` can add up to more than `collect_s`; the slowest call and the kind with the largest total show where the wait was.

The same figures go to the service log, one line per step (`run started`, `city done`, `run finished`), for example
`city done city=Bangkok seconds=121.3 locate_s=0.0 collect_s=118.2 merge_s=0.4 save_s=0.3 drafts=176 candidates=76 ...`.
