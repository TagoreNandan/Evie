# Phase 5: DRS Research Agent & Personality Configuration — Validation Strategy

## Overview & Automated Test Commands

Phase 5 validation ensures evidence-based, non-definitive research query responses (`DRS-01`) and dynamic conversational tone customization (`TONE-01`).

### Quick Test Execution

```bash
pytest tests/test_drs.py tests/test_tone.py
```

### Full System Test Execution

```bash
pytest tests/
```

---

## Test Suite & Requirement Mapping Matrix

| Test Module | Target Function / Endpoint | Tested Behavior / Scenario | Requirement |
|---|---|---|---|
| `tests/test_drs.py` | `ask_drs_question()` | Generates evidence-based research answer with cited sources and non-definitive disclaimer. | `DRS-01` |
| `tests/test_drs.py` | `ask_drs_question()` | Zero search results handling: returns `status = "insufficient_evidence"` without hallucinating answers. | `DRS-01` |
| `tests/test_tone.py` | `set_tone_preference()` / `get_tone_preference()` | Updates and retrieves tone preference in `user_config` table (`neutral`, `casual`, `formal`, `humorous`). | `TONE-01` |
| `tests/test_tone.py` | `apply_tone()` | Formats response text matching active tone preference. | `TONE-01` |
| `tests/test_drs.py` | `POST /drs/ask` | FastAPI endpoint returns structured research result with source attribution and disclaimer. | `DRS-01` |
| `tests/test_tone.py` | `POST /config/tone` & `GET /config/tone` | FastAPI endpoints update and return active tone setting. | `TONE-01` |

---

## Hard Security & Operational Fences

1. **Non-Definitive Framing**: All successful DRS outputs must contain the mandatory disclaimer and avoid absolute language.
2. **Source Attribution**: All successful DRS outputs must contain at least one valid source URL.
3. **No Secret Leaks**: Search queries and response outputs must not expose sensitive keys or credentials.

---

## Verification Status

- Wave 0 dependencies: Identified (Pending Plan Execution)
- Wave 1 unit tests (`test_drs.py`, `test_tone.py`): Pending
- Overall Phase 5 status: Ready for Execution
