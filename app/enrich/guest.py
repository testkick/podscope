"""Decide whether a show books guests, how often, and on what topics — the
signal behind 'best pods for a guest appearance'.

Rule-based pass (no key): interview shows announce guests in episode titles and
notes in very regular ways — 'with <Name>', 'ft. <Name>', 'my conversation with',
'<Name> on <topic>', 'interview'. We score guest_frequency from how many recent
episodes look guest-driven, and pull rough topics from title/notes keywords.

LLM pass (optional): summarizes the show's booking style and drafts a one-line
suitability note ('books founders and operators; pitch via owner email'), which
is what a guest-booking user actually wants.

Contact email comes straight from the RSS <itunes:owner><itunes:email> — public,
already fetched by transcripts.fetch_feed. That's the clean, defensible contact
rail for guest booking (brand/sponsor contacts are a separate, harder problem).
"""

import os
import re
import json
from collections import Counter

_GUEST_TITLE = [
    r"\bw/\s*[A-Z]",                              # "w/ Stavros Halkias" (most common!)
    r"\bwith\s+[A-Z][a-z]+",                      # "with Adam Ray"
    r"\bft\.?\s+[A-Z]",                            # "ft. Jane"
    r"\bfeat\.?\s+[A-Z]",                          # "feat. Jane"
    r"\bfeaturing\s+[A-Z]",
    r"\bguest[:\s]",                              # "Guest: ...", "guest "
    r"\binterview\b",
    r"\bconversation with\b",
    r"\b(?:my|our)\s+chat with\b",
    r"\bsits?\s+down\s+with\b",                   # "sits down with"
    r"\bjoins?\s+(?:the|us|me)\b",                # "X joins the show"
    r"\b[A-Z][a-z]+\s+[A-Z][a-z]+\s+on\b",       # "Jane Smith on ..."
]
_GUEST_RE = re.compile("|".join(_GUEST_TITLE))

# very light topic vocabulary; the LLM pass does better when available
_TOPIC_WORDS = [
    "startup", "founder", "business", "marketing", "technology", "ai", "crypto",
    "science", "health", "fitness", "psychology", "history", "politics",
    "finance", "investing", "design", "product", "music", "film", "sports",
    "comedy", "philosophy", "career", "leadership", "climate", "medicine",
]


def _freq_label(ratio: float) -> str:
    if ratio >= 0.6:
        return "every"
    if ratio >= 0.25:
        return "often"
    return "rare"


def analyze_guest_rules(episodes: list[dict]) -> dict:
    if not episodes:
        return {"books_guests": False, "guest_frequency": "rare",
                "topics": "", "format_note": None}
    hits = 0
    words = Counter()
    for ep in episodes:
        blob = f"{ep.get('title','')} {ep.get('description','')}"
        if _GUEST_RE.search(ep.get("title", "")) or "interview" in blob.lower():
            hits += 1
        low = blob.lower()
        for w in _TOPIC_WORDS:
            if re.search(rf"\b{re.escape(w)}\b", low):
                words[w] += 1
    ratio = hits / len(episodes)
    freq = _freq_label(ratio)
    topics = ", ".join(w for w, _ in words.most_common(6))
    return {
        # Consistent with frequency: a show that's "often"/"every" books guests.
        # (A separate threshold previously could report "often" while also saying
        # books_guests=False — contradictory on small samples.)
        "books_guests": freq in ("every", "often") or hits >= 2,
        "guest_frequency": freq,
        "topics": topics,
        "format_note": f"{hits} of {len(episodes)} recent episodes look guest-driven",
    }


def analyze_guest_llm(show_name: str, episodes: list[dict]) -> dict | None:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    try:
        import anthropic
    except ImportError:
        return None
    titles = "\n".join(f"- {e.get('title','')}" for e in episodes[:15])
    prompt = (
        f"Podcast: {show_name}\nRecent episode titles:\n{titles}\n\n"
        "From these titles, judge this show as a GUEST BOOKING target. "
        "Return ONLY JSON: {\"books_guests\":bool,"
        "\"guest_frequency\":\"every|often|rare\","
        "\"topics\":\"comma-separated\","
        "\"suitability_note\":\"one line: who should pitch and why\"}."
    )
    try:
        client = anthropic.Anthropic()
        resp = client.messages.create(
            model="claude-sonnet-4-6", max_tokens=400,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = "".join(b.text for b in resp.content if b.type == "text").strip()
        raw = raw.removeprefix("```json").removeprefix("```").removesuffix("```")
        return json.loads(raw)
    except Exception:
        return None


def analyze_guest(show_name: str, episodes: list[dict]) -> dict:
    base = analyze_guest_rules(episodes)
    llm = analyze_guest_llm(show_name, episodes)
    if llm:
        base.update({k: v for k, v in llm.items() if v not in (None, "")})
    return base
