"""Enricher-side models. Imported alongside the core models in models.py's Base.

Design:
- Episode: one row per episode we've pulled from a show's RSS. We only crawl
  episodes for shows worth enriching (charting / top-N), not all 4M shows.
- EpisodeText: the raw text we extracted for an episode and WHERE it came from
  (transcript tag / show notes / STT). Kept separate from Episode so re-running
  extraction never re-fetches, and so we can audit the source of every deal.
- DetectedDeal: a sponsor we detected in an episode, with a confidence band and
  the evidence snippet. Mirrors SponsorRadar's "deduced, not confirmed" model —
  every deal carries its evidence and a confidence level so the UI can hedge.
- GuestProfile: per-show summary of whether the show books guests and what topics
  it covers — the guest-appearance matching signal. One row per show.
- EnrichJob: the work queue. status = pending|running|done|error. The enricher
  worker pulls pending jobs, so a crash resumes cleanly and cost is cappable.
"""

from datetime import datetime, date

from sqlalchemy import (
    String, Integer, Float, Date, DateTime, ForeignKey, Text, Boolean,
    UniqueConstraint, Index,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.models import Base  # reuse the same declarative Base


class Episode(Base):
    __tablename__ = "episodes"
    __table_args__ = (
        UniqueConstraint("show_id", "guid", name="uq_episode_guid"),
        Index("ix_episode_show", "show_id", "published"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    show_id: Mapped[int] = mapped_column(ForeignKey("shows.id"), index=True)
    guid: Mapped[str] = mapped_column(String(600))
    title: Mapped[str] = mapped_column(String(1024))
    published: Mapped[date | None] = mapped_column(Date)
    audio_url: Mapped[str | None] = mapped_column(String(2048))
    description: Mapped[str | None] = mapped_column(Text)
    transcript_url: Mapped[str | None] = mapped_column(String(2048))
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class EpisodeText(Base):
    __tablename__ = "episode_texts"

    id: Mapped[int] = mapped_column(primary_key=True)
    episode_id: Mapped[int] = mapped_column(
        ForeignKey("episodes.id"), unique=True, index=True)
    source: Mapped[str] = mapped_column(String(24))   # transcript|notes|stt
    text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class DetectedDeal(Base):
    __tablename__ = "detected_deals"
    __table_args__ = (
        UniqueConstraint("episode_id", "brand_norm", name="uq_deal_ep_brand"),
        Index("ix_deal_show", "show_id"),
        Index("ix_deal_brand", "brand_norm"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    show_id: Mapped[int] = mapped_column(ForeignKey("shows.id"), index=True)
    episode_id: Mapped[int] = mapped_column(ForeignKey("episodes.id"), index=True)

    brand: Mapped[str] = mapped_column(String(256))        # as detected
    brand_norm: Mapped[str] = mapped_column(String(256))   # lowercased key
    promo_code: Mapped[str | None] = mapped_column(String(128))
    promo_url: Mapped[str | None] = mapped_column(String(512))
    # host_read = clear paid read; mention = named but ambiguous; affiliate = link only
    deal_type: Mapped[str] = mapped_column(String(16), default="mention")
    confidence: Mapped[str] = mapped_column(String(8), default="low")  # high|med|low
    evidence: Mapped[str | None] = mapped_column(Text)     # short quote of the ad
    detected_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class GuestProfile(Base):
    __tablename__ = "guest_profiles"

    show_id: Mapped[int] = mapped_column(
        ForeignKey("shows.id"), primary_key=True)
    books_guests: Mapped[bool] = mapped_column(Boolean, default=False)
    guest_frequency: Mapped[str | None] = mapped_column(String(16))  # every|often|rare
    topics: Mapped[str | None] = mapped_column(Text)   # comma-sep topic tags
    format_note: Mapped[str | None] = mapped_column(String(512))
    contact_email: Mapped[str | None] = mapped_column(String(256))  # from RSS owner
    suitability_note: Mapped[str | None] = mapped_column(Text)
    recent_guests: Mapped[str | None] = mapped_column(Text)  # comma-sep names detected
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class EnrichJob(Base):
    __tablename__ = "enrich_jobs"
    __table_args__ = (
        UniqueConstraint("show_id", "kind", name="uq_job_show_kind"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    show_id: Mapped[int] = mapped_column(ForeignKey("shows.id"), index=True)
    kind: Mapped[str] = mapped_column(String(16))    # sponsors|guest
    status: Mapped[str] = mapped_column(String(8), default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    queued_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)


class ShowEmbedding(Base):
    """Semantic embedding of a show's topic + recent-guest text, for meaning-based
    matching. Vector stored as JSON text for portability (SQLite dev / Postgres
    without pgvector); on Postgres with pgvector a real vector column + index is
    added by migrate.py for fast similarity search. One row per show."""
    __tablename__ = "show_embeddings"

    show_id: Mapped[int] = mapped_column(ForeignKey("shows.id"), primary_key=True)
    source_text: Mapped[str | None] = mapped_column(Text)   # what was embedded
    vector_json: Mapped[str | None] = mapped_column(Text)   # portable fallback store
    dim: Mapped[int | None] = mapped_column(Integer)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class OP3Metrics(Base):
    """Real download data from OP3 for shows that use its prefix. Only a subset
    of shows will have a row here (those measured by OP3) — that's expected.
    This is the ground-truth set for verified reach + future calibration."""
    __tablename__ = "op3_metrics"

    show_id: Mapped[int] = mapped_column(ForeignKey("shows.id"), primary_key=True)
    op3_show_uuid: Mapped[str | None] = mapped_column(String(64))
    recent_month_downloads: Mapped[int | None] = mapped_column(Integer)
    monthly_avg_downloads: Mapped[int | None] = mapped_column(Integer)
    weekly_avg_downloads: Mapped[int | None] = mapped_column(Integer)
    months_measured: Mapped[int | None] = mapped_column(Integer)
    checked_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    measured: Mapped[bool] = mapped_column(Boolean, default=False)  # is it in OP3?
