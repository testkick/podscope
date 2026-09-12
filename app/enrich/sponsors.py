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
    b = raw.strip()
    # cut at the first sentence/punctuation boundary
    b = re.split(r"[.,!?;:/\n]", b, 1)[0].strip()
    # cut at the first action verb / filler word ("Squarespace Go" -> "Squarespace")
    m = _BRAND_CUT.search(b)
    if m and m.start() > 0:
        b = b[: m.start()].strip()
    b = b.rstrip(".,!—-–").strip()
    return b[:60]


def detect_rule_based(text: str) -> list[dict]:
    found: dict[str, dict] = {}
    for pat in _READ_PATTERNS:
        for m in re.finditer(pat, text, flags=re.I):
            brand = _clean_brand(m.group(1))
            if len(brand) < 2:
                continue
            key = _norm(brand)
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
    # Drop pure affiliate/mention-low noise from the headline count later; keep
    # everything here so the UI can filter by confidence.
    return list(rules.values())
