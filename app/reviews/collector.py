"""Apple review-origin geography collector.

Apple exposes customer reviews PER STOREFRONT (country) via a public, keyless RSS
feed. The *distribution of reviews across storefronts* is a real geographic
signal for where a show's audience is — a show with mostly US reviews skews
American; heavy UK/AU reviews mean international reach. Nobody surfaces this
cleanly, and it's measured (from Apple), not modeled — consistent with the
"measured, not modeled" posture.

Feed: https://itunes.apple.com/{cc}/rss/customerreviews/id={apple_id}/json
  Response: { "feed": { "entry": [ ...reviews... ] } }
  The first entry can be show metadata; review entries have "im:rating" etc.
  Apple paginates ~50/page; we read page 1 and use its count as a sample signal
  (we want RELATIVE distribution across countries, not exact totals).

We store per-(show, country) review counts + average rating. The geo signal is
the SHARE of reviews per country, computed at read time.

Defensive parsing (lesson from OP3): the feed shape varies — entry may be a
list, a dict, or absent; ratings may be strings. Handle all without crashing.
"""

import os
import time

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from config.countries import MARKETS

UA = "PodScope/0.1"
FEED = "https://itunes.apple.com/{cc}/rss/customerreviews/page=1/id={aid}/sortby=mostrecent/json"
DELAY = float(os.environ.get("REVIEW_DELAY", "0.3"))


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, max=10))
def _get(url):
    r = httpx.get(url, headers={"User-Agent": UA}, timeout=20, follow_redirects=True)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json()


def _entries(payload):
    """Return the list of review entries, defensively."""
    if not isinstance(payload, dict):
        return []
    feed = payload.get("feed")
    if not isinstance(feed, dict):
        return []
    entries = feed.get("entry")
    if entries is None:
        return []
    if isinstance(entries, dict):
        entries = [entries]
    if not isinstance(entries, list):
        return []
    # the first entry is sometimes the app/show metadata (has "im:name" but no
    # "im:rating") — keep only things that look like reviews (have a rating).
    reviews = []
    for e in entries:
        if isinstance(e, dict) and e.get("im:rating"):
            reviews.append(e)
    return reviews


def _avg_rating(reviews):
    vals = []
    for e in reviews:
        r = e.get("im:rating")
        label = r.get("label") if isinstance(r, dict) else r
        try:
            vals.append(int(label))
        except (TypeError, ValueError):
            pass
    return round(sum(vals) / len(vals), 2) if vals else None


def collect_reviews_for_show(apple_id: str) -> list[dict]:
    """Pull page-1 review count + avg rating per storefront for one show.
    Returns [{country, review_count, avg_rating}] for storefronts with reviews."""
    out = []
    for cc, _sp, _name in MARKETS:
        time.sleep(DELAY)
        try:
            payload = _get(FEED.format(cc=cc, aid=apple_id))
        except Exception:
            continue
        if not payload:
            continue
        reviews = _entries(payload)
        if not reviews:
            continue
        out.append({
            "country": cc,
            "review_count": len(reviews),          # page-1 sample (relative signal)
            "avg_rating": _avg_rating(reviews),
        })
    return out
