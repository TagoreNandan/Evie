# Phase 3: Journal & Long-Term RAG Recall System — Research & Architecture

## Executive Summary & Goals

Phase 3 implements Evie's shared memory infrastructure (`memory.py`) and vector RAG recall system (`MEM-01`, `MEM-02`).

The key objectives are:
1. **Daily Journal Ingestion (`MEM-01`)**: Enable structured entry creation (`add_journal_entry()`) storing daily summaries, activity logs, and source metadata in SQLite `journal_entries`.
2. **Vector Indexing & Hybrid RAG Recall (`MEM-02`)**: Index journal entries with vector embeddings (`journal_embeddings`) to enable natural-language similarity search and date-filtered recall (`search_journal_entries()`).
3. **Shared Service Layer (`memory.py`)**: Centralize all memory operations into `memory.py` so future phases (briefings, DRS research agent, tone customization) access long-term memory through a unified interface.

---

## Component Architecture & System Design

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                            FastAPI Application                              │
│                                 (main.py)                                   │
│                                                                             │
│        ┌──────────────────────────┐    ┌───────────────────────────┐        │
│        │   POST /journal/entry    │    │    GET /journal/search    │        │
│        └────────────┬─────────────┘    └─────────────┬─────────────┘        │
│                     │                                │                      │
│                     └────────────────┬───────────────┘                      │
│                                      │                                      │
│                         ┌────────────▼─────────────┐                        │
│                         │        memory.py         │                        │
│                         │  (Shared Memory Engine)  │                        │
│                         └────────────┬─────────────┘                        │
│                                      │                                      │
│                ┌─────────────────────┴─────────────────────┐                │
│                ▼                                           ▼                │
│   ┌──────────────────────────┐                ┌──────────────────────────┐  │
│   │ SQLite: journal_entries  │                │ SQLite: journal_embeddings│ │
│   │ (id, entry_date, content)│                │ (entry_id, embedding,    │ │
│   │                          │                │  entry_date)             │ │
│   └──────────────────────────┘                └──────────────────────────┘  │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## Technical Specifications & API Contracts

### 1. Memory Engine (`memory.py`)

- `init_memory_tables(db_path: str = "evie.db") -> None`:
  Ensures `journal_entries` and `journal_embeddings` SQLite tables exist per `04_Backend_Schema.md`.

- `add_journal_entry(entry_date: str, content: str, source_data: dict | None = None, db_path: str = "evie.db") -> dict`:
  - Validates ISO-8601 `entry_date` (`YYYY-MM-DD`).
  - Inserts entry into `journal_entries`.
  - Computes vector embedding for `content` using local vector encoder (`numpy` vector normalization / TF-IDF / local embedding encoder).
  - Inserts vector embedding record into `journal_embeddings` table (`entry_id`, `embedding_json`, `entry_date`).
  - Returns `{"id": entry_id, "entry_date": entry_date, "content": content, "status": "created"}`.

- `search_journal_entries(query: str, top_k: int = 5, start_date: str | None = None, end_date: str | None = None, db_path: str = "evie.db") -> list[dict]`:
  - Computes query vector embedding.
  - Queries `journal_embeddings` (filtered by `start_date` and `end_date` if provided).
  - Computes cosine similarity between query vector and stored entry vectors.
  - Ranks top `top_k` matches and retrieves corresponding text content and metadata from `journal_entries`.
  - Returns list of matching entry dicts: `[{"id": 1, "entry_date": "2026-09-19", "content": "...", "score": 0.89}]`.

### 2. FastAPI Endpoints (`main.py`)

- `POST /journal/entry`: Accepts JSON body `{"entry_date": "YYYY-MM-DD", "content": str, "source_data": dict | None}`. Calls `memory.add_journal_entry()`.
- `GET /journal/search?q=...&start_date=...&end_date=...&top_k=5`: Accepts query parameter `q`. Calls `memory.search_journal_entries()`. Returns HTTP 200 JSON list of results.

---

## Database Schemas

From `04_Backend_Schema.md`:

```sql
CREATE TABLE IF NOT EXISTS journal_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_date DATE NOT NULL,
    content TEXT NOT NULL,
    source_data TEXT,                  -- JSON snapshot of what fed into this entry
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS journal_embeddings (
    entry_id INTEGER PRIMARY KEY,
    entry_date DATE NOT NULL,
    embedding_json TEXT NOT NULL,      -- JSON serialized vector float array
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(entry_id) REFERENCES journal_entries(id) ON DELETE CASCADE
);
```

---

## Risk Analysis & Mitigation

1. **Risk**: Heavy external vector DB dependency breaking local execution or portability.
   - **Mitigation**: Use lightweight SQLite vector table (`journal_embeddings`) with `numpy` cosine similarity calculation. Zero extra heavy C-extensions or external services required.
2. **Risk**: Secret leakage into long-term memory logs or vector embeddings.
   - **Mitigation**: Pass entry content through `redaction.redact_text()` before storage if unredacted inputs are passed.
3. **Risk**: Slow search latency over large historical entry counts.
   - **Mitigation**: Prune search space using date-range SQL filters (`start_date <= entry_date <= end_date`) prior to computing vector similarity.
