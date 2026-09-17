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

# "#2555 - Ron White" / "#2555 – Ron White" / "Ep 412: Ron White" — episode
# number, separator, then a GUEST NAME. Very common (JRE, Lex Fridman, etc.).
# We require the trailing part to look like a person's name (1-3 Capitalized
# words) so we don't flag recurring solo segments.
_NUM_NAME_RE = re.compile(
    r"^\s*(?:#|ep\.?\s*|episode\s*)?\d{1,5}\s*[-–—:]\s*([A-Z][A-Za-z.'-]+(?:\s+[A-Z][A-Za-z.'-]+){0,2})\s*$"
)
# Recurring non-guest segment names that fit "#N - Words" but aren't people.
_NOT_A_GUEST = {
    "fight companion", "protect our parks", "jre fight companion",
    "fight night", "q&a", "aftershow", "after show", "live", "compilation",
    "best of", "mailbag", "solo", "monologue", "news", "recap", "highlights",
}


def _looks_like_number_name_guest(title: str) -> bool:
    m = _NUM_NAME_RE.match(title.strip())
    if not m:
        return False
    name = m.group(1).strip()
    low = name.lower()
    # exclude recurring segments and anything with a trailing number
    # ("Protect Our Parks 15" won't match _NUM_NAME_RE anyway, but be safe)
    if any(seg in low for seg in _NOT_A_GUEST):
        return False
    if re.search(r"\d", name):
        return False
    return True

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


# "Guest Name: Topic..." at the START of a title (Diary of a CEO style:
# "Andrew Huberman: My Exact Routine..."). Part before the colon must look like
# a person's name (1-3 capitalized words), not a segment label.
_NAME_TOPIC_RE = re.compile(
    r"^([A-Z][A-Za-z.'-]+(?:\s+[A-Z][A-Za-z.'-]+){1,2})\s*:"
)
_NOT_A_NAME_PREFIX = {
    "bonus", "q&a", "qa", "part", "ep", "episode", "special", "update", "news",
    "recap", "live", "replay", "rewind", "best of", "throwback", "classic",
    "mailbag", "ask me anything", "ama", "trailer", "preview", "announcement",
}

# Description phrases that unambiguously announce a guest (Tier B).
_DESC_GUEST_PHRASES = [
    r"joined by", r"welcomes?\b", r"sits? down with", r"my guest",
    r"our guest", r"special guest", r"returns? to the (?:show|podcast)",
    r"back on the (?:show|podcast)", r"in conversation with",
    r"talks? (?:to|with)\b", r"chats? with\b", r"is joined", r"guest[:\s]",
]
_DESC_GUEST_RE = re.compile("|".join(_DESC_GUEST_PHRASES), re.I)

# A capitalized person-name (2-3 words), for extracting the guest near a phrase.
_PERSON_RE = re.compile(r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,2})\b")


def _norm_name(s: str) -> str:
    return re.sub(r"[^a-z]", "", (s or "").lower())


def _looks_like_name_topic_guest(title: str) -> str | None:
    """If title is 'Guest Name: Topic', return the guest name, else None."""
    m = _NAME_TOPIC_RE.match(title.strip())
    if not m:
        return None
    name = m.group(1).strip()
    low = name.lower()
    if low in _NOT_A_NAME_PREFIX or any(low.startswith(p) for p in _NOT_A_NAME_PREFIX):
        return None
    return name


def _extract_desc_guest(description: str, host_names: set[str]) -> str | None:
    """Tier C: extract a guest name from the description, but ONLY when it sits
    near a Tier-B guest phrase. Excludes host names. Phrase-anchoring keeps this
    from flagging every capitalized word."""
    if not description:
        return None
    m = _DESC_GUEST_RE.search(description)
    if not m:
        return None
    window = description[m.start(): m.start() + 90]
    for pm in _PERSON_RE.finditer(window):
        cand = pm.group(1).strip()
        if _norm_name(cand) in host_names:
            continue
        if cand.lower() in ("this week", "the show", "the podcast"):
            continue
        return cand
    return None


def _host_name_set(show_name: str, publisher: str | None) -> set[str]:
    """Names to treat as the host (never a 'guest'), from show name + publisher.
    'The Diary Of A CEO with Steven Bartlett' -> {'stevenbartlett', ...}."""
    names = set()
    for src in (show_name or "", publisher or ""):
        for pm in _PERSON_RE.finditer(src):
            names.add(_norm_name(pm.group(1)))
        names.add(_norm_name(src))
    return {n for n in names if n}


def analyze_guest_rules(episodes: list[dict], show_name: str = "",
                        publisher: str | None = None) -> dict:
    if not episodes:
        return {"books_guests": False, "guest_frequency": "rare",
                "topics": "", "format_note": None}
    hits = 0
    words = Counter()
    guest_names = []
    host_names = _host_name_set(show_name, publisher)
    for ep in episodes:
        blob = f"{ep.get('title','')} {ep.get('description','')}"
        title = ep.get("title", "")
        desc = ep.get("description", "")
        is_guest = False
        # Tier A: title patterns (highest precision)
        if _GUEST_RE.search(title) or _looks_like_number_name_guest(title):
            is_guest = True
        name_topic = _looks_like_name_topic_guest(title)
        if name_topic and _norm_name(name_topic) not in host_names:
            is_guest = True
            guest_names.append(name_topic)
        # Tier B/C: description guest-phrase (+ extracted name where possible)
        if not is_guest and _DESC_GUEST_RE.search(desc):
            is_guest = True
            dn = _extract_desc_guest(desc, host_names)
            if dn:
                guest_names.append(dn)
        if "interview" in blob.lower():
            is_guest = True
        if is_guest:
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
        "recent_guests": ", ".join(dict.fromkeys(guest_names)) or None,
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


def analyze_guest(show_name: str, episodes: list[dict],
                  publisher: str | None = None) -> dict:
    base = analyze_guest_rules(episodes, show_name=show_name, publisher=publisher)
    llm = analyze_guest_llm(show_name, episodes)
    if llm:
        base.update({k: v for k, v in llm.items() if v not in (None, "")})
    return base
