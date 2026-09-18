# Semantic guest matching (embeddings) — setup

Fixes the "venture capital → 0 results" problem: matches queries to shows by
MEANING, not exact words. "Venture capital" now finds shows tagged "startup
investing, founders" because they're semantically near.

## Two possible reasons a query returns 0 — check BOTH
Embeddings fix vocabulary mismatch. They do NOT fix an empty dataset. Before
assuming the matcher is broken, confirm coverage:

```sql
-- How many guest-booking shows exist to match against?
SELECT COUNT(*) AS profiles,
       COUNT(*) FILTER (WHERE books_guests) AS books_guests_true
FROM guest_profiles;

-- How many have embeddings computed?
SELECT COUNT(*) AS embedded FROM show_embeddings WHERE vector_json IS NOT NULL;
```
If `books_guests_true` is small, the enricher cron just hasn't covered the
category yet — let it run; embeddings can't match shows that aren't enriched.

## One-time setup
1. **Migrate** (enables pgvector + adds the vector column, idempotent):
   `python -m app.db.migrate`
   On Railway Postgres this runs `CREATE EXTENSION vector`. If the instance
   lacks pgvector, it silently falls back to the portable JSON vector store
   (slower but correct).
2. **Env vars** on whatever service embeds/serves:
   - `EMBEDDINGS_API_KEY` (required — no key ⇒ falls back to keyword matching)
   - `EMBEDDINGS_API_URL` (default OpenAI-compatible `/v1/embeddings`)
   - `EMBEDDINGS_MODEL` (default `text-embedding-3-small`)
   - `EMBEDDINGS_DIM` (default `1536` — MUST match the model and the DB column)

## Backfill embeddings
```
python -m app.match.embed_backfill            # guest-booking shows, missing/stale
python -m app.match.embed_backfill --all      # every show with topics
python -m app.match.embed_backfill --force    # re-embed everything
```
Idempotent: skips shows whose profile text hasn't changed. Run as a cron
(after the enricher) so new/updated shows get embedded automatically. Batched
and capped (`EMBED_BATCH`, `EMBED_MAX`).

## How matching now works
`match_guests` uses semantic search when embeddings exist (query embedded at
search time, ranked by cosine similarity blended with PodScope reach), and
falls back to keyword overlap when they don't. Same free/paid line: results are
open, booking contact gated for Pro.

## Note on model/dimension
The pgvector column is fixed-width at `EMBEDDINGS_DIM`. Changing embedding models
later means: update EMBEDDINGS_DIM, drop/re-add the `embedding` column (migrate),
and `embed_backfill --force`. Pick a model you're happy to keep.
