"""Fetch episodes and their text for a show, cheapest source first.

Tier 1  Podcasting 2.0 <podcast:transcript> tag  -> free, full transcript
Tier 2  episode <description>/show notes          -> free, usually carries the
                                                     host-read ad + promo URL
Tier 3  speech-to-text on the audio               -> paid; STUBBED here behind a
                                                     clean interface (transcribe()).
                                                     Wire in Whisper/Deepgram when
                                                     you decide on cost + key.

We resolve a show's RSS feed URL from Podcast Index (or a stored feed_url). For
the MVP the feed URL can also be passed directly so the module is testable
without the directory crawl.
"""

import re
from datetime import datetime
from xml.etree import ElementTree as ET

import httpx

UA = {"User-Agent": "PodScope/0.1 (+https://podscope.example)"}
NS = {
    "itunes": "http://www.itunes.com/dtds/podcast-1.0.dtd",
    "podcast": "https://podcastindex.org/namespace/1.0",
    "content": "http://purl.org/rss/1.0/modules/content/",
}

_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(s: str | None) -> str:
    if not s:
        return ""
    return _TAG_RE.sub(" ", s).replace("&amp;", "&").strip()


def fetch_feed(feed_url: str, max_episodes: int = 12) -> dict:
    """Return {'owner_email', 'episodes': [ {guid,title,published,audio_url,
    description,transcript_url}, ... ]} for the most recent episodes."""
    if feed_url.startswith("file://"):
        with open(feed_url[7:], "rb") as fh:
            content = fh.read()
    else:
        r = httpx.get(feed_url, headers=UA, timeout=30, follow_redirects=True)
        r.raise_for_status()
        content = r.content
    root = ET.fromstring(content)
    channel = root.find("channel")
    if channel is None:
        return {"owner_email": None, "episodes": []}

    owner = channel.find("itunes:owner", NS)
    owner_email = None
    if owner is not None:
        em = owner.find("itunes:email", NS)
        owner_email = em.text.strip() if em is not None and em.text else None

    episodes = []
    for item in channel.findall("item")[:max_episodes]:
        guid_el = item.find("guid")
        title_el = item.find("title")
        desc_el = item.find("description")
        content_el = item.find("content:encoded", NS)
        pub_el = item.find("pubDate")
        enc = item.find("enclosure")
        tr = item.find("podcast:transcript", NS)

        published = None
        if pub_el is not None and pub_el.text:
            for fmt in ("%a, %d %b %Y %H:%M:%S %z", "%a, %d %b %Y %H:%M:%S %Z"):
                try:
                    published = datetime.strptime(pub_el.text.strip(), fmt).date()
                    break
                except ValueError:
                    continue

        description = _strip_html(
            (content_el.text if content_el is not None else None)
            or (desc_el.text if desc_el is not None else None)
        )
        episodes.append({
            "guid": (guid_el.text if guid_el is not None else title_el.text) or "",
            "title": (title_el.text if title_el is not None else "Untitled").strip(),
            "published": published,
            "audio_url": enc.get("url") if enc is not None else None,
            "description": description,
            "transcript_url": tr.get("url") if tr is not None else None,
        })
    return {"owner_email": owner_email, "episodes": episodes}


def get_episode_text(ep: dict) -> tuple[str, str] | None:
    """Return (source, text) for one episode dict, cheapest first, or None."""
    # Tier 1: transcript tag
    if ep.get("transcript_url"):
        try:
            r = httpx.get(ep["transcript_url"], headers=UA, timeout=30,
                          follow_redirects=True)
            if r.status_code == 200 and r.text.strip():
                return ("transcript", _strip_html(r.text)[:40000])
        except Exception:
            pass
    # Tier 2: show notes / description
    if ep.get("description") and len(ep["description"]) > 40:
        return ("notes", ep["description"][:40000])
    # Tier 3: STT (stubbed)
    if ep.get("audio_url"):
        text = transcribe(ep["audio_url"])
        if text:
            return ("stt", text[:40000])
    return None


def transcribe(audio_url: str) -> str | None:
    """STUB. Wire in Whisper/Deepgram/Groq here when cost + key are decided.
    Returning None means Tier 3 is skipped and only free sources are used."""
    return None
