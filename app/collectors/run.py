"""Cron entrypoint. Railway's chart-collector service runs:  python -m app.collectors.run

Ensures tables exist, runs both collectors, prints a one-line summary per
platform so the Railway log answers "did it work and how many rows?" at a glance.
Exits non-zero if either collector errored, so Railway surfaces the failure.
"""

import os
import sys

from app.db.init_db import init_db
from app.db.session import SessionLocal
from app.collectors.apple import collect_apple
from app.collectors.spotify import collect_spotify


def main() -> int:
    init_db()
    db = SessionLocal()
    exit_code = 0
    try:
        apple_run = collect_apple(db)
        print(
            f"[apple] status={apple_run.status} "
            f"charts={apple_run.charts_collected} "
            f"rows_inserted={apple_run.rows_inserted}"
        )
        if apple_run.status != "ok":
            exit_code = 1
            print(f"[apple] problems:\n{apple_run.notes}", file=sys.stderr)

        # Spotify can be disabled with an env flag while you sort out legal.
        if os.environ.get("DISABLE_SPOTIFY", "").lower() not in ("1", "true", "yes"):
            spotify_run = collect_spotify(db)
            print(
                f"[spotify] status={spotify_run.status} "
                f"charts={spotify_run.charts_collected} "
                f"rows_inserted={spotify_run.rows_inserted}"
            )
            if spotify_run.status != "ok":
                exit_code = 1
                print(f"[spotify] problems:\n{spotify_run.notes}", file=sys.stderr)
        else:
            print("[spotify] skipped (DISABLE_SPOTIFY set)")
    finally:
        db.close()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
