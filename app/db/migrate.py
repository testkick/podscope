"""Tiny idempotent migration: add columns that create_all() won't add to an
already-existing table. Safe to run repeatedly. Call before reconcile on a DB
that predates the feed_url column.

Usage (once, or harmlessly on every deploy):
    python -m app.db.migrate
"""

import os
from sqlalchemy import inspect, text

from app.db.session import engine
from app.db.init_db import init_db


def _column_exists(table: str, column: str) -> bool:
    insp = inspect(engine)
    if table not in insp.get_table_names():
        return False
    return column in [c["name"] for c in insp.get_columns(table)]


def migrate() -> list[str]:
    init_db()  # ensure any brand-new tables exist first
    applied = []
    # add shows.feed_url if missing
    if not _column_exists("shows", "feed_url"):
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE shows ADD COLUMN feed_url VARCHAR(2048)"))
            # index it (Postgres + SQLite both accept IF NOT EXISTS)
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_shows_feed_url ON shows (feed_url)"))
        applied.append("shows.feed_url")
    # add guest_profiles.recent_guests if missing
    if not _column_exists("guest_profiles", "recent_guests"):
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE guest_profiles ADD COLUMN recent_guests TEXT"))
        applied.append("guest_profiles.recent_guests")

    # pgvector: enable extension + add a real vector column on show_embeddings
    # for fast semantic search. Postgres only; harmless to skip on SQLite (dev),
    # where we fall back to the JSON vector store. All idempotent.
    if engine.dialect.name == "postgresql":
        dim = int(os.environ.get("EMBEDDINGS_DIM", "1536"))
        try:
            with engine.begin() as conn:
                conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
                if not _column_exists("show_embeddings", "embedding"):
                    conn.execute(text(
                        f"ALTER TABLE show_embeddings ADD COLUMN embedding vector({dim})"))
                    applied.append("show_embeddings.embedding (pgvector)")
                # cosine-distance index for fast nearest-neighbour search
                conn.execute(text(
                    "CREATE INDEX IF NOT EXISTS ix_show_emb_cos "
                    "ON show_embeddings USING hnsw (embedding vector_cosine_ops)"))
        except Exception as exc:
            # pgvector not available on this instance — JSON fallback still works
            applied.append(f"pgvector skipped ({type(exc).__name__})")
    return applied


if __name__ == "__main__":
    done = migrate()
    print(f"migrations applied: {done}" if done else "nothing to migrate (up to date)")
