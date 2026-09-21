"""One ordered pipeline — replaces the separate collector / enricher / score
cron services with a single scheduled job.

    python -m app.pipeline

Runs, in order:
  1. collect  — pull fresh chart data (the irreducibly-temporal step)
  2. enrich   — fetch + detect sponsors/guests for charting shows not yet done
  3. score    — recompute PodScope Scores on the fresh chart data

Why one ordered job beats three crons:
  - Correct ordering is GUARANTEED (score always runs after collect, on the same
    data), instead of staggering three schedules and hoping they line up.
  - One service to deploy, configure, and watch — one log tells the whole story.

Failure isolation: each stage runs in its own try/except. A flaky enrich must
NOT stop scoring, and a bad score step must not hide that collection succeeded.
Each stage reports its own status; the pipeline exits non-zero only if the
CRITICAL stage (collect) failed or produced nothing — matching the collector's
own health semantics — so Railway's red badge still means "the important thing
broke", not "one optional enrichment hiccuped".

Stages can be skipped with env flags for flexibility:
  PIPELINE_SKIP_ENRICH=1   PIPELINE_SKIP_SCORE=1
"""

import os
import sys
import time
import traceback


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

    # 4. OP3 — verified downloads. Data changes slowly, so run it only every Nth
    # pipeline cycle (default: every cycle if OP3_EVERY_N=1). Non-critical.
    if os.environ.get("PIPELINE_SKIP_OP3", "").lower() not in ("1", "true", "yes"):
        every_n = int(os.environ.get("OP3_EVERY_N", "1"))
        if _should_run_op3(every_n):
            from app.op3.sync import main as op3_main
            results.append(_run_stage("op3", op3_main))
        else:
            print(f"\n[pipeline] op3 skipped this cycle (runs every {every_n} cycles)")
    else:
        print("\n[pipeline] op3 skipped (PIPELINE_SKIP_OP3 set)")

    # summary
    print("\n===== [pipeline] summary =====")
    for r in results:
        status = "ok" if r["ok"] else f"FAILED ({r['error']})"
        print(f"  {r['stage']:8} {status} — {r['seconds']}s")

    # Exit code: only the critical collect stage determines red/green, matching
    # the collector's own semantics. Enrich/score/op3 failures are logged but
    # don't flip the badge (they're best-effort downstream work).
    return 0 if collect["ok"] else 1


def _should_run_op3(every_n: int) -> bool:
    """OP3 data changes slowly — no need to hit it every pipeline cycle. Track a
    counter in a tiny DB marker so 'every Nth cycle' survives restarts."""
    if every_n <= 1:
        return True
    try:
        from sqlalchemy import text as _t
        from app.db.session import SessionLocal
        db = SessionLocal()
        try:
            db.execute(_t("CREATE TABLE IF NOT EXISTS pipeline_markers "
                          "(name TEXT PRIMARY KEY, n INTEGER)"))
            row = db.execute(_t("SELECT n FROM pipeline_markers WHERE name='op3_cycle'")).first()
            n = (row[0] if row else 0) + 1
            db.execute(_t("INSERT INTO pipeline_markers (name, n) VALUES ('op3_cycle', :n) "
                          "ON CONFLICT (name) DO UPDATE SET n = :n"), {"n": n})
            db.commit()
            return n % every_n == 0
        finally:
            db.close()
    except Exception:
        return True  # if the counter fails, just run it


if __name__ == "__main__":
    raise SystemExit(main())
