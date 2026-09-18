"""Read-side queries. Kept separate from the web layer so they're testable."""

from datetime import date, timedelta

from sqlalchemy import select, func, distinct

from app.db.models import Show, ChartSnapshot


def search_shows(db, q: str, limit: int = 20):
    q = (q or "").strip()
    if not q:
        return []
    stmt = (
        select(Show)
        .where(Show.name.ilike(f"%{q}%"))
        .order_by(Show.last_seen.desc())
        .limit(limit)
    )
    return list(db.scalars(stmt))


def get_show_by_slug(db, slug: str) -> Show | None:
    return db.scalar(select(Show).where(Show.slug == slug))


def show_current_positions(db, show_id: int):
    """Latest rank for this show in every (platform, country, chart) it appears."""
    latest_date = db.scalar(
        select(func.max(ChartSnapshot.captured_date))
        .where(ChartSnapshot.show_id == show_id)
    )
    if not latest_date:
        return []
    stmt = (
        select(ChartSnapshot)
        .where(
            ChartSnapshot.show_id == show_id,
            ChartSnapshot.captured_date == latest_date,
        )
        .order_by(ChartSnapshot.platform, ChartSnapshot.country,
                  ChartSnapshot.rank)
    )
    return list(db.scalars(stmt))


def show_history(db, show_id: int, platform: str, country: str, chart: str,
                 days: int = 90):
    since = date.today() - timedelta(days=days)
    stmt = (
        select(ChartSnapshot.captured_date, ChartSnapshot.rank)
        .where(
            ChartSnapshot.show_id == show_id,
            ChartSnapshot.platform == platform,
            ChartSnapshot.country == country,
            ChartSnapshot.chart == chart,
            ChartSnapshot.captured_date >= since,
        )
        .order_by(ChartSnapshot.captured_date)
    )
    return [{"date": d.isoformat(), "rank": r} for d, r in db.execute(stmt)]


def show_geo_footprint(db, show_id: int):
    """Which markets does this show chart in right now? The geo angle in miniature:
    a show's current best rank per country, across all charts/platforms.
    """
    latest_date = db.scalar(
        select(func.max(ChartSnapshot.captured_date))
        .where(ChartSnapshot.show_id == show_id)
    )
    if not latest_date:
        return []
    stmt = (
        select(ChartSnapshot.country, func.min(ChartSnapshot.rank))
        .where(
            ChartSnapshot.show_id == show_id,
            ChartSnapshot.captured_date == latest_date,
        )
        .group_by(ChartSnapshot.country)
        .order_by(func.min(ChartSnapshot.rank))
    )
    return [{"country": c, "best_rank": r} for c, r in db.execute(stmt)]


def platform_stats(db):
    """For the homepage: how much data have we accumulated?"""
    total_snaps = db.scalar(select(func.count(ChartSnapshot.id))) or 0
    total_shows = db.scalar(select(func.count(Show.id))) or 0
    days_tracked = db.scalar(
        select(func.count(distinct(ChartSnapshot.captured_date)))
    ) or 0
    return {
        "snapshots": total_snaps,
        "shows": total_shows,
        "days_tracked": days_tracked,
    }


# ---------- enricher read queries (sponsors + guest) ----------
from app.db.enrich_models import DetectedDeal, GuestProfile, Episode  # noqa: E402


def show_deals(db, show_id: int, limit: int = 12):
    """Recent detected deals for a show, newest episode first, with the episode
    title. Only host_read + medium/high confidence count as headline 'deals';
    everything else is available but flagged."""
    stmt = (
        select(DetectedDeal, Episode.title, Episode.published)
        .join(Episode, DetectedDeal.episode_id == Episode.id)
        .where(DetectedDeal.show_id == show_id)
        .order_by(Episode.published.desc().nullslast())
        .limit(limit)
    )
    out = []
    for deal, title, published in db.execute(stmt):
        out.append({
            "brand": deal.brand,
            "deal_type": deal.deal_type,
            "confidence": deal.confidence,
            "promo_code": deal.promo_code,
            "promo_url": deal.promo_url,
            "evidence": deal.evidence,
            "episode_title": title,
            "published": published.isoformat() if published else None,
        })
    return out


