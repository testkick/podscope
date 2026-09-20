"""Fetch OP3 download data for our shows and measure the OVERLAP.

The overlap number is the whole point of this first step: how many of our
charting shows does OP3 actually measure? That decides whether the calibration
model (chart rank -> real downloads) is worth building. Run:

    python -m app.op3.sync            # charting shows not yet checked
    python -m app.op3.sync --all      # every show with a feed_url
    python -m app.op3.sync --stats    # just print the current overlap, no fetch

We resolve each show in OP3 by its podcast GUID or feed_url. Most shows won't be
measured (no OP3 prefix) — we record measured=False so we don't re-check them
every run. Shows that ARE measured get their download counts stored.
"""

import os
import sys
import time

from sqlalchemy import select, func

from app.db.session import SessionLocal
from app.db.init_db import init_db
from app.db.models import Show, ChartSnapshot
from app.db.enrich_models import OP3Metrics
from app.op3.client import lookup_show, show_downloads, op3_enabled

MAX_PER_RUN = int(os.environ.get("OP3_MAX", "300"))
DELAY = float(os.environ.get("OP3_DELAY", "0.4"))


def _candidate_shows(db, all_shows, limit):
    """Shows to check: have a feed_url, not yet checked in OP3. Charting shows
    first (they matter most), unless --all."""
    already = select(OP3Metrics.show_id)
    q = select(Show).where(Show.feed_url.is_not(None), Show.id.not_in(already))
    if not all_shows:
        latest = db.scalar(select(func.max(ChartSnapshot.captured_date)))
        if latest:
            charting = select(ChartSnapshot.show_id).where(
                ChartSnapshot.captured_date == latest)
            q = q.where(Show.id.in_(charting))
    return db.scalars(q.limit(limit)).all()


def sync(db, all_shows=False, limit=MAX_PER_RUN) -> dict:
    if not op3_enabled():
        return {"error": "OP3 not enabled"}
    shows = _candidate_shows(db, all_shows, limit)
    checked = measured = 0
    for i, show in enumerate(shows):
        if i > 0:
            time.sleep(DELAY)
        op3 = lookup_show(feed_url=show.feed_url)
        row = db.get(OP3Metrics, show.id) or OP3Metrics(show_id=show.id)
        row.checked_at = __import__("datetime").datetime.utcnow()
        if op3 and op3.get("showUuid"):
            dl = show_downloads(op3["showUuid"])
            if dl:
                row.op3_show_uuid = op3["showUuid"]
                row.recent_month_downloads = dl["recent_month"]
                row.monthly_avg_downloads = dl["monthly_avg"]
                row.weekly_avg_downloads = dl["weekly_avg"]
                row.months_measured = dl["months_measured"]
                row.measured = True
                measured += 1
            else:
                row.measured = False
        else:
            row.measured = False
        db.merge(row)
        checked += 1
        if checked % 50 == 0:
            db.commit()
    db.commit()
    return {"checked": checked, "measured": measured}


def overlap_stats(db) -> dict:
    total = db.scalar(select(func.count(OP3Metrics.show_id))) or 0
    measured = db.scalar(
        select(func.count(OP3Metrics.show_id)).where(OP3Metrics.measured.is_(True))
    ) or 0
    charting = db.scalar(
        select(func.count(func.distinct(ChartSnapshot.show_id)))
    ) or 0
    pct = round(100 * measured / total, 1) if total else 0
    return {"checked_so_far": total, "measured_by_op3": measured,
            "measured_pct_of_checked": pct, "total_charting_shows": charting}


def main():
    all_shows = "--all" in sys.argv
    stats_only = "--stats" in sys.argv
    init_db()
    db = SessionLocal()
    try:
        if stats_only:
            print(f"[op3] overlap: {overlap_stats(db)}")
            return
        r = sync(db, all_shows=all_shows)
        print(f"[op3] {r}")
        print(f"[op3] overlap so far: {overlap_stats(db)}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
