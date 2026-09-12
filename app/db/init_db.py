"""Create tables if they don't exist. Safe to run on every boot."""

from app.db.models import Base
# Importing enrich_models registers its tables on the shared Base metadata.
from app.db import enrich_models  # noqa: F401
from app.db.session import engine


def init_db() -> None:
    Base.metadata.create_all(bind=engine)


if __name__ == "__main__":
    init_db()
    print("DB initialized (tables created if missing).")
