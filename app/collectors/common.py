"""Shared helpers for collectors: HTTP client, slugging, idempotent upserts."""

import re
from datetime import date, datetime

import httpx
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError

from app.db.models import Show, ChartSnapshot

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
TIMEOUT = httpx.Timeout(20.0, connect=10.0)


def client() -> httpx.Client:
    return httpx.Client(
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        timeout=TIMEOUT,
        follow_redirects=True,
    )


def slugify(name: str, external_id: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", (name or "show").lower()).strip("-")[:80]
    return f"{base}-{external_id}"


def upsert_show(db, *, apple_id=None, spotify_id=None, name, publisher=None,
                artwork_url=None) -> Show:
    """Find a show by platform id, or create it. Returns the Show row.

    We match on whichever platform id we have. Cross-platform identity merging
    (same show on Apple AND Spotify) is intentionally NOT done here — see
    models.py. This keeps the collector simple and deterministic.
    """
    show = None
    if apple_id:
        show = db.scalar(select(Show).where(Show.apple_id == apple_id))
    if show is None and spotify_id:
        show = db.scalar(select(Show).where(Show.spotify_id == spotify_id))

    if show is None:
        ext = apple_id or spotify_id or name
        show = Show(
            apple_id=apple_id,
            spotify_id=spotify_id,
            name=name,
            publisher=publisher,
            artwork_url=artwork_url,
            slug=slugify(name, str(ext)),
            first_seen=datetime.utcnow(),
            last_seen=datetime.utcnow(),
        )
        db.add(show)
        db.flush()  # get show.id
    else:
        # Backfill any newly-available fields.
        show.last_seen = datetime.utcnow()
        if apple_id and not show.apple_id:
            show.apple_id = apple_id
        if spotify_id and not show.spotify_id:
            show.spotify_id = spotify_id
        if artwork_url and not show.artwork_url:
            show.artwork_url = artwork_url
        if publisher and not show.publisher:
            show.publisher = publisher
    return show


def insert_snapshot(db, *, show_id, platform, country, chart, rank,
                    captured_date=None) -> bool:
    """Insert one chart snapshot. Returns True if a new row was written,
    False if it already existed (idempotent daily re-runs).

    Uses ON CONFLICT DO NOTHING on Postgres; falls back to try/except on SQLite.
    """
    captured_date = captured_date or date.today()
    dialect = db.bind.dialect.name

    if dialect == "postgresql":
        stmt = (
            pg_insert(ChartSnapshot)
            .values(
                show_id=show_id, platform=platform, country=country,
                chart=chart, rank=rank,
                captured_at=datetime.utcnow(), captured_date=captured_date,
            )
            .on_conflict_do_nothing(constraint="uq_snapshot_daily")
        )
        result = db.execute(stmt)
        return result.rowcount > 0

    # SQLite / other: attempt insert, swallow the unique violation.
    snap = ChartSnapshot(
        show_id=show_id, platform=platform, country=country,
        chart=chart, rank=rank,
        captured_at=datetime.utcnow(), captured_date=captured_date,
    )
    db.add(snap)
    try:
        db.flush()
        return True
    except IntegrityError:
        db.rollback()
        return False
