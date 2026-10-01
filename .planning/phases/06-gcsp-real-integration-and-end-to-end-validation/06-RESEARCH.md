# Phase 6 Research: GCSP Real Integration & End-to-End Validation

## Domain Overview
Phase 6 connects the modular security watchers (GitHub commit secret scanner, HIBP breach watch, Google Drive/Calendar exposure checker, account 2FA audit, Dependabot alerts, Gmail phishing scanner) to live integration adapters and validates end-to-end telemetry flow into `services/briefing.py` and `security_score.py`.

## Key Technical Decisions

### 1. Keyring & OAuth Token Life Cycle
- Google OAuth refresh tokens expire after 7 days if the Google Cloud OAuth Consent Screen is left in "Testing" mode.
- In production, refresh tokens remain valid indefinitely until revoked.
- `services/real_integrations.py` implements automatic access token refresh using client ID, client secret, and refresh token retrieved from `keyring`.

### 2. Error Handling & Secret Protection
- External API calls (GitHub REST, HIBP v3, Google Drive v3, Google Calendar v3, Gmail v1) must catch network errors (`urllib.error.HTTPError`, `requests.exceptions.RequestException`) and wrap them in sanitized error dicts.
- Exception details must pass through `redaction.redact_secrets()` before logging or outputting.

### 3. Verification & Testing Strategy
- Unit tests run against mock adapters with synthetic real-world responses to verify token refresh, rate limit handling, and secret redaction.
- Integration tests verify that `generate_briefing()` and `compute_security_score()` aggregate findings across all active integrations seamlessly.
