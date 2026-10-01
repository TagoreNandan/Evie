# Phase 1: Security Watchers & Keyring Foundations — Context & Decisions

## Phase Scope

Phase 1 establishes the foundational credential storage layer and core security detection watchers for Evie:
- **`SEC-05`**: OS Keyring interface (`keyring` library wrapper in `keyring_utils.py`) for storing API keys, OAuth tokens, and secrets.
- **`SEC-01`**: GitHub commit secret scanner (`integrations/github_watch.py`) detecting API keys and credentials using regex pattern matching and entropy analysis. **Hard Rule:** Secrets are ALWAYS redacted (first 2 and last 2 characters only) in output/logs/DB.
- **`SEC-02`**: Email breach watcher (`integrations/breach_watch.py`) polling Have I Been Pwned (HIBP) API.
- **`SEC-03`**: Public exposure auditor (`integrations/exposure_watch.py`) for Google Drive files and Google Calendar ACLs/calendar resources (event contents are strictly not inspected).
- **`SEC-04`**: Account 2FA auditor (`integrations/twofa_audit.py`) inspecting GitHub account 2FA setup (Google personal 2FA auditing is explicitly deferred/blocked pending a documented supported API or approved mechanism).

## Key Design Decisions & Fences

1. **Zero Secret Persistence / Logging:** Unredacted secret strings must NEVER be stored in SQLite, logged to stdout/logfiles, or returned unmasked. A `redact_secret(val: str) -> str` utility is mandatory.
2. **OS Keyring Vault:** Use Python's `keyring` package to interact with OS native secret storage (macOS Keychain). No credentials in SQLite database or committed `.env` files.
3. **Synthetic Test Fixtures:** Every detector must be accompanied by synthetic true positive and true negative test cases in `tests/`. Real credentials must NEVER be placed in test files.
4. **No Mocking External Failures:** If external APIs (HIBP, GitHub, Google) fail, surface real errors rather than returning fake mock data.

## Deliverables

- `keyring_utils.py` — OS Keyring wrapper for credential management
- `integrations/github_watch.py` — Secret scanner with regex + entropy detection and string redaction
- `integrations/breach_watch.py` — Scheduled HIBP email breach monitor
- `integrations/exposure_watch.py` — Google Drive permissions & Calendar ACL sharing exposure checker
- `integrations/twofa_audit.py` — 2FA status auditor for GitHub accounts (Google 2FA deferred)
- `tests/test_secret_scanner.py`, `tests/test_breach_watch.py`, `tests/test_exposure_watch.py`, `tests/test_twofa_audit.py`, `tests/test_keyring.py` — Comprehensive unit tests with synthetic data
