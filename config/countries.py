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
# are region-wide (not per-category) on the public page. We record both.
SPOTIFY_CHART_TYPES = ["top", "trending"]
