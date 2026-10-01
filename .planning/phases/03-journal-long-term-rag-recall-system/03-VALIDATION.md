# Phase 3: Journal & Long-Term RAG Recall System — Validation Strategy

## Overview & Automated Test Commands

Phase 3 validation ensures reliable daily journal entry storage (`MEM-01`) and accurate natural-language vector recall with date filtering (`MEM-02`).

### Quick Test Execution

```bash
pytest tests/test_memory.py
```

### Full System Test Execution

```bash
pytest tests/
```

---

## Test Suite & Requirement Mapping Matrix

| Test Module | Target Function / Endpoint | Tested Behavior / Scenario | Requirement |
|---|---|---|---|
| `tests/test_memory.py` | `add_journal_entry()` | Creates journal entry in `journal_entries` and vector embedding in `journal_embeddings`. | `MEM-01` |
| `tests/test_memory.py` | `add_journal_entry()` | Input validation: rejects invalid `entry_date` formats or empty content. | `MEM-01` |
| `tests/test_memory.py` | `search_journal_entries()` | Ranks relevant entries matching a natural-language query using vector cosine similarity. | `MEM-02` |
| `tests/test_memory.py` | `search_journal_entries()` | Date-range filtering: restricts search results strictly within `start_date` and `end_date`. | `MEM-02` |
| `tests/test_memory.py` | `POST /journal/entry` | FastAPI endpoint stores journal entry and returns 200 OK JSON response. | `MEM-01` |
| `tests/test_memory.py` | `GET /journal/search` | FastAPI endpoint returns top-k semantic matches for query `?q=...`. | `MEM-02` |

---

## Hard Security & Operational Fences

1. **Zero Secret Leakage**: Verification that sensitive tokens or secrets are redacted before being saved to memory tables or embeddings.
2. **Deterministic Recall**: Verification that date-range queries never return entries outside the requested range.

---

## Verification Status

- Wave 0 dependencies: Identified (Pending Plan Execution)
- Wave 1 unit tests (`test_memory.py`): Pending
- Overall Phase 3 status: Ready for Execution
