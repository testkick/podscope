"""Collect Apple review-origin geography for shows, store per-storefront counts.

    python -m app.reviews.sync            # charting shows not checked recently
    python -m app.reviews.sync --all      # all shows with an apple_id

Only shows with an apple_id can be checked (the feed is keyed by Apple ID).
Idempotent: skips shows checked within REVIEW_REFRESH_DAYS. Rate-paced.
"""

import os
import sys
from datetime import datetime, timedelta

from sqlalchemy import select, func

from app.db.session import SessionLocal
from app.db.init_db import init_db
from app.db.models import Show, ChartSnapshot
from app.db.enrich_models import ReviewGeo
from app.reviews.collector import collect_reviews_for_show

MAX_PER_RUN = int(os.environ.get("REVIEW_MAX", "150"))
REFRESH_DAYS = int(os.environ.get("REVIEW_REFRESH_DAYS", "30"))


def _candidates(db, all_shows, limit):
    cutoff = datetime.utcnow() - timedelta(days=REFRESH_DAYS)
    recently = select(ReviewGeo.show_id).where(ReviewGeo.checked_at >= cutoff)
    q = select(Show).where(Show.apple_id.is_not(None), Show.id.not_in(recently))
    if not all_shows:
        latest = db.scalar(select(func.max(ChartSnapshot.captured_date)))
        if latest:
            charting = select(ChartSnapshot.show_id).where(
                ChartSnapshot.captured_date == latest)
            q = q.where(Show.id.in_(charting))
    return db.scalars(q.limit(limit)).all()


def sync(db, all_shows=False, limit=MAX_PER_RUN) -> dict:
    shows = _candidates(db, all_shows, limit)
    checked = with_reviews = 0
    for show in shows:
        rows = collect_reviews_for_show(show.apple_id)
        # replace this show's rows
        db.query(ReviewGeo).filter(ReviewGeo.show_id == show.id).delete()
        for r in rows:
            db.add(ReviewGeo(show_id=show.id, country=r["country"],
                             review_count=r["review_count"],
                             avg_rating=r["avg_rating"],
                             checked_at=datetime.utcnow()))
        if rows:
            with_reviews += 1
        else:
            # record a checked marker so we don't re-poll every run
            db.add(ReviewGeo(show_id=show.id, country="_none",
                             review_count=0, checked_at=datetime.utcnow()))
        checked += 1
        if checked % 25 == 0:
            db.commit()
    db.commit()
    return {"checked": checked, "with_reviews": with_reviews}


def main():
    all_shows = "--all" in sys.argv
    init_db()
    db = SessionLocal()
    try:
        r = sync(db, all_shows=all_shows)
        print(f"[reviews] {r}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
