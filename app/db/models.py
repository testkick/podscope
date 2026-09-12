"""Data model.

Design notes:
- `Show` is the stable identity of a podcast across platforms. Apple and Spotify
  each have their own ids; a show is ONE row here keyed by our own surrogate id,
  with the platform ids as nullable unique columns. At MVP we don't try to merge
  Apple<->Spotify identities (that's a fuzzy-match problem for later); we just
  keep both id columns so the merge is possible without a migration.
- `ChartSnapshot` is the crown jewel and the biggest table. One row per
  (show, platform, country, chart, captured_date, rank). The unique constraint
  makes every collector run idempotent: re-running the same day is a no-op.
- `CollectionRun` is an audit log so you can answer "did the 6am run complete
  and how many rows did it write?" without grepping logs.
"""

from datetime import datetime, date

from sqlalchemy import (
    String, Integer, Date, DateTime, ForeignKey, UniqueConstraint, Index, Text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Show(Base):
    __tablename__ = "shows"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Platform identifiers. Nullable because a show may first appear on only one.
    apple_id: Mapped[str | None] = mapped_column(String(32), unique=True, index=True)
    spotify_id: Mapped[str | None] = mapped_column(String(64), unique=True, index=True)

    name: Mapped[str] = mapped_column(String(512), index=True)
    publisher: Mapped[str | None] = mapped_column(String(512))
    artwork_url: Mapped[str | None] = mapped_column(String(1024))
    # URL-safe slug for public pages, e.g. "the-daily-1200361736"
    slug: Mapped[str | None] = mapped_column(String(600), unique=True, index=True)

    first_seen: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    last_seen: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    snapshots: Mapped[list["ChartSnapshot"]] = relationship(back_populates="show")


class ChartSnapshot(Base):
    __tablename__ = "chart_snapshots"
    __table_args__ = (
        # One rank per show per chart per day. Idempotency lives here.
        UniqueConstraint(
            "show_id", "platform", "country", "chart",
            "captured_date",
            name="uq_snapshot_daily",
        ),
        Index("ix_chart_lookup", "platform", "country", "chart", "captured_date"),
        Index("ix_show_history", "show_id", "captured_date"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    show_id: Mapped[int] = mapped_column(ForeignKey("shows.id"), index=True)

    platform: Mapped[str] = mapped_column(String(16))          # "apple" | "spotify"
    country: Mapped[str] = mapped_column(String(8))            # storefront/region
    chart: Mapped[str] = mapped_column(String(64))            # genre name or "top"/"trending"
    rank: Mapped[int] = mapped_column(Integer)

    captured_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    captured_date: Mapped[date] = mapped_column(Date, default=date.today, index=True)

    show: Mapped["Show"] = relationship(back_populates="snapshots")


class CollectionRun(Base):
    __tablename__ = "collection_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    platform: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16), default="running")  # running|ok|error
    rows_inserted: Mapped[int] = mapped_column(Integer, default=0)
    charts_collected: Mapped[int] = mapped_column(Integer, default=0)
    notes: Mapped[str | None] = mapped_column(Text)