def show_brands(db, show_id: int):
    """Distinct brands with how many episodes they appear in (headline 'N deals')."""
    stmt = (
        select(DetectedDeal.brand, func.count(DetectedDeal.id))
        .where(DetectedDeal.show_id == show_id,
               DetectedDeal.deal_type == "host_read")
        .group_by(DetectedDeal.brand)
        .order_by(func.count(DetectedDeal.id).desc())
    )
    return [{"brand": b, "count": c} for b, c in db.execute(stmt)]


def show_guest_profile(db, show_id: int):
    p = db.get(GuestProfile, show_id)
    if not p:
        return None
    return {
        "books_guests": p.books_guests,
        "guest_frequency": p.guest_frequency,
        "topics": p.topics,
        "format_note": p.format_note,
        "suitability_note": p.suitability_note,
        "has_contact": bool(p.contact_email),  # gate the actual email behind Pro
    }


# ---------- collector health ----------
from app.db.models import CollectionRun  # noqa: E402


def collector_health(db):
    """Last run per platform: status, when, rows, and any logged problems.
    Powers /health/collector so 'is the cron alive?' is a URL, not a log dig."""
    from datetime import datetime, timezone

    platforms = ["apple", "spotify"]
    out = {"platforms": {}, "overall": "unknown"}
    healthy = []
    for plat in platforms:
        run = db.scalar(
            select(CollectionRun)
            .where(CollectionRun.platform == plat)
            .order_by(CollectionRun.started_at.desc())
            .limit(1)
        )
        if not run:
            out["platforms"][plat] = {"status": "never_run"}
            healthy.append(False)
            continue
        finished = run.finished_at or run.started_at
        age_hours = None
        if finished:
            age_hours = round(
                (datetime.utcnow() - finished).total_seconds() / 3600, 1
            )
        # "fresh" = last successful-ish run within ~7h (cron is every 6h)
        is_fresh = (age_hours is not None and age_hours <= 7
                    and run.rows_inserted > 0)
        out["platforms"][plat] = {
            "status": run.status,
            "last_run": run.started_at.isoformat() if run.started_at else None,
            "finished": finished.isoformat() if finished else None,
            "age_hours": age_hours,
            "rows_inserted": run.rows_inserted,
            "charts_collected": run.charts_collected,
            "fresh": is_fresh,
            "problems": run.notes or None,
        }
        healthy.append(is_fresh)

    if all(healthy):
        out["overall"] = "ok"
    elif any(healthy):
        out["overall"] = "degraded"
    else:
        out["overall"] = "down"
    return out


# ---------- PodScope Score ----------
import json as _json  # noqa: E402
from app.db.models import PodScopeScore, ScoreHistory  # noqa: E402


def show_score(db, show_id: int):
    row = db.get(PodScopeScore, show_id)
    if not row:
        return None
    comps = {}
    if row.components:
        try:
            comps = _json.loads(row.components)
        except Exception:
            comps = {}
    return {
        "score": row.score,
        "trend": row.trend,
        "components": comps,
        "computed_at": row.computed_at.isoformat() if row.computed_at else None,
    }


def top_scored_shows(db, limit: int = 100):
    """The PodScope Top Chart — shows ranked by our score. Powers the homepage."""
    stmt = (
        select(Show, PodScopeScore)
        .join(PodScopeScore, PodScopeScore.show_id == Show.id)
        .order_by(PodScopeScore.score.desc())
        .limit(limit)
    )
    out = []
    for pos, (show, score) in enumerate(db.execute(stmt), start=1):
        out.append({
            "position": pos,
            "slug": show.slug,
            "name": show.name,
            "publisher": show.publisher,
            "artwork_url": show.artwork_url,
            "score": score.score,
            "trend": score.trend,
        })
    return out
