"""What we track.

Start deliberately small. Every (country x category x platform) tuple is a
request every 6 hours; expand this list once the pipeline is proven rather than
starting at 100 countries and debugging rate limits on day one.

Apple uses two-letter storefront codes. Spotify uses similar region codes but
not identical — the map below keeps them aligned so a "market" means the same
place across platforms in the DB.

Apple genre IDs (podcasts): the top-level podcast genre is 26; sub-genres hang
off it. A few high-value ones are listed. Full list:
https://podcasters.apple.com/support/1691-apple-podcasts-categories
"""

# (apple_storefront, spotify_region, human_name)
MARKETS = [
    ("us", "us", "United States"),
    ("ca", "ca", "Canada"),
    ("gb", "gb", "United Kingdom"),
    ("au", "au", "Australia"),
    ("de", "de", "Germany"),
    ("fr", "fr", "France"),
]

# Apple podcast genre ids. None = "all/top overall" (no genre filter).
# id 26 is the Podcasts root; sub-genres are the useful market segments.
APPLE_GENRES = {
    None: "Top Overall",
    "1321": "Business",
    "1489": "News",
    "1303": "Comedy",
    "1512": "Health & Fitness",
    "1318": "Technology",
    "1487": "History",
    "1324": "Society & Culture",
}

# Spotify's public chart endpoint exposes a smaller set. "top" and "trending"
# Spotify's public chart slugs, as used by podcastcharts.byspotify.com.
# The chart type MUST match Spotify's own slug. The site uses "top-podcasts"
# (NOT "top") — requesting "top" fails every time, which is why the top charts
# were missing while "trending" worked. Verified against the live site URLs
# (e.g. podcastcharts.byspotify.com/us/top-podcasts).
#
# Spotify also publishes per-category charts (top 50 per category, select
# countries only) at slugs like "news", "comedy", etc. — the equivalent of the
# Apple genre charts. Add them here once "top-podcasts" is confirmed landing.
# NOTE: this rides an undocumented frontend JSON endpoint Spotify doesn't
# officially support; it can change without notice (Apple's feed is the stable
# spine; treat Spotify as best-effort).
SPOTIFY_CHART_TYPES = ["top-podcasts", "trending"]

