"""Topic expansion map — list-based semantic matching (no embeddings API needed).

A query like "venture capital" expands to related terms {vc, startup, investing,
fundraising, founders, ...} before keyword matching, so it finds shows tagged
"startup investing" even without the literal words. This is the transparent,
zero-cost, zero-dependency alternative to embeddings — you can see exactly why a
match happened and fix it by editing this list.

How it's used: match_guests expands the user's query terms through this map,
then matches the expanded set against each show's topic/guest text.

Growing it: when a real query misses obvious shows, add the term + its related
words here and it works immediately (no re-embed, no backfill). Keep entries
lowercase. Bidirectional expansion is handled at load time — you only need to
list a cluster once.
"""

# Each key expands to its related terms. Clusters are grouped by theme for
# maintainability; the loader also makes expansion symmetric within a cluster.
_CLUSTERS = [
    # business / startups / finance
    ["venture capital", "vc", "startup", "startups", "founder", "founders",
     "fundraising", "seed", "series a", "angel investing", "entrepreneurship",
     "entrepreneur", "small business", "bootstrapping", "saas", "scaling"],
    ["investing", "investment", "finance", "personal finance", "stocks",
     "stock market", "wealth", "money", "trading", "portfolio", "assets",
     "financial freedom", "fire", "retirement"],
    ["crypto", "cryptocurrency", "bitcoin", "ethereum", "web3", "blockchain",
     "defi", "nft", "digital assets"],
    ["marketing", "advertising", "branding", "growth", "seo", "content marketing",
     "social media marketing", "copywriting", "sales", "demand gen"],
    ["real estate", "property", "realtor", "housing", "rentals", "landlord",
     "reit", "flipping"],

    # tech
    ["ai", "artificial intelligence", "machine learning", "ml", "deep learning",
     "llm", "generative ai", "neural networks", "data science", "automation"],
    ["technology", "tech", "software", "engineering", "programming", "coding",
     "developer", "computer science", "cybersecurity", "infrastructure"],
    ["product", "product management", "ux", "design", "user experience",
     "product design"],

    # health / science
    ["health", "wellness", "wellbeing", "healthspan"],
    ["longevity", "aging", "anti-aging", "lifespan", "healthspan",
     "biohacking", "peptides"],
    ["fitness", "exercise", "training", "strength", "gym", "workout",
     "bodybuilding", "athletics", "running", "endurance"],
    ["nutrition", "diet", "food", "eating", "supplements", "keto", "fasting",
     "gut health", "metabolism"],
    ["mental health", "psychology", "therapy", "anxiety", "depression",
     "mindfulness", "meditation", "stress", "wellbeing", "self-help",
     "personal development", "mindset"],
    ["science", "research", "physics", "biology", "chemistry", "astronomy",
     "astrophysics", "space", "neuroscience", "genetics", "medicine",
     "scientific"],
    ["medicine", "medical", "doctor", "healthcare", "clinical", "disease",
     "public health", "surgery"],

    # society / culture / news
    ["politics", "political", "government", "policy", "elections", "democracy",
     "geopolitics", "foreign policy", "current events", "news"],
    ["history", "historical", "ancient history", "war", "military history",
     "archaeology", "civilization"],
    ["true crime", "crime", "murder", "investigation", "detective", "forensics",
     "cold case", "criminal"],
    ["philosophy", "ethics", "religion", "spirituality", "theology",
     "existential", "stoicism", "faith"],
    ["society", "culture", "sociology", "social issues", "identity",
     "relationships", "dating", "parenting", "family"],

    # entertainment / arts / lifestyle
    ["comedy", "humor", "funny", "standup", "stand-up", "comedian", "improv"],
    ["music", "musician", "songwriting", "band", "hip hop", "rap", "producer",
     "artist"],
    ["film", "movies", "cinema", "filmmaking", "hollywood", "tv", "television",
     "screenwriting", "acting"],
    ["sports", "athlete", "football", "basketball", "baseball", "soccer",
     "mma", "boxing", "nba", "nfl", "coaching"],
    ["gaming", "video games", "esports", "game development", "streamer"],
    ["food", "cooking", "chef", "recipes", "restaurant", "culinary", "baking"],
    ["travel", "adventure", "outdoors", "hiking", "exploration", "nomad"],

    # career / education
    ["career", "leadership", "management", "productivity", "workplace",
     "professional development", "coaching", "hr", "remote work"],
    ["education", "learning", "teaching", "academia", "students", "school",
     "edtech"],
    ["writing", "author", "books", "publishing", "storytelling", "journalism",
     "creative writing", "novelist"],

    # military / first responders (shows like Shawn Ryan)
    ["military", "veteran", "special forces", "navy seal", "army", "combat",
     "national security", "defense", "intelligence", "cia", "fbi", "law enforcement"],
]


def _build_expansion():
    """Make expansion symmetric: every term in a cluster maps to all others."""
    m = {}
    for cluster in _CLUSTERS:
        terms = set(cluster)
        for t in cluster:
            m.setdefault(t, set()).update(terms - {t})
    return m


EXPANSION = _build_expansion()


def expand_terms(terms: set[str], raw_query: str = "") -> set[str]:
    """Given a set of query terms (and the raw query for multiword lookups),
    return the terms plus all related terms from the map."""
    out = set(terms)
    # multiword phrases: check the raw query against multiword keys
    low = (raw_query or "").lower()
    for key, related in EXPANSION.items():
        if " " in key and key in low:
            out.add(key)
            out.update(related)
    # single tokens
    for t in list(terms):
        if t in EXPANSION:
            out.update(EXPANSION[t])
    # also split multiword expansions into tokens so keyword overlap can hit them
    tokenized = set()
    for term in out:
        tokenized.update(term.split())
    return out | tokenized
