# Local Search

Iris search should be local by default.

## Current Implementation

`apps/iris-api/src/lib/transcript-search.ts` searches the local
`transcript_segments` table directly. It does not call a managed vectorstore and
does not create remote embeddings.

This is intentionally simple. SQLite is now the default local database, and the
schema includes FTS5 tables for local full-text search. The query path still uses
plain local table search until ranking is tightened.

## Target Implementation

Move transcript and memory search to SQLite FTS5:

- `transcript_segments_fts` for final transcript text.
- `user_memories_fts` for user memory content.
- BM25 ranking for normal text search.
- Optional trigram tokenizer for fuzzy substring search.
- Triggers or explicit write helpers to keep FTS rows in sync.

SQLite FTS5 provides full-text virtual tables and BM25 ranking in-process, so it
matches the local-first goal without a vector database.

## Hybrid Search

For Iris, "hybrid" should mean local lexical ranking plus structured filters,
not a hosted vector database.

Useful filters:

- session
- speaker
- device
- source
- time range
- final/interim status

Useful ranking inputs:

- BM25 text score
- recency
- exact phrase match
- speaker match
- conversation/session context

Embeddings can be added later as a local optional feature if a small local model
is good enough. Do not make remote embeddings a requirement for search.

## Migration Tasks

1. Replace direct `like` search with FTS `match` queries.
2. Keep structured filters in ordinary SQLite columns.
3. Add BM25 ranking and recency scoring.
4. Add optional local embeddings only if a small on-device model is useful.
