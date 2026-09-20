# Consolidated pipeline — one cron replaces four

`python -m app.pipeline` runs collect → enrich → score → op3_sync in order, in
one job. This replaces the separate collector, enricher, score, and op3-sync
cron services.

## Migrate to it
1. On your existing **collector** service, change the start command to:
   `python -m app.pipeline`
   (Keep its schedule, e.g. `0 */6 * * *`, and its `DATABASE_URL` +
   `PODCASTINDEX_KEY` + `OP3_API_TOKEN` variables — the pipeline's enrich and
   op3_sync stages need those.)
2. **Delete** (or pause) the separate **enricher**, **score**, and **op3-sync**
   cron services — the pipeline now does their work, in guaranteed order,
   right after collection.
3. The **embed-backfill** service is already unused (list-based matching now) —
   delete it too if you haven't.

Result: one scheduled service, one log that tells the whole story, correct
ordering baked in (score always runs on freshly-collected data, op3_sync
inherits the pipeline's own `DATABASE_URL`).

## Behaviour
- **Ordering guaranteed:** enrich runs on the shows collect just refreshed; score
  runs on the chart data collect just wrote; op3_sync runs last since OP3 data
  changes slowly and doesn't need to block anything. No more staggering schedules.
- **Failure isolation:** each stage is independent. A flaky enrich or op3_sync
  does NOT stop score, and only a failed/empty **collect** flips the deploy red
  (matching the old collector's health semantics). Enrich/score/op3_sync
  hiccups are logged, not fatal.
- **Skip flags** (optional): `PIPELINE_SKIP_ENRICH=1`, `PIPELINE_SKIP_SCORE=1`,
  `PIPELINE_SKIP_OP3=1`.
- **OP3 throttling** (optional): OP3 download data changes slowly, so you don't
  need to hit the API every 6-hour cycle. Set `OP3_SYNC_EVERY_N_CYCLES=3` to run
  it only every 3rd cycle (once per 18h), or `=5` for once per 30h. Defaults to
  running every cycle.

## Why keep it scheduled (not "on update")
Collection is inherently temporal — chart snapshots must be captured every few
hours whether or not anyone visits the site, because the time-series is destroyed
by delay. There's no user "update" event to hang it on. So the pipeline stays a
cron; consolidating the THREE crons into ONE is the win, not eliminating the
schedule.

## Detector improvements are separate (on-demand, not cron)
When you tune sponsor/guest patterns, run `python -m app.enrich.redetect` once —
it re-runs detection over cached text, no fetching. That's deliberately NOT in
the pipeline (you only run it when you change a detector, not every 6 hours).
