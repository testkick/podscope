"""Apple Podcasts chart collector.

Source: Apple's iTunes RSS "top podcasts" feeds — public, keyless.
  Top overall (per storefront):
    https://itunes.apple.com/{cc}/rss/toppodcasts/limit=200/json
  Per-genre:
    https://itunes.apple.com/{cc}/rss/toppodcasts/limit=200/genre={id}/json
  Both return the same shape:
    { "feed": { "entry": [ { "id": {"attributes": {"im:id": "1200361736"}},
        "im:name": {"label": "The Daily"},
        "im:artist": {"label": "The New York Times"}, ... }, ... ] } }
  Rank is the array position (1-based).

We deliberately use ONE host (itunes.apple.com) for both charts. An earlier
version pulled "top overall" from rss.marketingtools.apple.com, but that host
intermittently returned nginx 500s under datacenter load while itunes.apple.com
served reliably — so both charts now go through the working host.

If Apple changes a shape, the parser logs a snippet of the payload it couldn't
read and skips that one chart — the rest of the run still completes, and the
circuit breaker aborts early if failures are systemic (blocked / upstream down).
"""

from datetime import datetime

from tenacity import retry, stop_after_attempt, wait_exponential

from app.collectors.common import (
    client, upsert_show, insert_snapshot, polite_pause, CIRCUIT_BREAK_AFTER,
)
from app.db.models import CollectionRun
from config.countries import MARKETS, APPLE_GENRES

# Both charts now come from itunes.apple.com — the host that answers reliably.
# rss.marketingtools.apple.com was throwing nginx 500s on the /top/200 feed while
# itunes.apple.com served the genre feeds fine, so we use one host for both.
#   Top overall (genre-agnostic): .../rss/toppodcasts/limit=200/json
#   Per-genre:                     .../rss/toppodcasts/limit=200/genre={id}/json
# Both return the same {"feed":{"entry":[...]}} shape -> _parse_genre_feed.
TOP_OVERALL_FEED = (
    "https://itunes.apple.com/{cc}/rss/toppodcasts/limit=200/json"
)
GENRE_FEED = (
    "https://itunes.apple.com/{cc}/rss/toppodcasts/limit=200/genre={gid}/json"
)


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, max=15))
def _get_json(http, url):
    r = http.get(url)
    r.raise_for_status()
    return r.json()


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
    db.commit()          # persist run row NOW so a later chart-level rollback
    run_id = run.id      # can't discard it; keep id to re-fetch at the end.

    inserted = 0
    charts = 0
    problems = []
    consecutive_failures = 0

    with client() as http:
        for cc, _spotify_cc, _name in MARKETS:
            for gid, gname in APPLE_GENRES.items():
                url = (
                    TOP_OVERALL_FEED.format(cc=cc) if gid is None
                    else GENRE_FEED.format(cc=cc, gid=gid)
                )
                polite_pause()  # randomized gap so we don't burst
                try:
                    payload = _get_json(http, url)
                    # Both endpoints now return the same iTunes RSS shape.
                    rows = _parse_genre_feed(payload)
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
                    consecutive_failures = 0  # recovered
                    db.commit()
                except Exception as exc:  # noqa: BLE001 - keep the run alive
                    db.rollback()
                    # Do NOT re-request just to grab a snippet — that doubles
                    # load exactly when we're already failing. Pull the body off
                    # the exception if it carried a response.
                    snippet = ""
                    resp = getattr(exc, "response", None)
                    if resp is not None:
                        snippet = (resp.text or "")[:200]
                    problems.append(f"{cc}/{gname}: {type(exc).__name__} :: {snippet}")
                    consecutive_failures += 1
                    if consecutive_failures >= CIRCUIT_BREAK_AFTER:
                        problems.append(
                            f"CIRCUIT BREAKER: {consecutive_failures} consecutive "
                            f"failures — aborting Apple run early (blocked or down)."
                        )
                        break
            else:
                continue  # inner loop finished without break
            break  # circuit breaker tripped; stop outer loop too

    # Re-fetch: chart-level rollbacks may have detached the original instance.
    run = db.get(CollectionRun, run_id)
    run.finished_at = datetime.utcnow()
    run.rows_inserted = inserted
    run.charts_collected = charts
    run.status = "ok" if not problems else "error"
    run.notes = "\n".join(problems)[:4000] if problems else None
    db.commit()
    return run
