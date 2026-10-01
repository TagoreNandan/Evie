# Phase 6 Validation Plan: GCSP Real Integration & End-to-End Validation

## Verification Strategy
Verify real integration adapters, OAuth refresh handling, error resilience, secret redaction, and end-to-end security briefing aggregation.

## Test Cases

### 1. OAuth & Credential Retrieval
- Verify `get_credential()` retrieves OAuth client secret and refresh token safely from OS Keyring.
- Verify Google OAuth access token refresh logic refreshes token without raising unhandled exceptions.

### 2. Integration Error Resilience & Rate Limits
- Test HIBP HTTP 429 rate limit response: adapter returns structured rate limit alert rather than throwing unhandled exception.
- Test GitHub HTTP 401 unauthorized response: adapter logs redacted error status.

### 3. Secret Non-Leakage Invariant
- Pass synthetic authorization header with raw bearer token through error formatter.
- Verify raw bearer token is redacted in returned exception detail and log output.

### 4. End-to-End Security Briefing Aggregation
- Invoke `generate_briefing()` with mock real-integration telemetry.
- Verify briefing summary includes HIBP findings, exposure alerts, 2FA status, and security score.

## Verification Command
```bash
python3 -m pytest tests/test_real_integrations.py
```
