"""One ordered pipeline — replaces the separate collector / enricher / score
cron services with a single scheduled job.

    python -m app.pipeline

Runs, in order:
  1. collect  — pull fresh chart data (the irreducibly-temporal step)
  2. enrich   — fetch + detect sponsors/guests for charting shows not yet done
  3. score    — recompute PodScope Scores on the fresh chart data
  4. op3_sync — refresh OP3 download-measurement overlap for our shows

Why one ordered job beats four crons:
  - Correct ordering is GUARANTEED (score always runs after collect, on the same
    data), instead of staggering four schedules and hoping they line up.
  - One service to deploy, configure, and watch — one log tells the whole story.
  - OP3 sync inherits the pipeline's DATABASE_URL for free — no separate
    service, no separate credentials to wire up.

Failure isolation: each stage runs in its own try/except. A flaky enrich must
NOT stop scoring, and a bad score or op3_sync step must not hide that
collection succeeded. Each stage reports its own status; the pipeline exits
non-zero only if the CRITICAL stage (collect) failed or produced nothing —
matching the collector's own health semantics — so Railway's red badge still
means "the important thing broke", not "one optional stage hiccuped".

Stages can be skipped with env flags for flexibility:
  PIPELINE_SKIP_ENRICH=1   PIPELINE_SKIP_SCORE=1   PIPELINE_SKIP_OP3=1

OP3 data changes slowly (download counts trickle in over weeks), so it's fine
to run it every pipeline cycle (every 6 hours) — but to avoid unnecessary API
hits you can throttle it to run only every Nth cycle via:
  OP3_SYNC_EVERY_N_CYCLES=3   # e.g. 3 => once per 18h, 5 => once per 30h
When set, the pipeline tracks a cycle counter in a small state file so the
throttle survives across separate `python -m app.pipeline` invocations.
"""

import os
import sys
import time
import traceback


STATE_FILE = os.environ.get("PIPELINE_STATE_FILE", "/tmp/.podscope_pipeline_state")


def _next_op3_cycle() -> int:
    """Increment and return the pipeline's persistent cycle counter, used to
    throttle OP3 sync to every Nth run (OP3_SYNC_EVERY_N_CYCLES). If the state
    file can't be read/written (e.g. no persistent volume across cron runs),
    we fail safe and just run OP3 sync every cycle."""
    try:
        cycle = 0
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE) as f:
                cycle = int((f.read() or "0").strip() or 0)
        cycle += 1
        with open(STATE_FILE, "w") as f:
            f.write(str(cycle))
        return cycle
    except Exception:  # noqa: BLE001 - throttling is best-effort, never fatal
        return 1


def _run_stage(name, fn) -> dict:
    """Run one stage, catching everything so later stages still run."""
    started = time.time()
    print(f"\n===== [pipeline] stage: {name} =====")
    try:
        rc = fn()
        # stages return either an int exit code (collector) or None
        ok = (rc in (None, 0))
        secs = round(time.time() - started, 1)
        print(f"[pipeline] {name} finished in {secs}s (ok={ok})")
        return {"stage": name, "ok": ok, "seconds": secs, "error": None}
    except Exception as exc:  # noqa: BLE001 - isolate stage failures
        secs = round(time.time() - started, 1)
        print(f"[pipeline] {name} FAILED after {secs}s: {exc}", file=sys.stderr)
        traceback.print_exc()
        return {"stage": name, "ok": False, "seconds": secs, "error": str(exc)}


def main() -> int:
    results = []

    # 1. COLLECT — the critical, temporal stage.
    from app.collectors.run import main as collect_main
    collect = _run_stage("collect", collect_main)
    results.append(collect)

    # 2. ENRICH — downstream of new chart data; optional/non-critical.
    if os.environ.get("PIPELINE_SKIP_ENRICH", "").lower() not in ("1", "true", "yes"):
        from app.enrich.run import main as enrich_main
        results.append(_run_stage("enrich", lambda: enrich_main()))
    else:
        print("\n[pipeline] enrich skipped (PIPELINE_SKIP_ENRICH set)")

    # 3. SCORE — downstream of fresh chart data; optional/non-critical.
    if os.environ.get("PIPELINE_SKIP_SCORE", "").lower() not in ("1", "true", "yes"):
        from app.score.run import main as score_main
        results.append(_run_stage("score", score_main))
    else:
        print("\n[pipeline] score skipped (PIPELINE_SKIP_SCORE set)")

    # 4. OP3_SYNC — refresh OP3 download-measurement overlap; optional/non-critical.
    # Data changes slowly, so it's safe to throttle to every Nth cycle.
    if os.environ.get("PIPELINE_SKIP_OP3", "").lower() not in ("1", "true", "yes"):
        every_n = int(os.environ.get("OP3_SYNC_EVERY_N_CYCLES", "1") or "1")
        cycle = _next_op3_cycle()
        if every_n <= 1 or cycle % every_n == 0:
            from app.op3.sync import main as op3_sync_main
            results.append(_run_stage("op3_sync", op3_sync_main))
        else:
            print(f"\n[pipeline] op3_sync skipped (cycle {cycle}, "
                  f"runs every {every_n} cycles)")
    else:
        print("\n[pipeline] op3_sync skipped (PIPELINE_SKIP_OP3 set)")

    # summary
    print("\n===== [pipeline] summary =====")
    for r in results:
        status = "ok" if r["ok"] else f"FAILED ({r['error']})"
        print(f"  {r['stage']:8} {status} — {r['seconds']}s")

    # Exit code: only the critical collect stage determines red/green, matching
    # the collector's own semantics. Enrich/score failures are logged but don't
    # flip the badge (they're best-effort downstream work).
    return 0 if collect["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
