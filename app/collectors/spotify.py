"""Spotify Podcasts chart collector.

Source: podcastcharts.byspotify.com — the public "Podcast Charts" page. Its
front-end fetches JSON from an endpoint of the form:
  https://podcastcharts.byspotify.com/api/charts/{chart_type}?region={cc}
where chart_type is "top" or "trending" and region is a 2-letter code.

Response is a JSON ARRAY of show objects. Field names have changed before, so
this parser reads defensively and logs anything it can't map. Commonly seen:
  { "showUri": "spotify:show:xxxx", "showName": "...", "showPublisher": "...",
    "showImageUrl": "...", "chartRankMove": "UP|DOWN|NEW|SAME", ... }
Rank is array order.

IMPORTANT (legal): unlike Apple's Marketing Tools feed, this endpoint is a
public-facing site API rather than an officially documented data product. It's
fine for validation and internal use; get a legal sanity-check before it's a
load-bearing dependency of a paid product. The collector is isolated so you can
disable Spotify without touching Apple.
"""

import sys
from datetime import datetime

from tenacity import retry, stop_after_attempt, wait_exponential

from app.collectors.common import client, upsert_show, insert_snapshot
from app.db.models import CollectionRun
from config.countries import MARKETS, SPOTIFY_CHART_TYPES

CHART_URL = "https://podcastcharts.byspotify.com/api/charts/{ctype}?region={cc}"


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, max=15))
def _get_json(http, url):
    r = http.get(url)
    r.raise_for_status()
    return r.json()


def _spotify_id(row):
    uri = row.get("showUri") or row.get("uri") or ""
    # "spotify:show:6E709HRH7XaiZrMfgtNCun" -> "6E709HRH7XaiZrMfgtNCun"
    return uri.split(":")[-1] if uri else None


def collect_spotify(db) -> CollectionRun:
    run = CollectionRun(platform="spotify", status="running",
                        started_at=datetime.utcnow())
    db.add(run)
    db.flush()

    inserted = 0
    charts = 0
    problems = []

    try:
        with client() as http:
            for _apple_cc, cc, _name in MARKETS:
                for ctype in SPOTIFY_CHART_TYPES:
                    url = CHART_URL.format(ctype=ctype, cc=cc)
                    try:
                        payload = _get_json(http, url)
                        if not isinstance(payload, list):
                            # Some deployments wrap it: {"charts":[...]} etc.
                            payload = (
                                payload.get("charts")
                                or payload.get("results")
                                or payload.get("data")
                                or []
                            )
                        rank = 0
                        for row in payload:
                            rank += 1
                            sid = _spotify_id(row)
                            name = (row.get("showName") or row.get("name")
                                    or "Unknown")
                            publisher = (row.get("showPublisher")
                                         or row.get("publisher"))
                            artwork = (row.get("showImageUrl")
                                       or row.get("imageUrl"))
                            show = upsert_show(
                                db, spotify_id=sid, name=name,
                                publisher=publisher, artwork_url=artwork,
                            )
                            if insert_snapshot(
                                db, show_id=show.id, platform="spotify",
                                country=cc, chart=ctype, rank=rank,
                            ):
                                inserted += 1
                        charts += 1
                        db.commit()
                    except Exception as exc:  # noqa: BLE001
                        db.rollback()
                        # tenacity's @retry wraps the underlying error (e.g.
                        # HTTPStatusError, ReadTimeout) in a RetryError once
                        # all attempts are exhausted. Log it clearly with
                        # platform/region/chart context and move on to the
                        # next chart/region rather than crashing the run.
                        print(
                            f"[spotify] error fetching chart cc={cc} chart={ctype}: "
                            f"{exc!r}",
                            file=sys.stderr,
                        )
                        snippet = ""
                        try:
                            snippet = http.get(url).text[:500]
                        except Exception:
                            pass
                        problems.append(f"{cc}/{ctype}: {exc} :: {snippet}")
    except Exception as exc:  # noqa: BLE001 - never let spotify take down the run
        db.rollback()
        print(f"[spotify] fatal error, aborting spotify collection early: {exc!r}",
              file=sys.stderr)
        problems.append(f"fatal: {exc}")

    run.finished_at = datetime.utcnow()
    run.rows_inserted = inserted
    run.charts_collected = charts
    run.status = "ok" if not problems else "error"
    run.notes = "\n".join(problems)[:4000] if problems else None
    db.commit()
    return run
