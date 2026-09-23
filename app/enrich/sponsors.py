"""Detect sponsors in episode text.

Two passes:
  1. RULE-BASED (always runs, no API key): catches the formulaic host-read ad
     pattern — "brought to you by X", "sponsored by X", "go to X.com/show",
     "use code SHOW at X". Extracts brand, promo code, promo URL. This alone
     gets you most host-read podcast ads because they're highly templated.
  2. LLM PASS (optional, runs if ANTHROPIC_API_KEY is set): re-reads the same
     text to catch brands the rules missed, and to CLASSIFY each candidate as
     host_read / mention / affiliate with a confidence band — the false-positive
     guard we care about (an affiliate link or a passing mention is NOT a paid
     deal, and must not be counted as one).

Output: list of dicts {brand, promo_code, promo_url, deal_type, confidence,
evidence}. deal_type in {host_read, mention, affiliate}; confidence in
{high, med, low}. Everything carries an evidence snippet — like SponsorRadar,
we surface the evidence and never claim a deal is brand-confirmed.
"""

import os
import re
import json

# Phrases that signal a paid read (high-signal) vs. a mere mention.
_READ_PATTERNS = [
    r"brought to you by\s+([A-Z][\w&.\- ]{1,40})",
    r"sponsored by\s+([A-Z][\w&.\- ]{1,40})",
    r"today'?s (?:episode|sponsor)\s+is\s+([A-Z][\w&.\- ]{1,40})",
    r"thanks to\s+([A-Z][\w&.\- ]{1,40})\s+for sponsoring",
    r"this episode is supported by\s+([A-Z][\w&.\- ]{1,40})",
]
_CODE_RE = re.compile(r"\b(?:code|coupon|promo(?:\s*code)?)\s+([A-Z0-9]{3,20})\b")
_URL_RE = re.compile(r"\b((?:[a-z0-9-]+\.)+[a-z]{2,}(?:/[\w\-./?%&=]*)?)\b", re.I)

# Common non-sponsor domains to ignore when guessing a promo URL.
_URL_STOP = {"youtube.com", "youtu.be", "twitter.com", "x.com", "instagram.com",
             "facebook.com", "tiktok.com", "spotify.com", "apple.com",
             "patreon.com", "linktr.ee", "bit.ly"}


