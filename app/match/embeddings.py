"""Embeddings client for semantic guest/product matching.

Turns text (a show's topic+guest blob, or a user's query) into a vector, so
"venture capital" matches a show tagged "startup investing, founders" by MEANING
rather than exact words — fixing the zero-results-for-obvious-queries problem.

Provider: uses an OpenAI-compatible embeddings endpoint (most embedding APIs,
including Voyage/OpenAI/others, expose this shape). Config via env:
    EMBEDDINGS_API_URL   (default: OpenAI's endpoint)
    EMBEDDINGS_API_KEY
    EMBEDDINGS_MODEL     (default: text-embedding-3-small, 1536 dims)
    EMBEDDINGS_DIM       (default: 1536 — MUST match the model & the DB column)

Design: keep the vector DIMENSION in one place (EMBED_DIM) because the pgvector
column is fixed-width — changing models later means a re-embed + column change.

If no API key is set, embed() returns None and callers fall back to keyword
matching, so the app never hard-fails on a missing key.
"""

import os
import httpx

EMBED_DIM = int(os.environ.get("EMBEDDINGS_DIM", "1536"))
_MODEL = os.environ.get("EMBEDDINGS_MODEL", "text-embedding-3-small")
_URL = os.environ.get("EMBEDDINGS_API_URL",
                      "https://api.openai.com/v1/embeddings")
_KEY = os.environ.get("EMBEDDINGS_API_KEY")


def embeddings_enabled() -> bool:
    return bool(_KEY)


def embed(text: str) -> list[float] | None:
    """Embed one string. Returns a vector or None (no key / error → caller
    falls back to keyword matching)."""
    if not _KEY or not text or not text.strip():
        return None
    try:
        r = httpx.post(
            _URL,
            headers={"Authorization": f"Bearer {_KEY}",
                     "Content-Type": "application/json"},
            json={"model": _MODEL, "input": text[:8000]},
            timeout=30,
        )
        r.raise_for_status()
        vec = r.json()["data"][0]["embedding"]
        if len(vec) != EMBED_DIM:
            # dimension mismatch = misconfiguration; fail safe to keyword
            return None
        return vec
    except Exception:
        return None


def embed_batch(texts: list[str]) -> list[list[float] | None]:
    """Embed several strings in one call where the API supports it; falls back
    to per-item on error. Order preserved; failures become None."""
    if not _KEY:
        return [None] * len(texts)
    clean = [(t or "")[:8000] for t in texts]
    try:
        r = httpx.post(
            _URL,
            headers={"Authorization": f"Bearer {_KEY}",
                     "Content-Type": "application/json"},
            json={"model": _MODEL, "input": clean},
            timeout=60,
        )
        r.raise_for_status()
        data = sorted(r.json()["data"], key=lambda d: d["index"])
        out = []
        for d in data:
            v = d["embedding"]
            out.append(v if len(v) == EMBED_DIM else None)
        return out
    except Exception:
        return [embed(t) for t in clean]
