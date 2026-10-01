# Phase 5: DRS Research Agent & Personality Configuration — Research & Architecture

## Executive Summary & Goals

Phase 5 implements Evie's Decision Research Support (DRS) agent (`DRS-01`) and conversational personality/tone configuration (`TONE-01`).

The key objectives are:
1. **Evidence-Based DRS Agent (`DRS-01`)**: Process complex research questions, query web search engines / knowledge bases, synthesize findings with cited URLs, and format answers using non-definitive, evidence-support framing.
2. **Personality & Tone Manager (`TONE-01`)**: Store user tone preferences (`neutral`, `casual`, `formal`, `humorous`) in SQLite `user_config` and apply dynamic response formatting.
3. **Database Logging**: Persist DRS queries in `drs_queries` and user preferences in `user_config`.

---

## Component Architecture & System Design

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                            FastAPI Application                              │
│                                 (main.py)                                   │
│                                                                             │
│        ┌──────────────────────────┐        ┌────────────────────────┐       │
│        │      POST /drs/ask       │        │   POST /config/tone    │       │
│        └────────────┬─────────────┘        └───────────┬────────────┘       │
│                     │                                  │                    │
│                     ▼                                  ▼                    │
│                  drs.py                             tone.py                 │
│         (Research Agent Engine)             (Tone & Personality)            │
│                     │                                  │                    │
│        ┌────────────┴───────────┐                      │                    │
│        ▼                        ▼                      │                    │
│  Web Search Engine      SQLite: drs_queries      SQLite: user_config        │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## Technical Specifications & Contracts

### 1. Decision Research Support (DRS) Engine (`drs.py`)

`ask_drs_question(question: str, db_path: str = "evie.db", search_fn = None) -> dict`

**Framing Rules (HARD RULE)**:
- Output MUST include explicit disclaimer: `"Note: This analysis represents evidence-based support from available sources, not a definitive verdict."`
- Absolute language (`"you should definitely"`, `"undoubtedly"`, `"100% proven"`) is strictly prohibited.
- If search returns 0 results or fails, returns `{"status": "insufficient_evidence", "summary": "Insufficient search evidence found to answer this question.", "sources": []}`.

**Output Structure**:
```json
{
  "question": "Should I use PostgreSQL or SQLite for personal side projects?",
  "summary": "Evidence suggests SQLite is optimal for single-user local applications due to zero configuration, whereas PostgreSQL is preferred for high-concurrency remote web servers.",
  "evidence_points": [
    "SQLite provides single-file local database storage with zero daemon overhead.",
    "PostgreSQL supports concurrent client connections and robust enterprise features."
  ],
  "sources": [
    "https://www.sqlite.org/whentouse.html",
    "https://www.postgresql.org/docs/about/"
  ],
  "disclaimer": "Note: This analysis represents evidence-based support from available sources, not a definitive verdict.",
  "status": "success"
}
```

### 2. Personality & Tone Manager (`tone.py`)

- `init_user_config_table(db_path: str = "evie.db") -> None`:
  Ensures `user_config` table exists with single-row constraint (`id = 1`).

- `get_tone_preference(db_path: str = "evie.db") -> str`:
  Returns active tone preference (`'neutral'`, `'casual'`, `'formal'`, `'humorous'`). Default is `'neutral'`.

- `set_tone_preference(tone: str, db_path: str = "evie.db") -> str`:
  Validates `tone` in `['neutral', 'casual', 'formal', 'humorous']`. Updates `user_config` table. Returns new tone.

- `apply_tone(text: str, tone: str | None = None, db_path: str = "evie.db") -> str`:
  Formats response string `text` matching the active tone preference.

---

## Database Schemas

From `04_Backend_Schema.md`:

```sql
CREATE TABLE IF NOT EXISTS drs_queries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    question TEXT NOT NULL,
    answer_summary TEXT,
    sources TEXT,                      -- JSON array of URLs
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS user_config (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    github_username TEXT,
    google_email TEXT,
    notes_platform TEXT,
    tone_preference TEXT DEFAULT 'neutral',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

---

## Risk Analysis & Mitigation

1. **Risk**: DRS research engine producing definitive or misleading assertions without citations.
   - **Mitigation**: Enforce mandatory non-definitive disclaimer string and require `len(sources) > 0` before returning `status = "success"`.
2. **Risk**: Empty search results causing hallucinated answers.
   - **Mitigation**: Explicit check returning `insufficient_evidence` status when zero search results are retrieved.
