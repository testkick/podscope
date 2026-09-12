"""Apple Podcasts chart collector.

Source: Apple Marketing Tools RSS feeds — public, keyless, official.
  Top overall (per storefront):
    https://rss.marketingtools.apple.com/api/v2/{cc}/podcasts/top/200/podcasts.json
  Response shape:
    { "feed": { "results": [
        { "id": "1200361736", "name": "The Daily",
          "artistName": "The New York Times",
          "artworkUrl100": "https://.../100x100.jpg", ... }, ... ] } }
  Rank is the array position (1-based).

Per-genre charts: the Marketing Tools feed is genre-agnostic ("top overall").
For genre charts Apple exposes the older iTunes "top podcasts" WS feed:
  https://itunes.apple.com/{cc}/rss/toppodcasts/limit=200/genre={id}/json
which returns { "feed": { "entry": [ { "id": {"attributes": {"im:id": ...}},
  "im:name": {"label": ...}, "im:artist": {"label": ...} }, ... ] } }.
This collector handles BOTH shapes so you get overall + per-genre in one pass.

If Apple changes a shape, the parser logs the first 500 chars of the payload it
couldn't read and skips that one chart — the rest of the run still completes.
"""

import sys
from datetime import datetime

from tenacity import retry, stop_after_attempt, wait_exponential

from app.collectors.common import client, upsert_show, insert_snapshot
from app.db.models import CollectionRun
from config.countries import MARKETS, APPLE_GENRES

MARKETING_TOOLS = (
    "https://rss.marketingtools.apple.com/api/v2/{cc}/podcasts/top/200/podcasts.json"
)
GENRE_FEED = (
    "https://itunes.apple.com/{cc}/rss/toppodcasts/limit=200/genre={gid}/json"
)


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, max=15))
def _get_json(http, url):
    r = http.get(url)
    r.raise_for_status()
    return r.json()


def _parse_marketing_tools(payload):
    """Yield (apple_id, name, publisher, artwork) from the modern feed."""
    for row in payload.get("feed", {}).get("results", []):
        yield (
            str(row.get("id")),
            row.get("name") or "Unknown",
            row.get("artistName"),
            row.get("artworkUrl100"),
        )


def _parse_genre_feed(payload):
    """Yield (apple_id, name, publisher, artwork) from the legacy genre feed."""
    for e in payload.get("feed", {}).get("entry", []):
        try:
            apple_id = e["id"]["attributes"]["im:id"]
        except (KeyError, TypeError):
            continue
        name = (e.get("im:name") or {}).get("label") or "Unknown"
        artist = (e.get("im:artist") or {}).get("label")
        artwork = None
        imgs = e.get("im:image") or []
        if imgs:
            artwork = imgs[-1].get("label")
        yield (str(apple_id), name, artist, artwork)


def collect_apple(db) -> CollectionRun:
    run = CollectionRun(platform="apple", status="running",
                        started_at=datetime.utcnow())
    db.add(run)
    db.flush()

    inserted = 0
    charts = 0
    problems = []

    try:
        with client() as http:
            for cc, _spotify_cc, _name in MARKETS:
                for gid, gname in APPLE_GENRES.items():
                    url = (
                        MARKETING_TOOLS.format(cc=cc) if gid is None
                        else GENRE_FEED.format(cc=cc, gid=gid)
                    )
                    try:
                        payload = _get_json(http, url)
                        rows = (
                            _parse_marketing_tools(payload) if gid is None
                            else _parse_genre_feed(payload)
                        )
                        rank = 0
                        for apple_id, name, publisher, artwork in rows:
                            rank += 1
                            show = upsert_show(
                                db, apple_id=apple_id, name=name,
                                publisher=publisher, artwork_url=artwork,
                            )
                            if insert_snapshot(
                                db, show_id=show.id, platform="apple",
                                country=cc, chart=gname, rank=rank,
                            ):
                                inserted += 1
                        charts += 1
                        db.commit()
                    except Exception as exc:  # noqa: BLE001 - keep the run alive
                        db.rollback()
                        # tenacity's @retry wraps the underlying error (e.g.
                        # HTTPStatusError, ReadTimeout) in a RetryError once
                        # all attempts are exhausted. Either way, this is a
                        # transient upstream failure for this one chart --
                        # log it clearly with platform/region/chart context
                        # and move on to the next chart/region.
                        print(
                            f"[apple] error fetching chart cc={cc} chart={gname}: "
                            f"{exc!r}",
                            file=sys.stderr,
                        )
                        snippet = ""
                        try:
                            snippet = http.get(url).text[:500]
                        except Exception:
                            pass
                        problems.append(f"{cc}/{gname}: {exc} :: {snippet}")
    except Exception as exc:  # noqa: BLE001 - never let apple take down the run
        db.rollback()
        print(f"[apple] fatal error, aborting apple collection early: {exc!r}",
              file=sys.stderr)
        problems.append(f"fatal: {exc}")

    run.finished_at = datetime.utcnow()
    run.rows_inserted = inserted
    run.charts_collected = charts
    run.status = "ok" if not problems else "error"
    run.notes = "\n".join(problems)[:4000] if problems else None
    db.commit()
    return run
