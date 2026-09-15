"""PodScope web service: public HTML pages (SEO wedge) + JSON API.

Run:  uvicorn app.api.main:app --host 0.0.0.0 --port $PORT
"""

from pathlib import Path
import os

from fastapi import FastAPI, Depends, Request, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db.init_db import init_db
from app.db.session import get_session
from app.api import queries as Q

BASE = Path(__file__).resolve().parent.parent
templates = Jinja2Templates(directory=str(BASE / "templates"))

app = FastAPI(title="PodScope", version="0.1")
app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")


@app.on_event("startup")
def _startup():
    init_db()  # idempotent; ensures tables exist on first web boot
    # Opt-in: set REDETECT_ON_STARTUP=1 to re-run detection over cached text
    # after a deploy, so detector improvements cascade to every show with no
    # manual step. Runs in a background thread so it never blocks web startup.
    # It only re-reads stored text (no fetching), so it's cheap and safe.
    if os.environ.get("REDETECT_ON_STARTUP", "").lower() in ("1", "true", "yes"):
        import threading

        def _bg():
            try:
                from app.db.session import SessionLocal
                from app.enrich.redetect import redetect_all
                db = SessionLocal()
                try:
                    stats = redetect_all(db)
                    print(f"[startup redetect] {stats}")
                finally:
                    db.close()
            except Exception as exc:  # never let this crash the app
                print(f"[startup redetect] skipped: {exc}")

        threading.Thread(target=_bg, daemon=True).start()


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/health/collector")
def health_collector(db: Session = Depends(get_session)):
    """Machine-readable collector status: last run per platform, freshness,
    row counts, and any logged per-chart problems. Use this for uptime pings."""
    return Q.collector_health(db)


@app.get("/status", response_class=HTMLResponse)
def status_page(request: Request, db: Session = Depends(get_session)):
    """Human-readable collector status page. Glance at /status in a browser to
    see whether the cron is alive and when it last wrote data."""
    health = Q.collector_health(db)
    stats = Q.platform_stats(db)
    return templates.TemplateResponse(
        "status.html",
        {"request": request, "health": health, "stats": stats},
    )


# ---------- HTML pages (the SEO surface) ----------

@app.get("/", response_class=HTMLResponse)
def home(request: Request, db: Session = Depends(get_session)):
    stats = Q.platform_stats(db)
    return templates.TemplateResponse(
        "home.html", {"request": request, "stats": stats}
    )


@app.get("/search", response_class=HTMLResponse)
def search(request: Request, q: str = "", db: Session = Depends(get_session)):
    results = Q.search_shows(db, q)
    return templates.TemplateResponse(
        "search.html", {"request": request, "q": q, "results": results}
    )


@app.get("/show/{slug}", response_class=HTMLResponse)
def show_page(slug: str, request: Request, db: Session = Depends(get_session)):
    show = Q.get_show_by_slug(db, slug)
    if not show:
        raise HTTPException(status_code=404, detail="Show not found")
    positions = Q.show_current_positions(db, show.id)
    geo = Q.show_geo_footprint(db, show.id)
    # Pick the most prominent chart (best current rank) to draw a trend line.
    trend = []
    trend_label = None
    if positions:
        top = min(positions, key=lambda p: p.rank)
        trend = Q.show_history(db, show.id, top.platform, top.country, top.chart)
        trend_label = f"{top.platform.title()} · {top.country.upper()} · {top.chart}"
    deals = Q.show_deals(db, show.id)
    brands = Q.show_brands(db, show.id)
    guest = Q.show_guest_profile(db, show.id)
    return templates.TemplateResponse(
        "show.html",
        {
            "request": request, "show": show, "positions": positions,
            "geo": geo, "trend": trend, "trend_label": trend_label,
            "deals": deals, "brands": brands, "guest": guest,
        },
    )


# ---------- JSON API (what the paid product & partners consume) ----------

@app.get("/api/search")
def api_search(q: str = Query(...), db: Session = Depends(get_session)):
    rows = Q.search_shows(db, q)
    return [
        {"slug": s.slug, "name": s.name, "publisher": s.publisher,
         "apple_id": s.apple_id, "spotify_id": s.spotify_id}
        for s in rows
    ]


@app.get("/api/show/{slug}")
def api_show(slug: str, db: Session = Depends(get_session)):
    show = Q.get_show_by_slug(db, slug)
    if not show:
        return JSONResponse({"error": "not found"}, status_code=404)
    positions = Q.show_current_positions(db, show.id)
    return {
        "slug": show.slug,
        "name": show.name,
        "publisher": show.publisher,
        "artwork_url": show.artwork_url,
        "apple_id": show.apple_id,
        "spotify_id": show.spotify_id,
        "current_positions": [
            {"platform": p.platform, "country": p.country,
             "chart": p.chart, "rank": p.rank}
            for p in positions
        ],
        "geo_footprint": Q.show_geo_footprint(db, show.id),
        "brands": Q.show_brands(db, show.id),
        "recent_deals": Q.show_deals(db, show.id),
        "guest_profile": Q.show_guest_profile(db, show.id),
    }


@app.get("/api/show/{slug}/history")
def api_history(slug: str, platform: str, country: str, chart: str,
                days: int = 90, db: Session = Depends(get_session)):
    show = Q.get_show_by_slug(db, slug)
    if not show:
        return JSONResponse({"error": "not found"}, status_code=404)
    return {
        "slug": show.slug, "platform": platform, "country": country,
        "chart": chart, "days": days,
        "history": Q.show_history(db, show.id, platform, country, chart, days),
    }


@app.get("/api/stats")
def api_stats(db: Session = Depends(get_session)):
    return Q.platform_stats(db)
