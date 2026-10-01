# Phase 9 Research: General-Purpose DRS Research Engine

## Domain Analysis
- **Core Engine File**: `drs.py` currently implements basic `ask_drs_question()`, `_sanitize_framing()`, and `drs_queries` SQLite database table.
- **Source Adapters**: Needs a formal `SourceAdapter` abstraction class so web search, news search, and local RAG journal recall can be queried uniformly without modifying query logic.
- **Deep Research Algorithm**:
  1. Input: Complex user topic (e.g., "Analyze the pros and cons of microservices vs monoliths for small teams").
  2. Plan: Deconstruct topic into 2-3 focused sub-queries.
  3. Execute: Run sub-queries across registered source adapters.
  4. Deduplicate: Deduplicate snippets by content hashing / text overlap and URLs by domain/path.
  5. Synthesize: Aggregate evidence into structured report sections.
  6. Sanitize: Apply `_sanitize_framing()` to eliminate absolute claims.
  7. Persist & Return: Write query record to `drs_queries` table in SQLite with `query_type='deep_research'`.

## User & API Contracts
- `POST /drs/deep_research`: Accepts `{"topic": str, "max_depth": int}` and returns `{ "topic": ..., "report": ..., "sections": {...}, "sources": [...], "disclaimer": ... }`.
- `POST /chat`: Recognizes intent like "deep research on X" or "do a deep dive on Y" and triggers `execute_deep_research()`.

## Verification Strategy
- Add unit tests in `tests/test_drs_deep_research.py` testing adapter registration, snippet deduplication, sub-query generation, non-definitive framing enforcement, and FastAPI endpoint execution.
