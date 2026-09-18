"""The PodScope Score — a proprietary 0-100 podcast strength metric.

Design goals (per product decisions):
  - 0-100, citable and absolute (a show's 87 doesn't change because another show
    appears). Not a zero-sum rank.
  - 7-day smoothed so it's stable/trustworthy — daily chart noise can't whip it
    around, but a real week-long move shifts it.
  - Only currently-charting shows are scored.

What it fuses (all from data we already have; the COMBINATION is the moat):
  - chart_strength : best rank across all (platform,country,chart) today, on a
        curve where #1 is worth vastly more than #20 (log decay). This is the
        dominant term.
  - breadth        : how many distinct markets (countries) it charts in — rewards
        international reach a single-country chart can't show.
  - consensus      : charts on BOTH Apple and Spotify > one platform. Unique to
        our cross-platform merge; a single-platform scraper can't compute it.
  - trajectory     : 7-day rank movement. Climbing adds, fading subtracts. Needs
        our historical time-series — un-scrapeable from today's charts alone.
  - durability     : how many of the last 7 days the show charted at all.

The inputs are publishable ("based on cross-market rank, trajectory, platform
consensus and durability"); the WEIGHTS below are the proprietary part — credible
and explainable, but not trivially reverse-engineered back to "it's the US rank".

Weights are tuned so a show dominant in one market still scores well, but
cross-market + cross-platform + climbing shows score highest — which is exactly
the profile a media buyer cares about.
"""

import math
from datetime import date, timedelta

from sqlalchemy import select, func

from app.db.models import ChartSnapshot

# ---- weights (the proprietary bit) -------------------------------------------
W_STRENGTH = 0.45
W_BREADTH = 0.15
W_CONSENSUS = 0.15
W_TRAJECTORY = 0.15
W_DURABILITY = 0.10
# smoothing window
SMOOTH_DAYS = 7


def _rank_to_points(rank: int) -> float:
    """A single rank -> 0..1. #1 ≈ 1.0, decaying logarithmically. A top-200 chart
    still gives credit at the bottom, but the top is worth far more."""
    # log curve: rank 1 -> 1.0, 10 -> ~0.63, 50 -> ~0.35, 200 -> ~0.13
    return max(0.0, 1.0 - math.log10(rank) / math.log10(400))


def compute_raw_score(db, show_id: int, on_date: date) -> dict | None:
    """Compute today's UNSMOOTHED score (0-100) + components for one show.
    Returns None if the show didn't chart on/near on_date."""
    # latest snapshots for this show on the given date
    snaps = db.execute(
        select(ChartSnapshot.platform, ChartSnapshot.country,
               ChartSnapshot.chart, ChartSnapshot.rank)
        .where(ChartSnapshot.show_id == show_id,
               ChartSnapshot.captured_date == on_date)
    ).all()
    if not snaps:
        return None

    ranks = [s.rank for s in snaps]
    platforms = {s.platform for s in snaps}
    countries = {s.country for s in snaps}

    # 1) chart strength: reward the BEST placement most, plus some credit for
    #    depth (charting well in several places). Use best rank + mean of top 3.
    pts = sorted((_rank_to_points(r) for r in ranks), reverse=True)
    best = pts[0]
    depth = sum(pts[:3]) / 3  # average of top-3 placements
    chart_strength = 0.7 * best + 0.3 * depth        # 0..1

    # 2) breadth: distinct markets, saturating (6 markets tracked now)
    breadth = min(1.0, len(countries) / 5.0)          # 5+ markets => full

    # 3) consensus: both platforms > one
    consensus = 1.0 if len(platforms) >= 2 else 0.4

    # 4) trajectory: compare best-rank now vs ~7 days ago (improvement = up)
    trajectory, trend = _trajectory(db, show_id, on_date, ranks)

    # 5) durability: fraction of last 7 days the show charted at all
    durability = _durability(db, show_id, on_date)

    raw = 100.0 * (
        W_STRENGTH * chart_strength
        + W_BREADTH * breadth
        + W_CONSENSUS * consensus
        + W_TRAJECTORY * trajectory
        + W_DURABILITY * durability
    )
    raw = round(max(0.0, min(100.0, raw)), 1)

    return {
        "raw_score": raw,
        "trend": trend,
        "components": {
            "chart_strength": round(chart_strength, 3),
            "breadth": round(breadth, 3),
            "consensus": round(consensus, 3),
            "trajectory": round(trajectory, 3),
            "durability": round(durability, 3),
            "markets": len(countries),
            "platforms": len(platforms),
            "best_rank": min(ranks),
        },
    }


def _trajectory(db, show_id, on_date, today_ranks) -> tuple[float, str]:
    """0..1 where 0.5 = flat, >0.5 improving, <0.5 declining. Compares today's
    best rank against the best rank AROUND 7 days ago (a 2-day window at the far
    end, so a steady climb registers instead of being masked by yesterday's rank).
    Needs history; returns neutral if none."""
    window_start = on_date - timedelta(days=SMOOTH_DAYS + 1)   # ~8 days ago
    window_end = on_date - timedelta(days=SMOOTH_DAYS - 1)     # ~6 days ago
    prior_best = db.scalar(
        select(func.min(ChartSnapshot.rank))
        .where(ChartSnapshot.show_id == show_id,
               ChartSnapshot.captured_date >= window_start,
               ChartSnapshot.captured_date <= window_end)
    )
    today_best = min(today_ranks)
    if not prior_best:
        return 0.5, "flat"          # no history that far back -> neutral
    delta = prior_best - today_best  # positive => moved up
    traj = 0.5 + max(-0.5, min(0.5, delta / 40.0))
    trend = "up" if delta > 2 else ("down" if delta < -2 else "flat")
    return traj, trend


def _durability(db, show_id, on_date) -> float:
    """Fraction of the last 7 days on which the show charted at all."""
    start = on_date - timedelta(days=SMOOTH_DAYS - 1)
    days = db.scalar(
        select(func.count(func.distinct(ChartSnapshot.captured_date)))
        .where(ChartSnapshot.show_id == show_id,
               ChartSnapshot.captured_date >= start,
               ChartSnapshot.captured_date <= on_date)
    ) or 0
    return min(1.0, days / SMOOTH_DAYS)
