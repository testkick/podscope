# Enricher cron service — setup

Fills in episodes, sponsor deals, and guest profiles for charting shows. Runs
like the collector: a scheduled Railway service that wakes, does a batch, exits.

## Create the service

Same pattern as the collector:

1. Railway project → **+ New → GitHub Repo** → same `podscope` repo.
2. In the new service **Settings**:
   - **Start Command:** `python -m app.enrich.run`
   - **Cron Schedule:** `0 */4 * * *`  (every 4 hours, offset from the collector)
   - No public domain / PORT needed — it runs to completion and exits.
3. **Variables** — this service needs:
   - `DATABASE_URL` → reference `${{Postgres.DATABASE_URL}}` (same DB as everything)
   - `PODCASTINDEX_KEY` and `PODCASTINDEX_SECRET` (feed fallback)
   - Optional tuning (sensible defaults built in):
     - `ENRICH_MAX_JOBS` — jobs processed per run (default 25)
     - `ENRICH_DELAY_MIN` / `ENRICH_DELAY_MAX` — polite gap between shows in
       seconds (default 0.5–2.0)
     - `ENRICH_QUEUE_LIMIT` — how many eligible shows to enroll (default 10000)
     - `ENRICH_CIRCUIT_BREAK` — abort after N consecutive failures (default 8)

## How it covers the catalog

Each run: `auto_queue` enrolls any eligible charting show that isn't already
queued (ordered best-rank first), then `run_pending` fetches + detects up to
`ENRICH_MAX_JOBS` of them. Jobs are one-and-done, so each run advances through
the backlog. At 25 jobs/run × 6 runs/day = ~150 jobs/day = ~75 shows/day
(2 jobs each). For ~2,000 charting shows that's a couple of weeks to full
coverage, then it idles except for newly-charting shows.

**Want faster initial coverage?** Temporarily raise `ENRICH_MAX_JOBS` (e.g. 200)
for a few days, then drop it back. Watch feed-host politeness — the built-in
0.5–2s delay between shows keeps it civil; don't set the delay to 0 at high
batch sizes.

## Improving detectors later

When you tune sponsor/guest patterns, DON'T re-run this (it re-fetches). Run the
**redetect** pass instead — it re-runs detection over already-cached text, no
network:

    python -m app.enrich.redetect            # whole catalog, from cache
    python -m app.enrich.redetect --show <slug>

Or set `REDETECT_ON_STARTUP=1` on the web service so each deploy refreshes
detection automatically.

## The two layers, restated

- **enrich.run (this cron)** — FETCH layer. Hits feeds, rate-limited, incremental,
  one pass per show. Fills in NEW shows and NEW episodes.
- **enrich.redetect** — DETECT layer. Re-reads stored text, free, re-runnable.
  Cascades detector improvements across everyone already fetched.
