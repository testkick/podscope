"""Guest-matching service — the inverted matching product.

Input: a user's expertise/topic (+ optional audience note).
Output: ranked shows that BOOK GUESTS, sized by reach (PodScope Score) and
scored by topical fit, with booking contact (gated later behind Pro).

Serves both PR agencies (booking clients) and individuals (pitching themselves).

Ranking = fit x reach, filtered to guest-bookers:
  - filter:  guest_profiles.books_guests = true  (never pitch a solo show)
  - fit:     overlap of the user's terms against the show's topics, recent guest
             names, and title. Shows that cover the user's subject rank higher.
  - reach:   PodScope Score (0-100) so strong shows beat obscure ones at equal fit.

The final score is a blend that requires SOME fit (a huge show with zero topical
overlap shouldn't top a niche-but-perfect match), then uses reach as the
tie-breaker/amplifier.
"""

import re

from sqlalchemy import select

from app.db.models import Show, PodScopeScore
from app.db.enrich_models import GuestProfile

_WORD_RE = re.compile(r"[a-z0-9]+")
# generic words that shouldn't count as topical signal
_STOP = {
    "the", "a", "an", "and", "or", "of", "for", "to", "in", "on", "with",
    "podcast", "show", "host", "guest", "about", "my", "our", "your", "i",
    "we", "expert", "expertise", "topic", "audience", "people", "who", "that",
}


def _terms(text: str) -> set[str]:
    return {w for w in _WORD_RE.findall((text or "").lower())
            if w not in _STOP and len(w) > 2}


def _fit_score(query_terms: set[str], show_blob_terms: set[str]) -> float:
    """0..1 overlap of query terms present in the show's topical text."""
    if not query_terms:
        return 0.0
    hits = len(query_terms & show_blob_terms)
    return hits / len(query_terms)


def match_guests(db, query: str, audience: str = "", limit: int = 25,
                 min_fit: float = 0.15) -> list[dict]:
    """Return ranked guest-booking shows for the query. Each row includes fit,
    reach (score), why-it-fits, and whether a booking contact exists (the actual
    email is gated for Pro later)."""
    query_terms = _terms(f"{query} {audience}")
    if not query_terms:
        return []

    # pull guest-booking shows joined to their score
    rows = db.execute(
        select(Show, GuestProfile, PodScopeScore)
        .join(GuestProfile, GuestProfile.show_id == Show.id)
        .outerjoin(PodScopeScore, PodScopeScore.show_id == Show.id)
        .where(GuestProfile.books_guests.is_(True))
    ).all()

    results = []
    for show, gp, score in rows:
        blob = " ".join(filter(None, [
            gp.topics, gp.recent_guests, show.name, show.publisher,
            gp.suitability_note,
        ]))
        show_terms = _terms(blob)
        fit = _fit_score(query_terms, show_terms)
        if fit < min_fit:
            continue
        reach = (score.score if score else 0.0) / 100.0   # 0..1
        # blend: fit dominates (you must be relevant), reach amplifies.
        # 0.65*fit + 0.35*reach, but scale reach's contribution by fit so a
        # zero-fit giant can't sneak in on reach alone.
        match_score = round(100 * (0.65 * fit + 0.35 * reach * (0.5 + 0.5 * fit)), 1)

        matched = sorted(query_terms & show_terms)
        results.append({
            "slug": show.slug,
            "name": show.name,
            "publisher": show.publisher,
            "artwork_url": show.artwork_url,
            "match_score": match_score,
            "fit": round(fit, 2),
            "podscope_score": score.score if score else None,
            "guest_frequency": gp.guest_frequency,
            "topics": gp.topics,
            "matched_terms": matched,
            "why": _why(matched, gp, score),
            "has_contact": bool(gp.contact_email),
        })

    results.sort(key=lambda r: r["match_score"], reverse=True)
    return results[:limit]


def _why(matched: list[str], gp: GuestProfile, score) -> str:
    """One-line fit explanation for the result card."""
    bits = []
    if matched:
        bits.append("covers " + ", ".join(matched[:4]))
    if gp.guest_frequency in ("every", "often"):
        bits.append(f"books guests {gp.guest_frequency}")
    if score and score.score:
        bits.append(f"PodScope {int(round(score.score))}")
    return " · ".join(bits) if bits else "guest-booking show"
