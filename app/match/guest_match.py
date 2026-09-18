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
import json
import math

from sqlalchemy import select, text as sqltext

from app.db.models import Show, PodScopeScore
from app.db.enrich_models import GuestProfile, ShowEmbedding
from app.match.embeddings import embed, embeddings_enabled, EMBED_DIM

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


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def _semantic_match(db, query: str, audience: str, limit: int,
                    min_sim: float) -> list[dict] | None:
    """Meaning-based match: embed the query, rank guest-booking shows by cosine
    similarity to their stored embedding. Returns None if embeddings aren't
    available (caller falls back to keyword matching), so 'venture capital'
    matches 'startup investing' shows by meaning, not exact words."""
    if not embeddings_enabled():
        return None
    qvec = embed(f"{query}. Audience: {audience}" if audience else query)
    if qvec is None:
        return None

    pg = db.bind.dialect.name == "postgresql"
    results = []

    if pg:
        # fast path: pgvector cosine distance, DB-side ordering
        try:
            qlit = "[" + ",".join(str(x) for x in qvec) + "]"
            rows = db.execute(sqltext("""
                SELECT s.id, s.slug, s.name, s.publisher, s.artwork_url,
                       gp.guest_frequency, gp.topics, gp.recent_guests,
                       gp.contact_email, ps.score AS podscore,
                       1 - (e.embedding <=> :qv) AS sim
                FROM show_embeddings e
                JOIN shows s ON s.id = e.show_id
                JOIN guest_profiles gp ON gp.show_id = s.id
                LEFT JOIN podscope_scores ps ON ps.show_id = s.id
                WHERE gp.books_guests = true AND e.embedding IS NOT NULL
                ORDER BY e.embedding <=> :qv
                LIMIT :lim
            """), {"qv": qlit, "lim": limit * 3}).all()
            for r in rows:
                sim = float(r.sim)
                if sim < min_sim:
                    continue
                results.append(_row_to_result(
                    r.slug, r.name, r.publisher, r.artwork_url, r.guest_frequency,
                    r.topics, r.recent_guests, r.contact_email, r.podscore, sim))
        except Exception:
            return None
    else:
        # portable path: cosine over JSON-stored vectors (dev / no pgvector)
        rows = db.execute(
            select(Show, GuestProfile, PodScopeScore, ShowEmbedding)
            .join(GuestProfile, GuestProfile.show_id == Show.id)
            .join(ShowEmbedding, ShowEmbedding.show_id == Show.id)
            .outerjoin(PodScopeScore, PodScopeScore.show_id == Show.id)
            .where(GuestProfile.books_guests.is_(True))
        ).all()
        for show, gp, ps, emb in rows:
            if not emb.vector_json:
                continue
            try:
                vec = json.loads(emb.vector_json)
            except Exception:
                continue
            sim = _cosine(qvec, vec)
            if sim < min_sim:
                continue
            results.append(_row_to_result(
                show.slug, show.name, show.publisher, show.artwork_url,
                gp.guest_frequency, gp.topics, gp.recent_guests,
                gp.contact_email, ps.score if ps else None, sim))

    # blend semantic similarity with reach, same philosophy as keyword path
    for r in results:
        reach = (r["podscope_score"] or 0) / 100.0
        r["match_score"] = round(100 * (0.7 * r["_sim"] + 0.3 * reach * (0.5 + 0.5 * r["_sim"])), 1)
    results.sort(key=lambda r: r["match_score"], reverse=True)
    return results[:limit]


def _row_to_result(slug, name, publisher, artwork, freq, topics, guests,
                   contact, podscore, sim):
    why_bits = []
    if topics:
        why_bits.append("covers " + ", ".join(topics.split(",")[:3]).strip())
    if freq in ("every", "often"):
        why_bits.append(f"books guests {freq}")
    if podscore:
        why_bits.append(f"PodScope {int(round(podscore))}")
    return {
        "slug": slug, "name": name, "publisher": publisher,
        "artwork_url": artwork, "podscope_score": podscore,
        "guest_frequency": freq, "topics": topics,
        "why": " · ".join(why_bits) if why_bits else "guest-booking show",
        "has_contact": bool(contact),
        "_sim": sim,
        "matched_terms": [],
    }


def match_guests(db, query: str, audience: str = "", limit: int = 25,
                 min_fit: float = 0.15) -> list[dict]:
    """Rank guest-booking shows for the query. Uses SEMANTIC matching when
    embeddings are available (so 'venture capital' matches 'startup investing'),
    and falls back to keyword overlap otherwise."""
    semantic = _semantic_match(db, query, audience, limit, min_sim=0.25)
    if semantic is not None:
        return semantic
    return _keyword_match(db, query, audience, limit, min_fit)


def _keyword_match(db, query: str, audience: str, limit: int,
                   min_fit: float) -> list[dict]:
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
