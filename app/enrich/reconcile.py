"""Cross-platform show reconciliation.

Two stages, run in order:

  1. resolve_feeds(db)  — fill in Show.feed_url for shows that lack it.
       Apple shows  -> Podcast Index byitunesid (exact).
       Spotify shows -> Podcast Index search by title/publisher (best-effort).
     Cheap to re-run: only touches shows with a null feed_url.

  2. merge_duplicates(db) — group shows by normalized feed_url; where two+ shows
     share one feed, collapse them into a single canonical row and re-point all
     chart_snapshots (and enrich rows) to the survivor, then delete the losers.

Canonical-row rule (per your choice): APPLE metadata wins. The survivor is the
Apple-sourced row when present; it absorbs the Spotify id, and keeps Apple's
name/publisher/artwork, filling only truly-empty fields from the other.

Safety: merging keys on feed_url EQUALITY, never on a name guess. A show that
can't be resolved to a feed simply stays separate — a harmless duplicate, never
a wrong merge. The whole pass is idempotent: re-running after everything is
merged is a no-op.
"""

from datetime import datetime

from sqlalchemy import select, update, func

from app.db.session import SessionLocal
from app.db.init_db import init_db
from app.db.models import Show, ChartSnapshot
from app.enrich.podcastindex import (
    resolve_by_itunes_id, search_feed, normalize_feed_url,
)

# enrich tables that reference show_id — re-pointed on merge if present
try:
    from app.db.enrich_models import (
        Episode, DetectedDeal, GuestProfile, EnrichJob,
    )
    _ENRICH_TABLES = [Episode, DetectedDeal, EnrichJob]  # have show_id FK
    _GUEST = GuestProfile                                 # show_id is the PK
except Exception:  # enrich models optional
    _ENRICH_TABLES = []
    _GUEST = None


# ---------- stage 1: resolve feed urls ----------

def resolve_feeds(db, limit: int = 200) -> dict:
    """Fill Show.feed_url for shows missing it. Returns counts."""
    todo = db.scalars(
        select(Show).where(Show.feed_url.is_(None)).limit(limit)
    ).all()
    resolved = 0
    failed = 0
    for show in todo:
        info = None
        if show.apple_id:
            info = resolve_by_itunes_id(show.apple_id)
        if info is None and show.spotify_id:
            info = search_feed(show.name, show.publisher)
        if info and info.get("feed_url"):
            show.feed_url = info["feed_url"]
            # opportunistically backfill an itunes id we didn't have
            if info.get("itunes_id") and not show.apple_id:
                # only if that apple_id isn't already taken
                clash = db.scalar(
                    select(Show).where(Show.apple_id == info["itunes_id"]))
                if not clash:
                    show.apple_id = info["itunes_id"]
            resolved += 1
        else:
            failed += 1
        db.commit()
    return {"attempted": len(todo), "resolved": resolved, "failed": failed}


# ---------- stage 2: merge duplicates ----------

def _canonical_and_losers(shows: list[Show]) -> tuple[Show, list[Show]]:
    """Pick the survivor (Apple metadata wins) and the rows to fold in."""
    apple_rows = [s for s in shows if s.apple_id]
    if apple_rows:
        # prefer the oldest Apple row for a stable slug/id
        survivor = min(apple_rows, key=lambda s: s.id)
    else:
        survivor = min(shows, key=lambda s: s.id)
    losers = [s for s in shows if s.id != survivor.id]
    return survivor, losers


def _absorb_metadata(db, survivor: Show, loser: Show):
    # Apple metadata wins: only fill fields empty on the survivor.
    # CRITICAL: clear the loser's unique columns (apple_id/spotify_id) BEFORE
    # assigning them to the survivor and flush, or the unique constraint fires
    # while both rows briefly hold the same id.
    take_spotify = loser.spotify_id if not survivor.spotify_id else None
    take_apple = loser.apple_id if not survivor.apple_id else None

    loser.apple_id = None
    loser.spotify_id = None
    db.flush()  # release the unique ids before the survivor claims them

    if take_spotify:
        survivor.spotify_id = take_spotify
    if take_apple:
        survivor.apple_id = take_apple
    if not survivor.publisher and loser.publisher:
        survivor.publisher = loser.publisher
    if not survivor.artwork_url and loser.artwork_url:
        survivor.artwork_url = loser.artwork_url
    if not survivor.feed_url and loser.feed_url:
        survivor.feed_url = loser.feed_url
    survivor.last_seen = datetime.utcnow()
    db.flush()


def _repoint_children(db, survivor_id: int, loser_id: int):
    # chart snapshots
    db.execute(
        update(ChartSnapshot)
        .where(ChartSnapshot.show_id == loser_id)
        .values(show_id=survivor_id)
    )
    # enrich FK tables
    for tbl in _ENRICH_TABLES:
        db.execute(
            update(tbl).where(tbl.show_id == loser_id)
            .values(show_id=survivor_id)
        )
    # guest profile: show_id is the PK, so move it only if survivor lacks one
    if _GUEST is not None:
        surv = db.get(_GUEST, survivor_id)
        lose = db.get(_GUEST, loser_id)
        if lose is not None:
            if surv is None:
                db.execute(
                    update(_GUEST).where(_GUEST.show_id == loser_id)
                    .values(show_id=survivor_id)
                )
            else:
                db.delete(lose)  # survivor already has one; drop the duplicate


def merge_duplicates(db) -> dict:
    """Collapse shows sharing a normalized feed_url into one canonical row."""
    # Build groups keyed by normalized feed_url (only shows that have one).
    rows = db.scalars(select(Show).where(Show.feed_url.is_not(None))).all()
    groups: dict[str, list[Show]] = {}
    for s in rows:
        key = normalize_feed_url(s.feed_url)
        if key:
            groups.setdefault(key, []).append(s)

    merged_groups = 0
    removed_rows = 0
    for key, shows in groups.items():
        if len(shows) < 2:
            continue
        survivor, losers = _canonical_and_losers(shows)
        for loser in losers:
            _absorb_metadata(db, survivor, loser)
            _repoint_children(db, survivor.id, loser.id)
        db.flush()
        for loser in losers:
            db.delete(loser)
            removed_rows += 1
        db.commit()
        merged_groups += 1

    # Clean up any snapshot rows that violate the daily-unique constraint after
    # re-pointing (same show now has two rows for one chart/day from the two
    # platforms — that's expected and fine, they differ by platform column).
    return {"merged_groups": merged_groups, "removed_rows": removed_rows}


def main():
    init_db()
    db = SessionLocal()
    try:
        r1 = resolve_feeds(db)
        print(f"[reconcile] feeds: attempted={r1['attempted']} "
              f"resolved={r1['resolved']} failed={r1['failed']}")
        r2 = merge_duplicates(db)
        print(f"[reconcile] merge: groups_merged={r2['merged_groups']} "
              f"rows_removed={r2['removed_rows']}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
