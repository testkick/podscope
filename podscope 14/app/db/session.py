"""Engine + session factory.

Railway's Postgres plugin injects DATABASE_URL. Locally, falls back to a SQLite
file so you can run the whole thing on your laptop with zero setup — but ship on
Postgres (chart history is append-heavy time-series; SQLite will bottleneck on
concurrent collector writes).
"""

import os

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///./podscope_local.db")

# Railway sometimes provides the legacy "postgres://" scheme; SQLAlchemy 2.x
# wants "postgresql://".
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}

engine = create_engine(
    DATABASE_URL,
    connect_args=connect_args,
    pool_pre_ping=True,   # survive Railway's idle connection drops
    future=True,
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_session():
    """FastAPI dependency."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
