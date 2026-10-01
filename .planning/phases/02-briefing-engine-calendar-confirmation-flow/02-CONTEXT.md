# Phase 2: Briefing Engine & Calendar Confirmation Flow — Context & Decisions

## Phase Scope

Phase 2 builds the central daily briefing aggregation engine and confirmation-gated calendar event scheduling system:
- **`BRIEF-01`**: Single unified briefing aggregation function (`services/briefing.py`) that aggregates overnight GitHub activity (commits, PRs), today's calendar events, active security findings (from Phase 1 watchers), and general highlights.
- **`BRIEF-02`**: Real-time webhook endpoint (`POST /webhooks/github`) that receives GitHub events and triggers instant notifications calling `services/briefing.py`.
- **`BRIEF-03`**: Wake-triggered report command/endpoint (`GET /briefing/wake`) delivering a complete morning briefing upon user greeting.
- **`BRIEF-04`**: On-demand query endpoint (`GET /briefing/on-demand`) enabling fresh briefing reports at any time.
- **`CAL-01`**: Calendar proposal endpoint (`POST /calendar/propose`) accepting structured calendar action fields (`action_type`, `summary`, `start_time`, `end_time`, `description`, `event_id`) and creating a confirmation-gated proposal with a unique proposal token without making any external API calls.
- **`CAL-02`**: Confirmation endpoint (`POST /calendar/confirm`) executing mutation to Google Calendar API only after explicit user confirmation (`confirmed = True`), proposal TTL validation (15-minute expiration), and a successful atomic proposal claim (`proposed` -> `claimed` -> `executing` -> `executed` or `execution_failed` / `cancelled` / `expired`).

## Key Design Decisions & Fences

1. **Single Aggregation Function (`BRIEF-01`)**: Real-time webhook, wake-triggered, and on-demand paths MUST call the exact same aggregation logic in `services/briefing.py`. No duplicating logic across triggers.
2. **Two-Step Calendar Confirmation (`CAL-01`, `CAL-02`)**: No calendar event creation, update, or deletion may execute directly without going through `/calendar/propose` (returns a unique proposal token) -> User Approval -> `/calendar/confirm`. Only a successfully atomically claimed proposal (`proposed` -> `claimed`) with `confirmed = True` and unexpired TTL may trigger a Google Calendar API mutation.
3. **No Fake API Data**: If external APIs or credentials fail, surface the error clearly with safe structured status codes rather than silently returning mock/placeholder data or leaking raw exception details/tokens.
4. **Credential Retrieval & Secrecy**: Retrieve Google OAuth / Calendar API credentials and GitHub webhook secret strictly from OS Keyring (`keyring_utils.get_credential`). Credentials and tokens must never appear in logs, API responses, or tracebacks.

## Deliverables

- `services/briefing.py` — Shared briefing aggregation engine
- `integrations/calendar_actions.py` — Calendar propose and confirm mutation handlers
- `main.py` / FastAPI routes — Webhook listener (`/webhooks/github`), wake trigger (`/briefing/wake`), on-demand query (`/briefing/on-demand`), and calendar endpoints (`/calendar/propose`, `/calendar/confirm`)
- `tests/test_briefing.py` & `tests/test_calendar_actions.py` — Unit & integration tests with mocked external APIs
