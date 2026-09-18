# PodScope MVP

Cross-market podcast chart intelligence. Collects Apple + Spotify chart rankings
by country and category on a schedule, stores every snapshot as time-series data,
and serves public per-show ranking pages plus a JSON API.

This is the **irreplaceable layer**: chart history is destroyed by delay. Deploy
`chart-collector` first and let it accumulate. Everything else builds on the data
it gathers.

## What's in the box

```
app/
  collectors/
    apple.py        # Apple Marketing Tools chart feeds (public, keyless)
    spotify.py      # podcastcharts.byspotify.com JSON (public)
    run.py          # entrypoint the cron worker calls
  db/
    models.py       # SQLAlchemy models
    session.py      # engine + session, reads DATABASE_URL
    init_db.py      # create tables + seed catalog of countries/categories
  api/
    main.py         # FastAPI app: JSON API + HTML show pages
  templates/        # Jinja2: home, show page
  static/           # css
config/
  countries.py      # storefronts to track (start small, expand)
Dockerfile
railway.json        # two services: web + collector cron
requirements.txt
```

## Two Railway services, one repo

1. **web** — the FastAPI app (`uvicorn app.api.main:app`). Public pages + API.
2. **chart-collector** — a cron service running `python -m app.collectors.run`
   every 6 hours. No HTTP port; it wakes, collects, writes, exits.

Both share one **Postgres** database (add the Railway Postgres plugin; it injects
`DATABASE_URL`).

## First deploy

1. Create a Railway project, add the **Postgres** plugin.
2. Deploy this repo as the **web** service. Set start command to
   `uvicorn app.api.main:app --host 0.0.0.0 --port $PORT`.
3. On first boot the web service runs `init_db` (idempotent) to create tables.
4. Add a second service from the same repo — the **collector**. Set it as a
   **cron** service with schedule `0 */6 * * *` and start command
   `python -m app.collectors.run`.
5. Watch the collector logs: it prints rows inserted per (platform, country,
   category). Re-runs are safe — duplicate snapshots for the same day are ignored.

## Verify the endpoints on first run

Two upstream shapes this code depends on (both public, both stable, but confirm):

- **Apple:** `https://rss.marketingtools.apple.com/api/v2/{country}/podcasts/top/200/podcasts.json`
  → `{ "feed": { "results": [ { "id", "name", "artistName", "artworkUrl100", ... } ] } }`
  Rank = array order. Note: this genre-agnostic feed is the "top overall" chart.
  Per-genre charts use the same host with a genre query — see `apple.py` notes.
- **Spotify:** `https://podcastcharts.byspotify.com/api/charts/top?region={cc}`
  → JSON array of shows with `showName`, `showPublisher`, `chartRankMove`, etc.
  Confirm the exact field names in the collector; Spotify has renamed them before.

If either shape has drifted, the collector logs the raw payload it couldn't parse
(first 500 chars) so you can adjust the parser without losing the run.

## Enricher service (sponsors + guest appearances)

A third service turns transcripts/show-notes into detected sponsor deals and
guest-booking suitability — the SponsorRadar-style layer.

- `app/enrich/transcripts.py` — RSS episode + transcript fetch, cheapest source
  first (Podcasting 2.0 transcript tag → show notes → STT stub).
- `app/enrich/sponsors.py` — rule-based ad detection (brand, promo code, URL,
  host_read/mention/affiliate classification, confidence). Optional LLM pass
  when `ANTHROPIC_API_KEY` is set upgrades classification.
- `app/enrich/guest.py` — books-guests? / frequency / topics / suitability note.
- `app/enrich/run.py` — queue-driven worker. `python -m app.enrich.run`
  auto-queues the top charting shows and processes pending jobs (cap with
  `ENRICH_MAX_JOBS`, default 25).

Run it as a **cron** service (e.g. `0 3 * * *`, offset from the collector) or a
worker. It never blocks chart collection. STT (Tier 3) is stubbed — wire in
Whisper/Deepgram/Groq in `transcripts.transcribe()` when you decide on cost.

**Contact data:** guest-booking contact comes from the RSS `<itunes:owner>`
email — public and clean. Brand/sponsor-side contacts are NOT in feeds; that's a
separate, carefully-sourced premium tier, deliberately not built here.

**Test the enricher offline (no key, no network):**
```bash
python scripts_test_enrich.py
```
