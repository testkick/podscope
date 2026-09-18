"""Local end-to-end test WITHOUT network. Simulates what the collectors write,
then exercises queries, idempotency, and the API. Not shipped to prod."""

import random
from datetime import date, timedelta

from app.db.init_db import init_db
from app.db.session import SessionLocal
from app.collectors.common import upsert_show, insert_snapshot
from app.api import queries as Q

random.seed(7)

SHOWS = [
    ("1200361736", None, "The Daily", "The New York Times"),
    ("1028908750", "5CfCWKI5pZ28U0uOzXkDHe", "Stuff You Should Know", "iHeartPodcasts"),
    (None, "4rOoJ6Egrf8K2IrywzwOMk", "The Joe Rogan Experience", "Spotify"),
    ("1613846923", None, "Huberman Lab", "Scicomm Media"),
]
MARKETS = [("us", "apple", "Top Overall"), ("ca", "apple", "Top Overall"),
           ("us", "apple", "Business"), ("gb", "spotify", "top")]


def seed():
    init_db()
    db = SessionLocal()
    rows = 0
    # 30 days of history so trends render.
    for day_offset in range(30, -1, -1):
        d = date.today() - timedelta(days=day_offset)
        for apple_id, spotify_id, name, pub in SHOWS:
            show = upsert_show(db, apple_id=apple_id, spotify_id=spotify_id,
                               name=name, publisher=pub,
                               artwork_url="https://example.com/art.jpg")
            for cc, platform, chart in MARKETS:
                # only apple shows chart on apple etc. keep it simple:
                if platform == "apple" and not show.apple_id:
                    continue
                if platform == "spotify" and not show.spotify_id:
                    continue
                base = {"The Daily": 3, "Stuff You Should Know": 12,
                        "The Joe Rogan Experience": 1, "Huberman Lab": 7}[name]
                rank = max(1, base + random.randint(-4, 6) - (day_offset // 10))
                if insert_snapshot(db, show_id=show.id, platform=platform,
                                   country=cc, chart=chart, rank=rank,
                                   captured_date=d):
                    rows += 1
        db.commit()
    db.close()
    return rows


def verify():
    db = SessionLocal()
    stats = Q.platform_stats(db)
    print("STATS:", stats)
    assert stats["shows"] == len(SHOWS), stats
    assert stats["days_tracked"] == 31, stats

    results = Q.search_shows(db, "daily")
    print("SEARCH 'daily':", [(s.name, s.slug) for s in results])
    assert any("Daily" in s.name for s in results)

    daily = Q.get_show_by_slug(db, results[0].slug)
    pos = Q.show_current_positions(db, daily.id)
    print("POSITIONS(the daily):", [(p.platform, p.country, p.chart, p.rank) for p in pos])
    assert pos, "expected current positions"

    hist = Q.show_history(db, daily.id, pos[0].platform, pos[0].country, pos[0].chart)
    print("HISTORY len:", len(hist), "first:", hist[0], "last:", hist[-1])
    assert len(hist) > 1

    geo = Q.show_geo_footprint(db, daily.id)
    print("GEO:", geo)
    assert geo
    db.close()


def test_idempotency():
    """Re-seeding the same day must insert ZERO new rows."""
    db = SessionLocal()
    show = Q.get_show_by_slug(db, Q.search_shows(db, "daily")[0].slug)
    before = Q.platform_stats(db)["snapshots"]
    # attempt to reinsert today's row for an existing chart
    dup = insert_snapshot(db, show_id=show.id, platform="apple",
                          country="us", chart="Top Overall", rank=999,
                          captured_date=date.today())
    db.commit()
    after = Q.platform_stats(db)["snapshots"]
    print(f"IDEMPOTENCY: dup_inserted={dup} snapshots before={before} after={after}")
    assert dup is False, "duplicate should not insert"
    assert before == after
    db.close()


if __name__ == "__main__":
    import os
    if os.path.exists("podscope_local.db"):
        os.remove("podscope_local.db")
    n = seed()
    print(f"SEEDED {n} snapshot rows")
    verify()
    test_idempotency()
    print("\nALL LOCAL CHECKS PASSED")
