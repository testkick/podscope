"""Enricher worker. Railway runs this as a separate service (cron or worker):
    python -m app.enrich.run

It pulls pending EnrichJobs, resolves the show's RSS feed, fetches recent
episodes, and runs the requested extractor (sponsors | guest). Idempotent:
episodes and deals use unique constraints, so re-running a show updates rather
than duplicates. Cost-capped by MAX_JOBS_PER_RUN so a big backlog can't run away.

Queueing: auto_queue() enrolls the top charting shows that don't yet have jobs.
Call it from the chart collector or on a schedule; here it's callable directly.
"""

import os
from datetime import datetime

from sqlalchemy import select, func

from app.db.session import SessionLocal
from app.db.init_db import init_db
from app.db.models import Show, ChartSnapshot
from app.db.enrich_models import (
    Episode, EpisodeText, DetectedDeal, GuestProfile, EnrichJob,
)
from app.enrich.transcripts import fetch_feed, get_episode_text
from app.enrich.sponsors import detect_sponsors
from app.enrich.guest import analyze_guest

MAX_JOBS_PER_RUN = int(os.environ.get("ENRICH_MAX_JOBS", "25"))


def resolve_feed_url(show: Show) -> str | None:
    """Where the RSS feed lives. Part 1 (reconcile) populated Show.feed_url for
    the whole catalog, so that's the primary source. Fall back to a live Podcast
    Index lookup only if a show somehow lacks a stored feed (e.g. added since the
    last reconcile run). A test override still wins for unit tests.
    """
    override = getattr(show, "_feed_url_override", None)
    if override:
        return override
    if show.feed_url:
        return show.feed_url
    # Fallback: resolve on the fly (same logic reconcile uses).
    try:
        from app.enrich.podcastindex import resolve_by_itunes_id, search_feed
        info = None
        if show.apple_id:
            info = resolve_by_itunes_id(show.apple_id)
        if info is None and show.spotify_id:
            info = search_feed(show.name, show.publisher)
        if info and info.get("feed_url"):
            show.feed_url = info["feed_url"]  # cache it for next time
            return show.feed_url
    except Exception:
        pass
    return None


def _upsert_episode(db, show_id, ep) -> Episode:
    row = db.scalar(select(Episode).where(
        Episode.show_id == show_id, Episode.guid == ep["guid"]))
    if row is None:
        row = Episode(show_id=show_id, guid=ep["guid"], title=ep["title"],
                      published=ep["published"], audio_url=ep["audio_url"],
                      description=ep["description"],
                      transcript_url=ep["transcript_url"])
        db.add(row)
        db.flush()
    return row


def _save_text(db, episode_id, source, text):
    row = db.scalar(select(EpisodeText).where(EpisodeText.episode_id == episode_id))
    if row is None:
        db.add(EpisodeText(episode_id=episode_id, source=source, text=text))
    else:
        row.source, row.text = source, text


def _upsert_deal(db, show_id, episode_id, d):
    existing = db.scalar(select(DetectedDeal).where(
        DetectedDeal.episode_id == episode_id,
        DetectedDeal.brand_norm == d["brand_norm"]))
    if existing:
        existing.confidence = d["confidence"]
        existing.deal_type = d["deal_type"]
        existing.promo_code = d.get("promo_code")
        existing.promo_url = d.get("promo_url")
        return
    db.add(DetectedDeal(
        show_id=show_id, episode_id=episode_id, brand=d["brand"],
        brand_norm=d["brand_norm"], promo_code=d.get("promo_code"),
        promo_url=d.get("promo_url"), deal_type=d["deal_type"],
        confidence=d["confidence"], evidence=d.get("evidence")))


def process_job(db, job: EnrichJob, feed_url: str | None = None) -> str:
    show = db.get(Show, job.show_id)
    feed_url = feed_url or resolve_feed_url(show)
    if not feed_url:
        return "no_feed_url"

    feed = fetch_feed(feed_url, max_episodes=12)
    episodes = feed["episodes"]

    if job.kind == "sponsors":
        total = 0
        for ep in episodes:
            row = _upsert_episode(db, show.id, ep)
            got = get_episode_text(ep)
            if not got:
                continue
            source, text = got
            _save_text(db, row.id, source, text)
            for d in detect_sponsors(text):
                _upsert_deal(db, show.id, row.id, d)
                total += 1
        db.flush()
        return f"deals:{total}"

    if job.kind == "guest":
        for ep in episodes:
            _upsert_episode(db, show.id, ep)
        g = analyze_guest(show.name, episodes)
        prof = db.get(GuestProfile, show.id)
        if prof is None:
            prof = GuestProfile(show_id=show.id)
            db.add(prof)
        prof.books_guests = g["books_guests"]
        prof.guest_frequency = g.get("guest_frequency")
        prof.topics = g.get("topics")
        prof.format_note = g.get("format_note")
        prof.suitability_note = g.get("suitability_note")
        prof.contact_email = feed.get("owner_email")
        prof.updated_at = datetime.utcnow()
        db.flush()
        return f"guest:{'yes' if g['books_guests'] else 'no'}"

    return "unknown_kind"


