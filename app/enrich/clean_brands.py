"""One-time cleanup: purge junk 'brands' from detected_deals.

The sponsor detector historically let through HTML entities (nbsp), ad-copy
fragments ("Visit", "free listening", "our"), and number strings. is_valid_brand
now blocks these at detection time; this removes the ones already stored so the
brand pages / deals tables are clean.

    python -m app.enrich.clean_brands            # delete junk rows
    python -m app.enrich.clean_brands --dry-run  # show what WOULD be deleted

Safe to re-run. Uses the same is_valid_brand filter as detection, so cleanup and
prevention can't drift.
"""

import sys

from sqlalchemy import select, func, delete

from app.db.session import SessionLocal
from app.db.init_db import init_db
from app.db.enrich_models import DetectedDeal
from app.enrich.sponsors import is_valid_brand


def find_junk(db):
    """Return list of (brand, brand_norm, deal_count) that fail the filter."""
    rows = db.execute(
        select(DetectedDeal.brand, DetectedDeal.brand_norm,
               func.count(DetectedDeal.id))
        .group_by(DetectedDeal.brand, DetectedDeal.brand_norm)
    ).all()
    junk = []
    for brand, brand_norm, n in rows:
        if not is_valid_brand(brand or "", brand_norm):
            junk.append((brand, brand_norm, n))
    return junk


def main():
    dry = "--dry-run" in sys.argv
    init_db()
    db = SessionLocal()
    try:
        junk = find_junk(db)
        total = sum(n for _, _, n in junk)
        print(f"[clean] {len(junk)} junk brand(s), {total} deal rows:")
        for brand, _norm, n in sorted(junk, key=lambda x: -x[2])[:50]:
            print(f"    {n:4}  {brand!r}")
        if dry:
            print("[clean] dry run — nothing deleted")
            return
        norms = [bn for _, bn, _ in junk if bn]
        if norms:
            db.execute(delete(DetectedDeal).where(DetectedDeal.brand_norm.in_(norms)))
            db.commit()
        print(f"[clean] deleted {total} junk deal rows across {len(junk)} brands")
    finally:
        db.close()


if __name__ == "__main__":
    main()
