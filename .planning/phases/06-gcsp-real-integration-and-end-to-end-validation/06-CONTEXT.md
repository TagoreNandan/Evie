# Phase 6 Context: GCSP Real Integration & End-to-End Validation

## Goals
Connect Evie's completed security capabilities (Secret Scanner, Breach Watcher, Public Exposure Auditor, 2FA Audit, Security Health Score, Briefing Aggregator) to real external service APIs (GitHub, HIBP, Google Drive/Calendar/Gmail) and validate the complete security briefing flow end-to-end (`SEC-06`, `BRIEF-05`, `SCORE-05`).

## Requirements
- **`SEC-06`**: Real integration adapters for GitHub PAT, HIBP API, Google OAuth, and Gmail API operating safely under OS Keyring credential management.
- **`BRIEF-05`**: End-to-end briefing aggregation over live/simulated external telemetry feeds.
- **`SCORE-05`**: Real-world finding aggregation and dynamic security score computation.

## Design Decisions & Invariants
1. **OS Keyring Pre-flight & Production OAuth (`keyring_utils.py`):**
   - Credentials loaded strictly from OS Keyring (`github_pat`, `hibp_api_key`, `google_oauth_client_secret`, `google_oauth_refresh_token`, `llm_api_key`).
   - Google OAuth app configured with publishing status "Production" to prevent 7-day refresh token expiration.
2. **Resilient Integration Adapters (`services/real_integrations.py`):**
   - Handle rate limits (HTTP 429), OAuth token refresh failures, and network timeouts gracefully.
   - Surface structured error codes without ever leaking raw exceptions or authorization header strings.
3. **Data Protection Invariant:**
   - Zero raw secrets, passwords, or API keys printed, logged, or returned in findings or briefing responses.

## Scope Fences
- Do not commit real credentials to repository files or test fixtures.
- Gracefully handle offline or missing API key scenarios by returning clear error statuses rather than crashing.
