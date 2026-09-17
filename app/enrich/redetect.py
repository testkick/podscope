"""Re-run detection over ALREADY-STORED data — no network, no fetching, no cost.

This is the cheap half of enrichment, split out so improving a detector
(sponsor patterns, guest patterns) can cascade across the WHOLE catalog in one
batch. It reads the text we already fetched (episode_texts) and the episode
titles we already stored (episodes), re-runs the current detection logic, and
overwrites the results.

Contrast with app.enrich.run (the fetch layer): that hits RSS feeds, is
rate-limited, and is job-based so it runs once per show. Redetect touches no
network — run it freely, every time you tune a pattern.

Usage:
    python -m app.enrich.redetect                 # whole catalog
    python -m app.enrich.redetect --show <slug>   # one show
    python -m app.enrich.redetect --kind sponsors # just sponsors (or guest)

Deploy hook: safe to run automatically after each deploy so detector
improvements refresh every show without manual steps.
"""

import sys

from sqlalchemy import select, delete

from app.db.session import SessionLocal
from app.db.init_db import init_db
from app.db.models import Show
from app.db.enrich_models import Episode, EpisodeText, DetectedDeal, GuestProfile
from app.enrich.sponsors import detect_sponsors
from app.enrich.guest import analyze_guest


def _shows_with_text(db, slug=None):
    """Shows that have at least one stored episode text (i.e. already fetched)."""
    q = select(Show).where(
        Show.id.in_(
            select(Episode.show_id)
            .join(EpisodeText, EpisodeText.episode_id == Episode.id)
            .distinct()
        )
    )
    if slug:
        q = q.where(Show.slug == slug)
    return db.scalars(q).all()


def redetect_sponsors(db, show: Show) -> int:
    """Re-run sponsor detection from stored episode_texts for one show.
    Replaces this show's DetectedDeal rows with fresh results."""
    # gather (episode_id, text) we already have
    rows = db.execute(
        select(Episode.id, EpisodeText.text)
        .join(EpisodeText, EpisodeText.episode_id == Episode.id)
        .where(Episode.show_id == show.id)
    ).all()
    if not rows:
        return 0
    # clear old deals for this show, then re-detect from cached text
    db.execute(delete(DetectedDeal).where(DetectedDeal.show_id == show.id))
    total = 0
    for episode_id, text in rows:
        if not text:
            continue
        for d in detect_sponsors(text):
            db.add(DetectedDeal(
                show_id=show.id, episode_id=episode_id,
                brand=d["brand"], brand_norm=d["brand_norm"],
                promo_code=d.get("promo_code"), promo_url=d.get("promo_url"),
                deal_type=d["deal_type"], confidence=d["confidence"],
                evidence=d.get("evidence")))
            total += 1
    db.flush()
    return total


def redetect_guest(db, show: Show) -> bool:
    """Re-run guest analysis from stored episode titles/descriptions for one show.
    Updates the GuestProfile in place, preserving the stored contact_email."""
    episodes = db.scalars(
        select(Episode).where(Episode.show_id == show.id)
        .order_by(Episode.published.desc().nullslast())
    ).all()
    if not episodes:
        return False
    eps = [{"title": e.title or "", "description": e.description or ""}
           for e in episodes]
    g = analyze_guest(show.name, eps, publisher=show.publisher)

    prof = db.get(GuestProfile, show.id)
    if prof is None:
        prof = GuestProfile(show_id=show.id)
        db.add(prof)
    prof.books_guests = g["books_guests"]
    prof.guest_frequency = g.get("guest_frequency")
    prof.topics = g.get("topics")
    prof.format_note = g.get("format_note")
    prof.suitability_note = g.get("suitability_note")
    prof.recent_guests = g.get("recent_guests")
    # contact_email came from the feed at fetch time — keep whatever we stored.
    db.flush()
    return g["books_guests"]


def redetect_all(db, slug=None, kind=None) -> dict:
    shows = _shows_with_text(db, slug=slug)
    stats = {"shows": 0, "deals": 0, "guest_yes": 0}
    for show in shows:
        stats["shows"] += 1
        if kind in (None, "sponsors"):
            stats["deals"] += redetect_sponsors(db, show)
        if kind in (None, "guest"):
            if redetect_guest(db, show):
                stats["guest_yes"] += 1
        db.commit()
    return stats


def main():
    slug = None
    kind = None
    args = sys.argv[1:]
    for i, a in enumerate(args):
        if a == "--show" and i + 1 < len(args):
            slug = args[i + 1]
        if a == "--kind" and i + 1 < len(args):
            kind = args[i + 1]
    if kind and kind not in ("sponsors", "guest"):
        print(f"--kind must be 'sponsors' or 'guest', got {kind!r}")
        return

    init_db()
    db = SessionLocal()
    try:
        target = f"show={slug}" if slug else "all shows"
        scope = f"kind={kind}" if kind else "sponsors+guest"
        print(f"[redetect] re-running detection over cached text ({target}, {scope})")
        stats = redetect_all(db, slug=slug, kind=kind)
        print(f"[redetect] done: shows={stats['shows']} "
              f"deals={stats['deals']} guest_yes={stats['guest_yes']}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