def auto_queue(db, top_n: int = 100):
    """Enroll top charting shows for both extractors if not already queued.
    Prefers shows that already have a feed_url (from Part 1 reconcile) — no point
    queuing a show the enricher can't fetch episodes for."""
    latest = db.scalar(select(func.max(ChartSnapshot.captured_date)))
    if not latest:
        return 0
    show_ids = db.scalars(
        select(ChartSnapshot.show_id)
        .join(Show, Show.id == ChartSnapshot.show_id)
        .where(
            ChartSnapshot.captured_date == latest,
            Show.feed_url.is_not(None),   # only shows we can actually enrich
        )
        .group_by(ChartSnapshot.show_id)
        .order_by(func.min(ChartSnapshot.rank))
        .limit(top_n)
    ).all()
    added = 0
    for sid in show_ids:
        for kind in ("sponsors", "guest"):
            exists = db.scalar(select(EnrichJob).where(
                EnrichJob.show_id == sid, EnrichJob.kind == kind))
            if not exists:
                db.add(EnrichJob(show_id=sid, kind=kind, status="pending"))
                added += 1
    db.commit()
    return added


def run_pending(db, limit=MAX_JOBS_PER_RUN):
    jobs = db.scalars(
        select(EnrichJob).where(EnrichJob.status == "pending").limit(limit)
    ).all()
    done = 0
    failed = 0
    consecutive_failures = 0
    # If the first several jobs ALL fail, something systemic is wrong (bad feeds,
    # no network, dependency missing) — abort loudly instead of grinding through
    # the whole batch. Lesson from the silent reconcile auth failure.
    CIRCUIT_BREAK_AFTER = int(os.environ.get("ENRICH_CIRCUIT_BREAK", "8"))

    for job in jobs:
        job.status = "running"
        job.attempts += 1
        db.commit()
        try:
            result = process_job(db, job)
            ok = not result.startswith("no_feed")
            job.status = "done" if ok else "error"
            job.last_error = None if ok else result
            job.finished_at = datetime.utcnow()
            db.commit()
            print(f"[enrich] show={job.show_id} kind={job.kind} -> {result}")
            if ok:
                done += 1
                consecutive_failures = 0
            else:
                failed += 1
                consecutive_failures += 1
        except Exception as exc:  # noqa: BLE001
            db.rollback()
            job.status = "error"
            job.last_error = str(exc)[:1000]
            db.commit()
            print(f"[enrich] show={job.show_id} kind={job.kind} ERROR {exc}")
            failed += 1
            consecutive_failures += 1

        if consecutive_failures >= CIRCUIT_BREAK_AFTER:
            print(f"[enrich] CIRCUIT BREAKER: {consecutive_failures} consecutive "
                  f"failures — aborting run (systemic problem, not per-show).")
            break

    return {"done": done, "failed": failed}


def auto_queue_one(db, slug: str) -> int:
    """Queue both extractors for a single show by slug — for proving the pipeline
    on one known show (e.g. Bad Friends) before running the whole catalog."""
    show = db.scalar(select(Show).where(Show.slug == slug))
    if not show:
        print(f"[enrich] no show with slug {slug!r}")
        return 0
    added = 0
    for kind in ("sponsors", "guest"):
        exists = db.scalar(select(EnrichJob).where(
            EnrichJob.show_id == show.id, EnrichJob.kind == kind))
        if not exists:
            db.add(EnrichJob(show_id=show.id, kind=kind, status="pending"))
            added += 1
    db.commit()
    return added


def main(slug: str | None = None):
    init_db()
    db = SessionLocal()
    try:
        if slug:
            queued = auto_queue_one(db, slug)
            print(f"[enrich] queued {queued} jobs for {slug}")
        else:
            queued = auto_queue(db)
            print(f"[enrich] auto-queued {queued} new jobs")
        result = run_pending(db)
        print(f"[enrich] processed: done={result['done']} failed={result['failed']}")
    finally:
        db.close()


if __name__ == "__main__":
    import sys
    # Optional: `python -m app.enrich.run --show <slug>` to enrich one show.
    slug_arg = None
    if len(sys.argv) > 2 and sys.argv[1] == "--show":
        slug_arg = sys.argv[2]
    main(slug=slug_arg)
