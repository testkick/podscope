"""Offline test of the enricher: synthetic RSS -> episodes -> deals + guest.
No network, no API key. Uses the existing seeded shows from scripts_test_local.
"""

import os
from datetime import date

# minimal RSS with two host-read ads (one with promo code+url), interview titles
FAKE_RSS = """<?xml version="1.0"?>
<rss xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd"
     xmlns:podcast="https://podcastindex.org/namespace/1.0"
     xmlns:content="http://purl.org/rss/1.0/modules/content/">
<channel>
  <title>Deep Dive Show</title>
  <itunes:owner><itunes:email>booking@deepdiveshow.com</itunes:email></itunes:owner>
  <item>
    <title>The future of AI, my conversation with Jane Doe</title>
    <guid>ep-101</guid>
    <pubDate>Mon, 08 Sep 2026 10:00:00 +0000</pubDate>
    <enclosure url="https://cdn.example.com/101.mp3" type="audio/mpeg"/>
    <description>This episode is brought to you by Squarespace. Go to
      squarespace.com/deepdive and use code DEEPDIVE for 10% off your first
      order. Today I sit down with Jane Doe to discuss startups and technology.</description>
  </item>
  <item>
    <title>Building better habits — interview with Dr. Alan Smith</title>
    <guid>ep-102</guid>
    <pubDate>Mon, 01 Sep 2026 10:00:00 +0000</pubDate>
    <enclosure url="https://cdn.example.com/102.mp3" type="audio/mpeg"/>
    <description>Sponsored by BetterHelp. Visit betterhelp.com/deepdive to get
      10% off. A great conversation with Dr. Alan Smith about psychology and health.</description>
  </item>
  <item>
    <title>Solo episode: my thoughts on the market</title>
    <guid>ep-103</guid>
    <pubDate>Mon, 25 Aug 2026 10:00:00 +0000</pubDate>
    <enclosure url="https://cdn.example.com/103.mp3" type="audio/mpeg"/>
    <description>No sponsor today. Just me talking about finance and investing.</description>
  </item>
</channel></rss>"""


def run():
    # serve the fake RSS from a local file:// URL so fetch_feed works unchanged
    path = os.path.abspath("fake_feed.xml")
    open(path, "w").write(FAKE_RSS)
    feed_url = "file://" + path

    from app.db.init_db import init_db
    from app.db.session import SessionLocal
    from app.db.models import Show
    from app.db.enrich_models import EnrichJob
    from app.enrich import run as enr
    from app.api import queries as Q

    init_db()
    db = SessionLocal()

    # make a show to attach to
    show = db.query(Show).filter(Show.name == "Deep Dive Show").first()
    if not show:
        show = Show(name="Deep Dive Show", slug="deep-dive-show-test",
                    apple_id="999999")
        db.add(show); db.commit()

    for kind in ("sponsors", "guest"):
        if not db.query(EnrichJob).filter_by(show_id=show.id, kind=kind).first():
            db.add(EnrichJob(show_id=show.id, kind=kind, status="pending"))
    db.commit()

    # process the two jobs with the injected feed url
    for job in db.query(EnrichJob).filter_by(show_id=show.id).all():
        job.status = "running"; db.commit()
        result = enr.process_job(db, job, feed_url=feed_url)
        job.status = "done"; db.commit()
        print(f"JOB {job.kind} -> {result}")

    print("\n--- BRANDS (headline host_read deals) ---")
    for b in Q.show_brands(db, show.id):
        print(" ", b)
    print("\n--- RECENT DEALS ---")
    for d in Q.show_deals(db, show.id):
        print(f"  {d['published']} {d['brand']:14} type={d['deal_type']:9} "
              f"conf={d['confidence']:4} code={d['promo_code']} url={d['promo_url']}")
    print("\n--- GUEST PROFILE ---")
    print(" ", Q.show_guest_profile(db, show.id))

    # assertions
    brands = {b["brand"].lower() for b in Q.show_brands(db, show.id)}
    assert any("squarespace" in b for b in brands), f"missing squarespace: {brands}"
    assert any("betterhelp" in b for b in brands), f"missing betterhelp: {brands}"
    deals = Q.show_deals(db, show.id)
    sq = next(d for d in deals if "squarespace" in d["brand"].lower())
    assert sq["promo_code"] == "DEEPDIVE", sq
    assert sq["confidence"] == "high", sq
    g = Q.show_guest_profile(db, show.id)
    assert g["books_guests"] is True, g
    assert g["contact_email_hidden"] if False else g["has_contact"], g
    db.close()
    print("\nENRICHER CHECKS PASSED")


if __name__ == "__main__":
    for f in ("podscope_local.db",):
        if os.path.exists(f):
            os.remove(f)
    run()
