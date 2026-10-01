# Phase 2: Briefing Engine & Calendar Confirmation Flow — Research & Architecture

## Executive Summary & Goals

Phase 2 builds the core daily briefing aggregator and two-step confirmation-gated calendar scheduling system for Evie. It addresses requirements `BRIEF-01` through `BRIEF-04` and `CAL-01` through `CAL-02`.

The key objectives are:
1. **Single Unified Briefing Engine (`services/briefing.py`)**: Aggregate overnight GitHub activity (commits, PRs), today's calendar events, active security findings (from Phase 1 watchers), and general highlights into a single consolidated report.
2. **Three Delivery Paths (`main.py`)**:
   - Real-time path: `POST /webhooks/github` (receives push/PR webhooks with mandatory HMAC SHA-256 signature verification, stores minimal event metadata in SQLite `github_events`, triggers instant notification).
   - Wake-triggered path: `GET /briefing/wake` (returns consolidated morning briefing upon user greeting).
   - On-demand path: `GET /briefing/on-demand` (returns fresh consolidated briefing at any time).
3. **Confirmation-Gated Calendar Mutations (`integrations/calendar_actions.py`)**:
   - Proposal phase (`POST /calendar/propose`): Accepts validated structured calendar action fields (`action_type`, `summary`, `start_time`, `end_time`, `description`, `event_id`), generates a unique `proposal_token`, logs to SQLite `calendar_actions` table (`confirmed_by_user = 0`, `status = 'proposed'`), and returns a confirmation prompt. **Zero external HTTP / Calendar API calls occur at proposal time.** No natural-language parsing layer is introduced in this endpoint.
   - Confirmation phase (`POST /calendar/confirm`): Validates proposal token, checks proposal TTL (15-minute expiration), and requires `confirmed = True`. Only upon a successful atomic SQLite claim (`status = 'claimed'`, `rowcount == 1`) does it execute the mutation via Google Calendar REST API v3.
4. **Resilience & Security Fences**:
   - Zero-secret leaks (all OAuth credentials/tokens loaded via `keyring_utils`).
   - Safe error handling: External API failures return stable error codes (`status = "unavailable"`, `error_code = "CALENDAR_FETCH_FAILED"`). Raw exception strings, OAuth tokens, and secrets are never exposed in briefing output, API responses, database records, or logs.
   - Partial briefing fallback: If Calendar API fails during briefing generation, return GitHub and Security sections cleanly with a safe error note for Calendar, rather than failing the briefing.
   - Event deduplication: `github_events` tracks `notified_realtime` and `included_in_briefing` flags to avoid re-announcing events.

---

