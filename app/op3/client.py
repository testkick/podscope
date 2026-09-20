"""OP3 (Open Podcast Prefix Project) client — real, auditable download data.

IMPORTANT SCOPE: OP3 only has data for shows whose podcasters added the OP3
prefix (op3.dev/e/) to their feed — ~5,200 shows as of 2026. It is NOT a
universal download database. We use it as a ground-truth set: for the subset of
our charting shows that use OP3, we get verified downloads, which lets us
(a) show real numbers on those pages and (b) later calibrate our reach model.

Auth: bearer token via env OP3_API_TOKEN. A shared preview token exists for
testing but is rate-limited; get a real key from op3.dev for production.

We look a show up by its podcast GUID (from the feed) or feed URL, then pull
monthly/weekly download counts. If a show isn't measured by OP3, the lookup
returns None — that's expected for most shows, not an error.
"""

import os
import base64
import httpx

BASE = "https://op3.dev/api/1"
_TOKEN = os.environ.get("OP3_API_TOKEN", "preview07ce")  # preview token fallback
UA = "PodScope/0.1"


def op3_enabled() -> bool:
    # preview token works but is rate-limited; treat as enabled either way
    return bool(_TOKEN)


def _headers():
    return {"Authorization": f"Bearer {_TOKEN}", "User-Agent": UA}


def _get(path, params=None):
    r = httpx.get(f"{BASE}{path}", params=params or {}, headers=_headers(),
                  timeout=30, follow_redirects=True)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json()


def lookup_show(*, podcast_guid: str | None = None, feed_url: str | None = None):
    """Resolve a show in OP3 by podcast GUID or feed URL. Returns the OP3 show
    object (with showUuid) or None if OP3 doesn't measure it."""
    ident = None
    if podcast_guid:
        ident = podcast_guid
    elif feed_url:
        # OP3 accepts a base64url-encoded feed URL as the identifier
        ident = base64.urlsafe_b64encode(feed_url.encode()).decode().rstrip("=")
    if not ident:
        return None
    try:
        data = _get(f"/shows/{ident}")
    except Exception:
        return None
    if not data:
        return None
    # endpoint returns the show object directly (has showUuid) or wraps it
    return data.get("show", data)


def show_downloads(show_uuid: str) -> dict | None:
    """Monthly + weekly download counts for an OP3 show UUID. Returns
    {monthly_avg, recent_month, weekly_avg, raw} or None."""
    try:
        data = _get("/queries/show-download-counts", {"showUuid": show_uuid})
    except Exception:
        return None
    if not data:
        return None
    # response shape: { "showDownloadCounts": { "<showUuid>": {
    #   "monthlyDownloads": {...}, "weeklyDownloads": {...} } } } (defensive parse)
    block = None
    counts = data.get("showDownloadCounts") or data.get("rows") or data
    if isinstance(counts, dict):
        block = counts.get(show_uuid) or next(iter(counts.values()), None) \
            if counts else None
    if not isinstance(block, dict):
        block = data  # last resort

    monthly = block.get("monthlyDownloads") or {}
    weekly = block.get("weeklyDownloads") or {}
    m_vals = [v for v in monthly.values() if isinstance(v, (int, float))]
    w_vals = [v for v in weekly.values() if isinstance(v, (int, float))]
    if not m_vals and not w_vals:
        return None
    return {
        "recent_month": m_vals[-1] if m_vals else None,
        "monthly_avg": round(sum(m_vals) / len(m_vals)) if m_vals else None,
        "weekly_avg": round(sum(w_vals) / len(w_vals)) if w_vals else None,
        "months_measured": len(m_vals),
    }
