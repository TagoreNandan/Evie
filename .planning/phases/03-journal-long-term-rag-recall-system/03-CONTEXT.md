# Phase 3: Journal & Long-Term RAG Recall System — Context & Decisions

## Phase Scope

Phase 3 builds the core memory infrastructure and RAG recall system for Evie:
- **`MEM-01`**: Daily journal entry logging & structured database storage (`memory.py` & SQLite `journal_entries` table).
- **`MEM-02`**: Vector embedding indexing and natural-language RAG recall engine over historical dates (`memory.py` vector search & `GET /journal/search` endpoint).

## Key Design Decisions & Fences

1. **Shared Infrastructure (`memory.py`)**: All journal logging and natural-language memory recall operations MUST pass through a single shared service module (`memory.py`). Features in Phase 4 and Phase 5 will consume memory through this exact service without reimplementing memory storage or search logic.
2. **Hybrid Storage Architecture**:
   - Structured entry metadata (date, content, source_data JSON) stored in SQLite `journal_entries` table.
   - Embeddings and vector similarity search stored and computed in SQLite vector index table (`journal_embeddings`) using numpy vector operations (or Chroma if available).
3. **Deterministic Date Filtering & Semantic Matching**: RAG search supports both natural-language semantic query matching and explicit/approximate date-range filters (`entry_date`).
4. **Data Minimization & Credential Safety**: Journal entries and vector metadata must never log or store raw credentials, tokens, or secret values.

## Deliverables

- `memory.py` — Shared memory & vector search service module
- `main.py` — FastAPI routes (`POST /journal/entry`, `GET /journal/search`)
- `tests/test_memory.py` — Unit & integration test suite verifying journal ingestion, vector indexing, date-range filtering, and RAG recall
