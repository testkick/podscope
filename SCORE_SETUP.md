# PodScope Score — setup

Computes the proprietary 0–100 score for every currently-charting show, 7-day
smoothed. Powers the score badge on show pages and the "PodScope Top Chart" on
the homepage.

## New tables (auto-created)
`podscope_scores` (one row per show, what the UI reads) and `score_history`
(daily raw score per show, for smoothing + trend). Created automatically by
init_db on next boot — no manual migration needed.

## Run it as a cron service
Same pattern as collector/enricher:
- **Start Command:** `python -m app.score.run`
- **Cron Schedule:** `0 */6 * * *` — run a bit AFTER the collector so it scores
  fresh chart data (e.g. collector at `0 */6`, score at `30 */6`).
- **Variables:** `DATABASE_URL` → `${{Postgres.DATABASE_URL}}`. (No Podcast Index
  key needed — scoring reads only chart data already in the DB.)

## How the score works (for the FAQ / methodology page)
Publishable inputs, proprietary weighting:
- **Chart strength** (45%) — best rank across all markets/platforms, log-curved
  so #1 ≫ #20, plus depth of top placements.
- **Cross-platform consensus** (15%) — charting on both Apple AND Spotify beats
  one platform. Unique to our merged show identities.
- **Market breadth** (15%) — how many countries it charts in.
- **Trajectory** (15%) — 7-day rank movement (climbing vs fading). Needs our
  history; un-scrapeable from today's charts.
- **Durability** (10%) — how many of the last 7 days it charted.

Smoothed over 7 days so it's stable (won't swing 20 pts overnight). Trend arrow
(▲/▼) reflects 7-day direction.

## Note on smoothing warm-up
Until there are several days of score_history, the smoothed score ≈ that day's
raw score — expected. It tightens as history accumulates. Trend needs ~7 days of
chart history to read anything but "flat".
