# Phase 2: Briefing Engine & Calendar Confirmation Flow — Validation Strategy

## Overview & Automated Test Commands

Phase 2 validation focuses on verifying the unified briefing aggregation logic across all delivery triggers (`BRIEF-01` to `BRIEF-04`) and enforcing the strict two-step user confirmation protocol for calendar mutations (`CAL-01` to `CAL-02`).

### Quick Test Execution

```bash
pytest tests/test_briefing.py tests/test_calendar_actions.py
```

### Full Phase 1 & 2 Test Execution

```bash
pytest tests/
```

---

## Test Suite & Requirement Mapping Matrix

| Test Module | Target Function / Endpoint | Tested Behavior / Scenario | Requirement |
|---|---|---|---|
| `tests/test_briefing.py` | `generate_briefing()` | Aggregates GitHub events, calendar events, and security findings into a single report. | `BRIEF-01` |
| `tests/test_briefing.py` | `generate_briefing()` | Partial API failure: Calendar API raises exception; returns GitHub + Security findings with a safe structured status/error code for Calendar (never a raw exception string). | `BRIEF-01` |
| `tests/test_briefing.py` | `generate_briefing()` | Deduplication: Events surfaced in a report are marked `included_in_briefing = 1` and not re-announced as new alerts. | `BRIEF-01` |
| `tests/test_briefing.py` | `POST /webhooks/github` | Accepts GitHub push/PR webhook, validates HMAC (if configured), stores in `github_events`, calls briefing aggregator. | `BRIEF-02` |
| `tests/test_briefing.py` | `GET /briefing/wake` | Wake-triggered morning briefing route returns consolidated report from `generate_briefing(trigger="wake")`. | `BRIEF-03` |
| `tests/test_briefing.py` | `GET /briefing/on-demand` | On-demand briefing route returns fresh consolidated report from `generate_briefing(trigger="on_demand")`. | `BRIEF-04` |
| `tests/test_calendar_actions.py` | `propose_calendar_action()` / `POST /calendar/propose` | Proposal creates `calendar_actions` DB entry with `confirmed_by_user = 0`, returns `proposal_token` & prompt, performs **ZERO** API calls. | `CAL-01` |
| `tests/test_calendar_actions.py` | `confirm_calendar_action()` / `POST /calendar/confirm` | Declining proposal (`confirmed=False`) sets status to cancelled and performs **ZERO** API calls. | `CAL-02` |
| `tests/test_calendar_actions.py` | `confirm_calendar_action()` / `POST /calendar/confirm` | Confirming proposal (`confirmed=True`): verifies 15-min TTL, atomically claims proposal (`proposed` -> `claimed`), transitions `executing` -> `executed` upon API success, or `execution_failed` upon API failure. Verifies only a successful atomic claim permits Calendar API calls, and a failed claim performs **ZERO** API calls. | `CAL-02` |
| `tests/test_calendar_actions.py` | `confirm_calendar_action()` | Invalid or expired token returns 400/404 error without executing Calendar API mutation. | `CAL-02` |

---

## Hard Security & Operational Fences

1. **Zero Unconfirmed Mutations**: Any test attempting to execute a Calendar API call directly without a prior proposal and `confirmed=True` confirmation must fail.
2. **Keyring Credential Usage**: All tests must mock `keyring_utils.get_credential` and HTTP external services (`httpx`). Real tokens or live API endpoints must never be invoked in unit tests.
3. **No Fake Data Fallback**: External API exceptions must be surfaced as explicit error status fields, never masked with generic silent mock data.

---

## Verification Status

- Wave 0 dependencies: Identified (Pending Plan Execution)
- Wave 1 unit tests (`test_briefing.py`, `test_calendar_actions.py`): Pending
- Overall Phase 2 status: Ready for Execution
