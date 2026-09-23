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
    """Distinct brands with how many episodes they appear in (headline 'N deals').
    Includes a slug so the UI can link each brand to its brand page."""
    stmt = (
        select(DetectedDeal.brand, DetectedDeal.brand_norm, func.count(DetectedDeal.id))
        .where(DetectedDeal.show_id == show_id,
               DetectedDeal.deal_type == "host_read")
        .group_by(DetectedDeal.brand, DetectedDeal.brand_norm)
        .order_by(func.count(DetectedDeal.id).desc())
    )
    return [{"brand": b, "slug": _brand_slug(bn), "count": c}
            for b, bn, c in db.execute(stmt)]


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


# ---------- OP3 verified downloads ----------
from app.db.enrich_models import OP3Metrics  # noqa: E402


def show_op3(db, show_id: int):
    row = db.get(OP3Metrics, show_id)
    if not row or not row.measured:
        return None
    return {
        "recent_month": row.recent_month_downloads,
        "monthly_avg": row.monthly_avg_downloads,
        "weekly_avg": row.weekly_avg_downloads,
        "months_measured": row.months_measured,
    }


# ---------- Brand pages (sponsor pivot) ----------
def _brand_slug(brand_norm: str) -> str:
    import re
    return re.sub(r"[^a-z0-9]+", "-", (brand_norm or "").lower()).strip("-")


def top_brands(db, limit: int = 100, min_shows: int = 1):
    """Most-active sponsors: brands grouped by normalized key, counting distinct
    shows they sponsor. Powers a brand directory + is the basis for each brand
    page. Picks the most common display spelling per normalized brand."""
    rows = db.execute(
        select(DetectedDeal.brand_norm,
               func.count(func.distinct(DetectedDeal.show_id)).label("shows"),
               func.count(DetectedDeal.id).label("deals"))
        .where(DetectedDeal.deal_type == "host_read")
        .group_by(DetectedDeal.brand_norm)
        .having(func.count(func.distinct(DetectedDeal.show_id)) >= min_shows)
        .order_by(func.count(func.distinct(DetectedDeal.show_id)).desc())
        .limit(limit)
    ).all()
    out = []
    for brand_norm, shows, deals in rows:
        out.append({
            "brand_norm": brand_norm,
            "slug": _brand_slug(brand_norm),
            "display": _brand_display(db, brand_norm),
            "shows": shows,
            "deals": deals,
        })
    return out


def _brand_display(db, brand_norm: str) -> str:
    """The most common human spelling for a normalized brand key."""
    row = db.execute(
        select(DetectedDeal.brand, func.count(DetectedDeal.id).label("n"))
        .where(DetectedDeal.brand_norm == brand_norm)
        .group_by(DetectedDeal.brand)
        .order_by(func.count(DetectedDeal.id).desc())
        .limit(1)
    ).first()
    return row[0] if row else brand_norm


def get_brand(db, slug: str):
    """Resolve a brand page by slug: display name + every show it sponsors,
    ranked by PodScope reach. Returns None if unknown."""
    # find the brand_norm whose slug matches
    candidates = db.scalars(
        select(DetectedDeal.brand_norm).distinct()
    ).all()
    brand_norm = next((b for b in candidates if _brand_slug(b) == slug), None)
    if not brand_norm:
        return None

    display = _brand_display(db, brand_norm)
    # shows this brand sponsors, best PodScope first
    rows = db.execute(
        select(Show, PodScopeScore,
               func.count(DetectedDeal.id).label("deals"),
               func.max(DetectedDeal.promo_code).label("promo_code"))
        .join(DetectedDeal, DetectedDeal.show_id == Show.id)
        .outerjoin(PodScopeScore, PodScopeScore.show_id == Show.id)
        .where(DetectedDeal.brand_norm == brand_norm,
               DetectedDeal.deal_type == "host_read")
        .group_by(Show.id, PodScopeScore.show_id)
        .order_by(func.coalesce(PodScopeScore.score, 0).desc())
    ).all()
    shows = []
    for show, score, deals, promo in rows:
        shows.append({
            "slug": show.slug, "name": show.name, "publisher": show.publisher,
            "artwork_url": show.artwork_url,
            "podscope_score": score.score if score else None,
            "deals": deals, "promo_code": promo,
        })
    return {"display": display, "slug": slug, "brand_norm": brand_norm,
            "shows": shows, "show_count": len(shows)}


# ---------- richer show page: episodes, links, host ----------
def show_recent_episodes(db, show_id: int, limit: int = 8):
    from app.db.enrich_models import Episode
    rows = db.scalars(
        select(Episode).where(Episode.show_id == show_id)
        .order_by(Episode.published.desc().nullslast())
        .limit(limit)
    ).all()
    out = []
    for e in rows:
        desc = (e.description or "").strip()
        out.append({
            "title": e.title,
            "published": e.published.isoformat() if e.published else None,
            "blurb": (desc[:280] + "…") if len(desc) > 280 else desc,
        })
    return out


def show_links(show) -> dict:
    """Deep links to the show on each platform. Apple/Spotify from stored IDs;
    YouTube as a search link (we don't store a channel id)."""
    import urllib.parse
    links = {}
    if show.apple_id:
        links["apple"] = f"https://podcasts.apple.com/podcast/id{show.apple_id}"
    if show.spotify_id:
        links["spotify"] = f"https://open.spotify.com/show/{show.spotify_id}"
    q = urllib.parse.quote(show.name or "")
    links["youtube"] = f"https://www.youtube.com/results?search_query={q}+podcast"
    return links


def show_about(show) -> dict:
    return {"host_name": show.host_name, "about": show.about}


# ---------- review-origin geography ----------
from app.db.enrich_models import ReviewGeo  # noqa: E402

_CC_NAMES = {"us": "United States", "ca": "Canada", "gb": "United Kingdom",
             "au": "Australia", "de": "Germany", "fr": "France"}


def show_review_geo(db, show_id: int):
    """Audience-geography signal: share of recent Apple reviews per storefront.
    Returns [{country, name, count, pct}] sorted desc, or None if no data."""
    rows = db.execute(
        select(ReviewGeo.country, ReviewGeo.review_count, ReviewGeo.avg_rating)
        .where(ReviewGeo.show_id == show_id, ReviewGeo.country != "_none",
               ReviewGeo.review_count > 0)
    ).all()
    if not rows:
        return None
    total = sum(c for _, c, _ in rows)
    if total == 0:
        return None
    out = [{
        "country": cc,
        "name": _CC_NAMES.get(cc, cc.upper()),
        "count": c,
        "pct": round(100 * c / total),
        "avg_rating": rating,
    } for cc, c, rating in rows]
    out.sort(key=lambda r: r["count"], reverse=True)
    return out
