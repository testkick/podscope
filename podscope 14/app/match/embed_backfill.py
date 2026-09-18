"""Compute and store semantic embeddings for shows, for meaning-based matching.

Separate from redetect because embeddings hit an API (a cost/network profile
redetect deliberately avoids). Run on a cron or manually:

    python -m app.match.embed_backfill              # all guest-booking shows missing/stale
    python -m app.match.embed_backfill --all        # every show with topics
    python -m app.match.embed_backfill --limit 500

What it embeds: a compact "profile blob" per show = topics + recent guests +
name. That's the text a query like "venture capital" should match against.

Storage: writes vector_json always (portable), and the pgvector `embedding`
column when present (fast search). Idempotent; skips shows whose source_text
hasn't changed unless --force.
"""

import os
import sys
import json

from sqlalchemy import select, text as sqltext

from app.db.session import SessionLocal
from app.db.init_db import init_db
from app.db.models import Show
from app.db.enrich_models import GuestProfile, ShowEmbedding
from app.match.embeddings import embed_batch, embeddings_enabled, EMBED_DIM

BATCH = int(os.environ.get("EMBED_BATCH", "50"))
MAX_PER_RUN = int(os.environ.get("EMBED_MAX", "1000"))


def _profile_blob(db, show, gp) -> str:
    """Build the text we embed for semantic matching. The KEY to topic depth:
    use what the show ACTUALLY discusses (recent episode titles + descriptions +
    guest names) — not just the handful of generic category tags. This is what
    lets 'CRISPR gene editing' or 'seed-stage fundraising' match a show that
    covers those specifics, instead of only matching broad terms like 'science'."""
    from app.db.enrich_models import Episode
    parts = [show.name, show.publisher]
    if gp:
        parts += [gp.topics, gp.recent_guests]
    # pull recent episode titles + descriptions — the real topical signal
    eps = db.execute(
        select(Episode.title, Episode.description)
        .where(Episode.show_id == show.id)
        .order_by(Episode.published.desc().nullslast())
        .limit(15)
    ).all()
    for title, desc in eps:
        if title:
            parts.append(title)
        if desc:
            parts.append(desc[:400])   # cap per-episode so one long note can't dominate
    blob = " — ".join(p for p in parts if p)
    return blob[:8000]                 # overall cap for the embedding call


def _has_pgvector(db) -> bool:
    if db.bind.dialect.name != "postgresql":
        return False
    try:
        cols = db.execute(sqltext(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name='show_embeddings' AND column_name='embedding'"
        )).first()
        return cols is not None
    except Exception:
        return False


def _store(db, show_id, blob, vec, pgvector: bool):
    row = db.get(ShowEmbedding, show_id)
    if row is None:
        row = ShowEmbedding(show_id=show_id)
        db.add(row)
    row.source_text = blob
    row.vector_json = json.dumps(vec)
    row.dim = len(vec)
    from datetime import datetime
    row.updated_at = datetime.utcnow()
    db.flush()
    if pgvector:
        # write the real vector column via raw SQL (pgvector accepts the text form)
        db.execute(sqltext(
            "UPDATE show_embeddings SET embedding = :v WHERE show_id = :sid"
        ), {"v": "[" + ",".join(str(x) for x in vec) + "]", "sid": show_id})


def backfill(db, all_shows=False, limit=MAX_PER_RUN, force=False) -> dict:
    if not embeddings_enabled():
        return {"error": "EMBEDDINGS_API_KEY not set — nothing embedded"}

    pgvector = _has_pgvector(db)

    # candidate shows: guest-booking (default) or all with topics
    q = select(Show, GuestProfile).outerjoin(
        GuestProfile, GuestProfile.show_id == Show.id)
    if not all_shows:
        q = q.where(GuestProfile.books_guests.is_(True))
    rows = db.execute(q).all()

    # build work list, skipping unchanged unless force
    work = []
    for show, gp in rows:
        blob = _profile_blob(db, show, gp)
        if not blob or len(blob) < 3:
            continue
        existing = db.get(ShowEmbedding, show.id)
        if existing and not force and existing.source_text == blob:
            continue    # unchanged -> skip (idempotent, cheap re-runs)
        work.append((show.id, blob))
        if len(work) >= limit:
            break

    embedded = 0
    for i in range(0, len(work), BATCH):
        chunk = work[i:i + BATCH]
        vecs = embed_batch([b for _, b in chunk])
        for (sid, blob), vec in zip(chunk, vecs):
            if vec is None:
                continue
            _store(db, sid, blob, vec, pgvector)
            embedded += 1
        db.commit()

    return {"candidates": len(work), "embedded": embedded,
            "pgvector": pgvector, "dim": EMBED_DIM}


def main():
    all_shows = "--all" in sys.argv
    force = "--force" in sys.argv
    limit = MAX_PER_RUN
    if "--limit" in sys.argv:
        i = sys.argv.index("--limit")
        if i + 1 < len(sys.argv):
            limit = int(sys.argv[i + 1])

    init_db()
    db = SessionLocal()
    try:
        r = backfill(db, all_shows=all_shows, limit=limit, force=force)
        print(f"[embed] {r}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