## Component Architecture & System Design

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                            FastAPI Application                              │
│                        (main.py, localhost:8000)                            │
│                                                                             │
│  ┌───────────────────────┐ ┌───────────────────┐ ┌──────────────────────┐  │
│  │ POST /webhooks/github │ │ GET /briefing/wake│ │ GET /briefing/on-demand│  │
│  └───────────┬───────────┘ └─────────┬─────────┘ └──────────┬───────────┘  │
│              │                       │                      │              │
│              └───────────────────────┼──────────────────────┘              │
│                                      │                                     │
│                         ┌────────────▼─────────────┐                       │
│                         │    services/briefing.py  │                       │
│                         │  (generate_briefing())   │                       │
│                         └────────────┬─────────────┘                       │
│                                      │                                     │
│      ┌───────────────────────────────┼──────────────────────────────┐      │
│      │                               │                              │      │
│ ┌────▼────────────────┐   ┌──────────▼──────────┐       ┌───────────▼────┐ │
│ │ github_events (DB)  │   │ Google Calendar API │       │  Phase 1 DB    │ │
│ │ (metadata only)     │   │  (today's events)   │       │  (findings)    │ │
│ └─────────────────────┘   └─────────────────────┘       └────────────────┘ │
└─────────────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────────────┐
│                   Confirmation-Gated Calendar Lifecycle                     │
│                                                                             │
│  Structured Request ──> POST /calendar/propose                              │
│                                │                                            │
│                                ▼                                            │
│                 Store proposal in calendar_actions                          │
│                 (confirmed_by_user = 0, status = 'proposed')               │
│                 Return proposal_token & confirmation prompt                 │
│                 [ZERO Google Calendar API calls]                            │
│                                │                                            │
│                                ▼                                            │
│                 User Review & Confirmation ("confirmed: true")             │
│                                │                                            │
│                                ▼                                            │
│                         POST /calendar/confirm                              │
│                                │                                            │
│                                ▼                                            │
│                 Check TTL (15 min) -> Atomic SQLite Claim                   │
│                 (UPDATE ... status = 'claimed' WHERE status = 'proposed')   │
│                 If rowcount == 1: status = 'executing'                     │
│                                │                                            │
│                                ▼                                            │
│                 Execute Google Calendar API REST v3 Call                     │
│                                │                                            │
│                ┌───────────────┴───────────────┐                            │
│                ▼                               ▼                            │
│       [Success: 'executed']         [Failure: 'execution_failed']           │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## Technical Specifications & API Contracts

### 1. Unified Briefing Aggregator (`services/briefing.py`)

`generate_briefing(db_path: str = "evie.db", trigger: str = "wake", repo_name: str | None = None) -> dict`

**Aggregation Data Sources**:
- **GitHub Events**: Queries `github_events` table deterministically for un-briefed events (`included_in_briefing = 0`). Updates `included_in_briefing = 1` for included events.
- **Calendar Events**: Invokes helper `fetch_today_calendar_events()` using credentials from `keyring_utils.get_credential("evie_assistant", "google_calendar_oauth")`. If calendar token is missing or API call fails, records safe structured error `calendar_today: {"status": "unavailable", "error_code": "CALENDAR_FETCH_FAILED"}` and appends to `partial_failures`.
- **Security Findings**: Queries Phase 1 SQLite tables (`secret_findings`, `breach_checks`, `exposure_findings`, `twofa_audit`) for counts and redacted previews (`AKIA****...**34`). Zero raw secrets present.

**Output Structure**:
```json
{
  "timestamp": "2026-09-19T16:30:00Z",
  "trigger": "wake",
  "summary": "Good morning! Here is your daily summary.",
  "sections": {
    "github_activity": {
      "count": 3,
      "events": [{"repo_name": "Evie", "summary": "8 commits pushed", "actor": "somespecies"}]
    },
    "calendar_today": {
      "status": "ok",
      "event_count": 2,
      "events": [{"summary": "Team Sync", "start": "2026-09-19T10:00:00Z", "end": "2026-09-19T10:30:00Z"}]
    },
    "security_findings": {
      "open_secrets": 0,
      "unacknowledged_breaches": 1,
      "public_exposures": 0,
      "twofa_warnings": []
    }
  },
  "partial_failures": []
}
```

### 2. FastAPI Endpoints (`main.py`)

- `POST /webhooks/github`: Accepts GitHub webhook JSON payload. Validates HMAC SHA-256 signature `X-Hub-Signature-256` if webhook secret exists in keyring. Checks idempotency using `X-GitHub-Delivery`. Writes minimal metadata row to `github_events` (`notified_realtime = 1`, `included_in_briefing = 0`). Calls `generate_briefing(trigger="webhook")`.
- `GET /briefing/wake`: Calls `generate_briefing(trigger="wake")`. Returns 200 OK JSON response.
- `GET /briefing/on-demand`: Calls `generate_briefing(trigger="on_demand")`. Returns 200 OK JSON response.

### 3. Calendar Confirmation Module (`integrations/calendar_actions.py`)

- `propose_calendar_action(db_path: str, action_type: str, summary: str, start_time: str, end_time: str, description: str = "", event_id: str | None = None) -> dict`:
  - Validates `action_type` (`'create'`, `'update'`, `'delete'`), ISO-8601 timestamps, `end_time > start_time`, and length bounds.
  - Generates unique `proposal_token` (UUID4 string).
  - Inserts row into `calendar_actions` SQLite table (`confirmed_by_user = 0`, `status = 'proposed'`).
  - Returns dict with `proposal_token`, `prompt`, and `proposal_details`.
  - **Does NOT call Google Calendar API.**

- `confirm_calendar_action(db_path: str, proposal_token: str, confirmed: bool) -> dict`:
  - Fetches proposal from `calendar_actions` table.
  - Checks proposal TTL (15 minutes). If expired, sets `status = 'expired'` and returns error response without API call.
  - If `confirmed` is False: updates proposal `status = 'cancelled'`, returns cancellation response without API call.
  - If `confirmed` is True:
    - Performs atomic SQLite claim: `UPDATE calendar_actions SET status = 'claimed', confirmed_by_user = 1 WHERE proposal_token = ? AND status = 'proposed' AND created_at >= ?`
    - If `rowcount == 0` (already processed or claimed), rejects confirmation without API call.
    - If `rowcount == 1`: updates `status = 'executing'`.
    - Retrieves OAuth access token via `keyring_utils.get_credential("evie_assistant", "google_calendar_oauth")`.
    - Executes Google Calendar REST API v3 operation (`POST`, `PUT`, `DELETE`) via `httpx.Client` with `Authorization: Bearer <token>`.
    - If HTTP success: records `event_id`, sets `executed_at = CURRENT_TIMESTAMP`, `status = 'executed'`.
    - If HTTP failure: sets `status = 'execution_failed'` (does not mark executed), returns safe error code.

---

## Database Schemas & Data Minimization

From `04_Backend_Schema.md` (with Phase 2 data minimization & status lifecycle requirements):

```sql
CREATE TABLE IF NOT EXISTS github_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    delivery_id TEXT UNIQUE,           -- X-GitHub-Delivery header for idempotency
    repo_name TEXT NOT NULL,
    event_type TEXT NOT NULL,          -- 'commit' | 'pull_request' | 'issue'
    actor TEXT,                        -- who raised it
    summary TEXT,                      -- e.g. "8 commits pushed", "PR #42 opened"
    event_data TEXT,                   -- Intentionally NULL/empty in Phase 2 for data minimization
    received_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    notified_realtime INTEGER DEFAULT 0, -- 0/1, whether processed by webhook path
    included_in_briefing INTEGER DEFAULT 0 -- 0/1, whether surfaced in a briefing yet
);

CREATE TABLE IF NOT EXISTS calendar_actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    proposal_token TEXT UNIQUE,
    action_type TEXT NOT NULL,         -- 'create' | 'update' | 'delete'
    event_summary TEXT,
    start_time TEXT,
    end_time TEXT,
    description TEXT,
    event_id TEXT,                     -- Google Calendar resource ID
    confirmed_by_user INTEGER DEFAULT 0, -- must be 1 before execution
    status TEXT DEFAULT 'proposed',     -- 'proposed' | 'claimed' | 'executing' | 'executed' | 'execution_failed' | 'cancelled' | 'expired'
    executed_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

---

## Risk Analysis & Mitigation

1. **Risk**: External Calendar API outage or missing token causing briefing generator to crash.
   - **Mitigation**: Wrap Calendar fetch in `try...except Exception` inside `services/briefing.py`. Store safe error summary in `sections.calendar_today: {"status": "unavailable", "error_code": "CALENDAR_FETCH_FAILED"}` and append to `partial_failures` list, while completing GitHub and Security sections cleanly.
2. **Risk**: Unintended calendar mutations from LLM tool execution or race conditions.
   - **Mitigation**: Enforce mandatory two-step protocol (`propose_calendar_action` -> explicit user `/calendar/confirm` with atomic claim `UPDATE ... status = 'claimed' WHERE status = 'proposed'`). `/calendar/propose` has zero API mutation capabilities. Replayed requests fail the atomic claim.
3. **Risk**: GitHub event notification duplication across triggers or webhooks.
   - **Mitigation**: Check `delivery_id` for webhook idempotency. Query `github_events` for `included_in_briefing = 0` and atomically update `included_in_briefing = 1` upon inclusion in a briefing report.
4. **Risk**: Exposure of raw secret values, OAuth tokens, or PII in logs, responses, or tracebacks.
   - **Mitigation**: Prohibit storing raw webhook payloads in `github_events.event_data`. Return stable error codes (`CALENDAR_FETCH_FAILED`, `UNAUTHORIZED_WEBHOOK`) without raw exception strings. Filter log messages through `redaction.py`.
