"""Cron entrypoint. Railway's chart-collector service runs:  python -m app.collectors.run

Ensures tables exist, runs both collectors, prints a one-line summary per
platform so the Railway log answers "did it work and how many rows?" at a glance.

External chart APIs (Apple, Spotify) are flaky and occasionally return 500s
or time out. Each collector already isolates per-chart failures internally,
but we also guard the top-level call here in case a platform blows up in a
way its own error handling didn't anticipate (e.g. an unhandled RetryError
bubbling out of the HTTP client). A platform failing should never take down
the other platform's run, and the cron should only be marked failed if NO
rows were inserted at all -- partial success is still success.
"""

import os
import sys
import traceback

from app.db.init_db import init_db
from app.db.session import SessionLocal
from app.collectors.apple import collect_apple
from app.collectors.spotify import collect_spotify


def _run_platform(name, fn, db):
    """Run a single collector, never letting it raise out of this function.

    Returns (status, charts_collected, rows_inserted).
    """
    try:
        run = fn(db)
        print(
            f"[{name}] status={run.status} "
            f"charts={run.charts_collected} "
            f"rows_inserted={run.rows_inserted}"
        )
        if run.status != "ok":
            print(f"[{name}] problems:\n{run.notes}", file=sys.stderr)
        return run.status, run.charts_collected, run.rows_inserted
    except Exception as exc:  # noqa: BLE001 - a platform failing must not crash the run
        db.rollback()
        print(
            f"[{name}] status=error charts=0 rows_inserted=0 "
            f"(unhandled exception, platform skipped)",
        )
        print(
            f"[{name}] problems:\n{name}: unhandled {exc!r}\n"
            f"{traceback.format_exc()}",
            file=sys.stderr,
        )
        return "error", 0, 0


def main() -> int:
    init_db()
    db = SessionLocal()
    total_inserted = 0
    any_errors = False
    try:
        status, _charts, inserted = _run_platform("apple", collect_apple, db)
        total_inserted += inserted
        any_errors = any_errors or status != "ok"

        # Spotify can be disabled with an env flag while you sort out legal.
        if os.environ.get("DISABLE_SPOTIFY", "").lower() not in ("1", "true", "yes"):
            status, _charts, inserted = _run_platform("spotify", collect_spotify, db)
            total_inserted += inserted
            any_errors = any_errors or status != "ok"
        else:
            print("[spotify] skipped (DISABLE_SPOTIFY set)")
    finally:
        db.close()

    if any_errors:
        print(
            f"[collector] completed with errors, total rows_inserted={total_inserted}",
            file=sys.stderr,
        )

    # A platform erroring out (e.g. transient upstream 500s) is non-fatal as
    # long as *something* got inserted this run. Only fail the cron outright
    # when every platform came back empty-handed.
    if total_inserted > 0:
        return 0
    return 1 if any_errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
