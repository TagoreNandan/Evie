# Phase 5: DRS Research Agent & Personality Configuration — Context & Decisions

## Phase Scope

Phase 5 implements the evidence-based Decision Research Support engine and conversational tone configuration for Evie:
- **`DRS-01`**: Evidence-based Decision/Research Support engine (`drs.py`, SQLite `drs_queries` table, `POST /drs/ask` endpoint).
- **`TONE-01`**: Assistant personality & tone configuration system (`tone.py`, SQLite `user_config` table, `POST /config/tone` endpoint).

## Key Design Decisions & Fences

1. **Non-Definitive Evidence Framing (`DRS-01`)**:
   - DRS answers MUST always be framed as evidence-based support, never as a definitive final verdict.
   - Response template MUST include explicit sources (URLs) and avoid absolute language ("you should definitely...").
   - If search results are empty or low confidence, return an honest "insufficient evidence" response rather than a fabricated answer.
   - Query logs saved to `drs_queries` table (`question`, `answer_summary`, `sources` JSON).
2. **Dynamic Tone Formatting (`TONE-01`)**:
   - User preference stored in `user_config` table (`tone_preference` e.g. `'neutral'`, `'casual'`, `'formal'`, `'humorous'`).
   - `format_response_tone(text: str, tone: str)` dynamically adjusts phrasing to match active tone preference.
3. **Credential & Cost Awareness**:
   - Web search API keys / LLM keys retrieved from OS Keyring or environment variables.
   - Zero secret leakage in queries, responses, or error logs.

## Deliverables

- `drs.py` — DRS research query planner, search aggregator, & response formatter
- `tone.py` — Personality & tone preference manager
- `main.py` — Endpoints (`POST /drs/ask`, `POST /config/tone`, `GET /config/tone`)
- `tests/test_drs.py`, `tests/test_tone.py` — Unit test suites