def _norm(brand: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", brand.lower())


_BRAND_CUT = re.compile(
    r"\b(is|are|was|has|have|helps?|makes?|offers?|the|who|which|and|for|to|"
    r"go|visit|use|get|head|check|today|this|that|dot\s?com|com)\b",
    re.I,
)


def _clean_brand(raw: str) -> str:
    import html as _html
    b = _html.unescape(raw or "").replace("\xa0", " ").strip()
    # cut trailing ad copy after a dash/pipe: "Kalshi – Download" -> "Kalshi",
    # "Tecovas – Right now" -> "Tecovas", "HSBC UK – https" -> "HSBC UK".
    b = re.split(r"\s+[–—|-]\s+", b, 1)[0].strip()
    # cut at the first sentence/punctuation boundary
    b = re.split(r"[.,!?;:/\n]", b, 1)[0].strip()
    # cut at the first action verb / filler word ("Squarespace Go" -> "Squarespace")
    m = _BRAND_CUT.search(b)
    if m and m.start() > 0:
        b = b[: m.start()].strip()
    b = b.rstrip(".,!—-–").strip()
    return b[:60]


# Junk that leaks in as "brands": HTML entities, ad-copy fragments, call-to-action
# words, generic filler. Matched on the normalized key so spacing/case don't matter.
_BRAND_STOPLIST = {
    # HTML entities that survived stripping
    "nbsp", "amp", "quot", "apos", "lt", "gt", "mdash", "ndash",
    # call-to-action / ad-copy fragments
    "visit", "our", "more", "look", "get", "go", "headto", "head", "code",
    "freelistening", "free", "listening", "use", "check", "checkout", "today",
    "start", "startyour", "tryit", "try", "learnmore", "learn", "shopnow",
    "shop", "download", "downloadthe", "sign", "signup", "join", "click",
    "save", "getstarted", "findyour", "find", "call", "text", "the", "your",
    "you", "we", "us", "me", "and", "for", "with", "at", "on", "in", "to",
    "new", "now", "here", "visitthe", "plus", "also", "just", "only",
    # generic revenue/return phrases seen in the data
    "amonthinrevenue", "dayreturns", "365dayreturns", "moneyback",
    "freeshipping", "freetrial", "offyourfirst", "percentoff",
    # second-pass fragments seen in the brand directory
    "going", "apply", "especially", "budget", "support", "twit", "withsso",
    "banking", "career", "upgradeyoureveryday", "anamericanoriginal",
    "ifyouvebeeninbusiness", "cardiffifyouvebeeninbusiness", "disclaimer",
    "https", "http", "www", "download", "downloadcashapp", "rightnow",
    "simpleingredients", "eqs", "sponsoredby", "thisepisode", "brought",
    "broughttoyou", "promo", "promocode", "offer", "deal", "discount",
    "limited", "limitedtime", "exclusive", "everyday", "yourfirst",
}


def is_valid_brand(display: str, brand_norm: str | None = None) -> bool:
    """True if this looks like a real brand, not ad-copy junk. Conservative —
    leans toward KEEPING ambiguous names (real brands like 'Article', 'Factor',
    'Quince' look word-like), only rejecting clear junk."""
    if not display or not display.strip():
        return False
    norm = brand_norm if brand_norm is not None else _norm(display)
    if not norm:
        return False
    # 1. stop-list (HTML entities, ad-copy fragments, filler)
    if norm in _BRAND_STOPLIST:
        return False
    # 2. starts with a digit / mostly numeric ("365 day returns", "000 a month")
    if display.strip()[0].isdigit():
        return False
    if sum(c.isdigit() for c in norm) > len(norm) / 2:
        return False
    # 3. too short to be a brand (single/double char)
    if len(norm) < 3:
        return False
    # 4. all-lowercase multi-word fragments are almost always ad copy
    #    ("free listening", "head to") — real brands are capitalized or one token.
    #    Keep single lowercase tokens (could be a stylized brand) unless stoplisted.
    if " " in display.strip() and display.strip().islower():
        return False
    # 5. leftover HTML-entity remnants or URLs -> junk ("Europe&apos;s", "HSBC UK – https")
    low = display.lower()
    if any(x in low for x in ("&apos", "&amp", "&quot", "&nbsp", "http", "www.", "://")):
        return False
    # 6. contains a dash followed by ad-copy that survived ("Cardiff – If you've...")
    #    or is just too long/wordy to be a brand name (>5 words = a sentence).
    if len(display.split()) > 5:
        return False
    # 7. disclaimer / NMLS / regulatory boilerplate
    if any(x in low for x in ("disclaimer", "nmls", "terms apply", "see site")):
        return False
    return True


# Matches an explicit sponsor-list header, the dominant real format:
#   "Thank you to our Sponsors: Mountain Dew, Draft Kings, Acorns & Talkspace"
#   "This week's sponsors: X, Y and Z"   "Sponsored by: A, B, C"
_SPONSOR_LIST_RE = re.compile(
    r"(?:thank(?:s| you)?(?:\s+to)?(?:\s+our)?\s+sponsors?"
    r"|this (?:week'?s?|episode'?s?) sponsors?"
    r"|sponsored by|brought to you by|our sponsors?|sponsors?)\s*[:\-]\s*"
    r"([^\n\r]{3,240})",
    re.I,
)

# Split a brand list on commas, ampersands, "and", slashes.
_LIST_SPLIT_RE = re.compile(r"\s*(?:,|&|/|\band\b)\s*", re.I)

# Words that signal the sponsor list has ended (so we don't swallow trailing prose)
_LIST_STOPWORDS = ("http", "www.", "youtube", "subscribe", "merch", "instagram",
                    "twitter", "tickets", "patreon", "•")


def _extract_brand_details(text: str, brand: str) -> tuple[str | None, str | None]:
    """Find a promo code and URL in the brand's OWN detail bullet. Bullets look
    like 'Brand: ....' — we take the text from this brand's bullet up to the next
    bullet (next 'Word:' or newline), so brands don't steal each other's codes."""
    # find the brand's detail bullet: "Brand:" (not the header-list mention)
    m = re.search(re.escape(brand) + r"\s*[:*]", text)
    start = m.end() if m else text.lower().find(brand.lower())
    if start == -1:
        return None, None
    # bullet ends at the next "Capitalized Word:" bullet or double-space/newline
    rest = text[start: start + 400]
    end_m = re.search(r"\s{2,}[A-Z][A-Za-z ]{2,20}\s*[:*]", rest)
    window = rest[: end_m.start()] if end_m else rest

    code_m = _CODE_RE.search(window)
    url = None
    for um in _URL_RE.finditer(window):
        host = um.group(1).lower().split("/")[0]
        if host not in _URL_STOP and "." in host:
            url = um.group(1)
            break
    return (code_m.group(1) if code_m else None), url


def detect_sponsor_list(text: str) -> list[dict]:
    """Parse the explicit 'Sponsors: A, B, C' header format."""
    out = {}
    for m in _SPONSOR_LIST_RE.finditer(text):
        raw_list = m.group(1)
        # The list ends where the per-brand detail bullets begin. Cut at the
        # first "  Word:" bullet, or at the first stopword (URLs/socials), or at
        # a period — whichever comes first — so the last list item doesn't merge
        # with the following sentence.
        low = raw_list.lower()
        cut = len(raw_list)
        bullet = re.search(r"\s{2,}[A-Z][A-Za-z ]{2,20}\s*[:*]", raw_list)
        if bullet:
            cut = min(cut, bullet.start())
        for sw in _LIST_STOPWORDS:
            p = low.find(sw)
            if p != -1:
                cut = min(cut, p)
        dot = raw_list.find(". ")
        if dot != -1:
            cut = min(cut, dot)
        raw_list = raw_list[:cut]

        for piece in _LIST_SPLIT_RE.split(raw_list):
            brand = _clean_brand(piece)
            if len(brand) < 2 or _norm(brand) in ("the", "our", "us", "me"):
                continue
            code, url = _extract_brand_details(text, brand)
            out[_norm(brand)] = {
                "brand": brand,
                "brand_norm": _norm(brand),
                "promo_code": code,
                "promo_url": url,
                "deal_type": "host_read",
                "confidence": "high" if (code or url) else "med",
                "evidence": m.group(0)[:240],
            }
    return list(out.values())


def detect_rule_based(text: str) -> list[dict]:
    found: dict[str, dict] = {}
    # 1) explicit sponsor-list header (dominant real format)
    for d in detect_sponsor_list(text):
        found[d["brand_norm"]] = d
    # 2) inline "brought to you by X" style single reads
    for pat in _READ_PATTERNS:
        for m in re.finditer(pat, text, flags=re.I):
            brand = _clean_brand(m.group(1))
            if len(brand) < 2:
                continue
            key = _norm(brand)
            if key in found:
                continue
            window = text[max(0, m.start() - 40): m.end() + 160]
            code_m = _CODE_RE.search(window)
            url = None
            for um in _URL_RE.finditer(window):
                cand = um.group(1).lower().split("/")[0]
                if cand not in _URL_STOP and "." in cand:
                    url = um.group(1)
                    break
            found[key] = {
                "brand": brand,
                "brand_norm": key,
                "promo_code": code_m.group(1) if code_m else None,
                "promo_url": url,
                "deal_type": "host_read",
                "confidence": "high" if (code_m or url) else "med",
                "evidence": window.strip()[:240],
            }
    return list(found.values())


def detect_with_llm(text: str) -> list[dict] | None:
    """Optional upgrade. Returns None if no API key (caller keeps rule results)."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    try:
        import anthropic
    except ImportError:
        return None

    client = anthropic.Anthropic()
    prompt = (
        "Extract podcast SPONSORS from this episode text. A sponsor is a brand "
        "given a PAID promotional read. Distinguish:\n"
        "- host_read: a clear ad read ('brought to you by', promo code/URL)\n"
        "- mention: brand named but no clear paid promotion\n"
        "- affiliate: only an affiliate/discount link, no read\n"
        "Return ONLY a JSON array, no prose. Each item: "
        '{"brand","promo_code"|null,"promo_url"|null,'
        '"deal_type":"host_read|mention|affiliate",'
        '"confidence":"high|med|low","evidence":"<=200 char quote"}.\n'
        "Do NOT invent brands. If none, return [].\n\nTEXT:\n" + text[:12000]
    )
    try:
        resp = client.messages.create(
            model="claude-sonnet-4-6", max_tokens=1500,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = "".join(b.text for b in resp.content if b.type == "text")
        raw = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```")
        items = json.loads(raw)
        out = []
        for it in items:
            if not it.get("brand"):
                continue
            out.append({
                "brand": it["brand"][:60],
                "brand_norm": _norm(it["brand"]),
                "promo_code": (it.get("promo_code") or None),
                "promo_url": (it.get("promo_url") or None),
                "deal_type": it.get("deal_type", "mention"),
                "confidence": it.get("confidence", "low"),
                "evidence": (it.get("evidence") or "")[:240],
            })
        return out
    except Exception:
        return None


def detect_sponsors(text: str) -> list[dict]:
    """Rule pass always; LLM pass merged in when available (LLM wins on conflict
    because it classifies deal_type/confidence better)."""
    rules = {d["brand_norm"]: d for d in detect_rule_based(text)}
    llm = detect_with_llm(text)
    if llm:
        for d in llm:
            rules[d["brand_norm"]] = d  # LLM classification supersedes
    # Filter out junk "brands" (HTML entities, ad-copy fragments) so they never
    # enter the DB. is_valid_brand is conservative — keeps ambiguous real brands.
    return [d for d in rules.values()
            if is_valid_brand(d.get("brand", ""), d.get("brand_norm"))]
