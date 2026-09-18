"""Compute PodScope Scores for all currently-charting shows.

Run on a cron (after the collector, so it scores fresh chart data):
    python -m app.score.run

Each run:
  1. finds shows that charted on the latest collection date
  2. computes today's raw score (engine.compute_raw_score) and appends it to
     score_history
  3. computes the 7-day SMOOTHED score = mean of the last 7 daily raw scores,
     and upserts it into podscope_scores (what the UI reads)

Smoothing needs a few days of history to matter; until then the smoothed score
≈ the raw score, which is fine.
"""

import json
from datetime import date, timedelta

from sqlalchemy import select, func
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.db.session import SessionLocal
from app.db.init_db import init_db
from app.db.models import ChartSnapshot, PodScopeScore, ScoreHistory
from app.score.engine import compute_raw_score, SMOOTH_DAYS


def _charting_show_ids(db, on_date):
    return db.scalars(
        select(ChartSnapshot.show_id)
        .where(ChartSnapshot.captured_date == on_date)
        .group_by(ChartSnapshot.show_id)
    ).all()


def _write_history(db, show_id, raw_score, on_date):
    dialect = db.bind.dialect.name
    if dialect == "postgresql":
        stmt = (
            pg_insert(ScoreHistory)
            .values(show_id=show_id, raw_score=raw_score, captured_date=on_date)
            .on_conflict_do_update(
                constraint="uq_score_daily",
                set_={"raw_score": raw_score},
            )
        )
        db.execute(stmt)
    else:
        row = db.scalar(select(ScoreHistory).where(
            ScoreHistory.show_id == show_id,
            ScoreHistory.captured_date == on_date))
        if row:
            row.raw_score = raw_score
        else:
            db.add(ScoreHistory(show_id=show_id, raw_score=raw_score,
                                captured_date=on_date))


def _smoothed(db, show_id, on_date) -> float:
    start = on_date - timedelta(days=SMOOTH_DAYS - 1)
    avg = db.scalar(
        select(func.avg(ScoreHistory.raw_score))
        .where(ScoreHistory.show_id == show_id,
               ScoreHistory.captured_date >= start,
               ScoreHistory.captured_date <= on_date)
    )
    return round(float(avg), 1) if avg is not None else 0.0


def _upsert_score(db, show_id, smoothed, raw, trend, components):
    row = db.get(PodScopeScore, show_id)
    if row is None:
        row = PodScopeScore(show_id=show_id)
        db.add(row)
    row.score = smoothed
    row.raw_score = raw
    row.trend = trend
    row.components = json.dumps(components)
    from datetime import datetime
    row.computed_at = datetime.utcnow()


def compute_all(db) -> dict:
    latest = db.scalar(select(func.max(ChartSnapshot.captured_date)))
    if not latest:
        return {"scored": 0, "latest": None}
    show_ids = _charting_show_ids(db, latest)
    scored = 0
    for sid in show_ids:
        result = compute_raw_score(db, sid, latest)
        if not result:
            continue
        _write_history(db, sid, result["raw_score"], latest)
        db.flush()
        smoothed = _smoothed(db, sid, latest)
        _upsert_score(db, sid, smoothed, result["raw_score"],
                      result["trend"], result["components"])
        scored += 1
        if scored % 200 == 0:
            db.commit()
    db.commit()
    return {"scored": scored, "latest": latest.isoformat()}


def main():
    init_db()
    db = SessionLocal()
    try:
        r = compute_all(db)
        print(f"[score] computed PodScope Scores: {r['scored']} shows "
              f"(chart date {r['latest']})")
    finally:
        db.close()


if __name__ == "__main__":
    main()
