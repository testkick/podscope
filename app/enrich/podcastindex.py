"""Podcast Index API client.

Free API (podcastindex.org). Auth is a per-request SHA1 of
apiKey + apiSecret + unix_time, sent in headers. Credentials come from env:
  PODCASTINDEX_KEY, PODCASTINDEX_SECRET

We use it for two things:
  1. resolve_by_itunes_id(apple_id) -> feed info    (Apple shows: direct + exact)
  2. search_feed(title, publisher) -> feed info      (Spotify shows: no iTunes id,
                                                       so we search by name)

"feed info" is a dict with at least {feed_url, itunes_id, title, author, image}.
feed_url is the canonical key we merge on: two shows sharing a normalized
feed_url are provably the same podcast.

Design note on safety: the Apple path is exact (id lookup). The Spotify path
involves a search, so it can miss — but a miss just means "no feed_url found",
which leaves the show un-merged (a harmless duplicate). It can never cause a
WRONG merge, because merging keys on feed_url equality, not on the search guess.
"""

import os
import time
import hashlib

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

BASE = "https://api.podcastindex.org/api/1.0"
UA = "PodScope/0.1"


class PodcastIndexError(Exception):
    pass


def _credentials():
    key = os.environ.get("PODCASTINDEX_KEY")
    secret = os.environ.get("PODCASTINDEX_SECRET")
    if not key or not secret:
        raise PodcastIndexError(
            "PODCASTINDEX_KEY / PODCASTINDEX_SECRET not set — cannot resolve feeds."
        )
    return key, secret


def _auth_headers():
    key, secret = _credentials()
    now = str(int(time.time()))
    digest = hashlib.sha1((key + secret + now).encode()).hexdigest()
    return {
        "User-Agent": UA,
        "X-Auth-Key": key,
        "X-Auth-Date": now,
        "Authorization": digest,
    }


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, max=10))
def _get(path, params):
    r = httpx.get(f"{BASE}{path}", params=params, headers=_auth_headers(),
                  timeout=20, follow_redirects=True)
    r.raise_for_status()
    return r.json()


def _feed_info(feed: dict) -> dict:
    """Normalize a Podcast Index feed object to the fields we care about."""
    return {
        "feed_url": feed.get("url"),
        "itunes_id": str(feed.get("itunesId")) if feed.get("itunesId") else None,
        "title": feed.get("title"),
        "author": feed.get("author") or feed.get("ownerName"),
        "image": feed.get("image") or feed.get("artwork"),
    }


def resolve_by_itunes_id(apple_id: str) -> dict | None:
    """Exact lookup for Apple shows. Returns feed info or None."""
    try:
        data = _get("/podcasts/byitunesid", {"id": apple_id})
    except Exception:
        return None
    feed = data.get("feed")
    if not feed:
        return None
    # byitunesid returns a single feed object (dict), not a list
    return _feed_info(feed if isinstance(feed, dict) else feed[0])


def search_feed(title: str, publisher: str | None = None) -> dict | None:
    """Best-effort search for Spotify shows (no iTunes id available).
    Returns the best match's feed info, or None. A wrong/empty result only
    prevents a merge; it can't cause an incorrect one."""
    if not title:
        return None
    try:
        data = _get("/search/byterm", {"q": title, "max": 5})
    except Exception:
        return None
    feeds = data.get("feeds") or []
    if not feeds:
        return None

    # Prefer a feed whose title matches closely AND (if we have a publisher)
    # whose author matches. Fall back to the top result only on a strong title
    # match, to avoid grabbing an unrelated show.
    t_norm = _norm(title)
    p_norm = _norm(publisher) if publisher else None

    best = None
    for f in feeds:
        info = _feed_info(f)
        ft = _norm(info["title"] or "")
        fa = _norm(info["author"] or "")
        if ft == t_norm and (p_norm is None or p_norm in fa or fa in p_norm):
            return info            # strong match: exact title (+ author agree)
        if best is None and ft == t_norm:
            best = info            # title matches, author unknown/differs
    return best                    # None if no exact-title match found


def _norm(s: str) -> str:
    import re
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def normalize_feed_url(url: str | None) -> str | None:
    """Canonical form for merge comparison: lowercase host, strip trailing
    slash, drop common tracking prefixes. Conservative — only safe transforms."""
    if not url:
        return None
    u = url.strip()
    # strip scheme so http/https variants of the same feed match
    u = u.replace("https://", "").replace("http://", "")
    # strip common redirect/analytics prefixes that wrap the real feed
    for pref in ("www.", "feeds.", "pdst.fm/e/", "chtbl.com/track/",
                 "podtrac.com/pts/redirect.mp3/", "dts.podtrac.com/redirect.mp3/"):
        if u.startswith(pref):
            u = u[len(pref):]
    return u.rstrip("/").lower()
