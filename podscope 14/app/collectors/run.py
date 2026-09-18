"""Cron entrypoint. Railway's chart-collector service runs:  python -m app.collectors.run

Ensures tables exist, runs both collectors, prints a one-line summary per
platform so the Railway log answers "did it work and how many rows?" at a glance.

Exit policy — this is the important bit:
  Individual chart requests to Apple/Spotify fail intermittently (500s under
  load). Each collector already tolerates that per-chart and still writes every
  chart that succeeded. So a run with some flaky charts is a PARTIAL SUCCESS and
  must NOT be reported to Railway as a crash.

  We therefore exit 0 when the run inserted a healthy number of rows, and exit 1
  only when almost nothing came back (a real upstream outage worth a red badge
  and an alert). Per-chart failures are always logged either way, so you never
  lose visibility into what 500'd.
"""

import os
import sys

from app.db.init_db import init_db
from app.db.session import SessionLocal
from app.collectors.apple import collect_apple
from app.collectors.spotify import collect_spotify

# Below this many total rows, treat the run as a genuine failure (upstream down,
# bad deploy, DB unreachable). Tune once you know a normal run's row count.
MIN_HEALTHY_ROWS = int(os.environ.get("MIN_HEALTHY_ROWS", "100"))


def main() -> int:
    init_db()
    db = SessionLocal()
    total_rows = 0
    try:
        apple_run = collect_apple(db)
        total_rows += apple_run.rows_inserted
        print(
            f"[apple] status={apple_run.status} "
            f"charts={apple_run.charts_collected} "
            f"rows_inserted={apple_run.rows_inserted}"
        )
        if apple_run.notes:
            # Partial per-chart failures: logged, not fatal.
            print(f"[apple] partial failures:\n{apple_run.notes}", file=sys.stderr)

        # Spotify can be disabled with an env flag while you sort out legal.
        if os.environ.get("DISABLE_SPOTIFY", "").lower() not in ("1", "true", "yes"):
            spotify_run = collect_spotify(db)
            total_rows += spotify_run.rows_inserted
            print(
                f"[spotify] status={spotify_run.status} "
                f"charts={spotify_run.charts_collected} "
                f"rows_inserted={spotify_run.rows_inserted}"
            )
            if spotify_run.notes:
                print(f"[spotify] partial failures:\n{spotify_run.notes}",
                      file=sys.stderr)
        else:
            print("[spotify] skipped (DISABLE_SPOTIFY set)")
    finally:
        db.close()

    # Partial success is success. Only a near-empty run is a real failure.
    if total_rows >= MIN_HEALTHY_ROWS:
        print(f"[run] ok — {total_rows} rows inserted "
              f"(per-chart failures tolerated)")
        return 0
    print(f"[run] FAILED — only {total_rows} rows inserted "
          f"(threshold {MIN_HEALTHY_ROWS}); upstream likely down",
          file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
