---
phase: "1"
slug: "security-watchers-keyring-foundations"
status: validated
nyquist_compliant: true
wave_0_complete: true
created: "2026-09-19"
---

# Phase 1 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest |
| **Config file** | `pytest.ini` / `pyproject.toml` |
| **Quick run command** | `pytest tests/test_keyring.py tests/test_redaction.py tests/test_secret_scanner.py tests/test_breach_watch.py tests/test_twofa_audit.py tests/test_exposure_watch.py` |
| **Full suite command** | `pytest tests/` |
| **Estimated runtime** | ~0.3 seconds |

---

## Sampling Rate

- **After every task commit:** Run `pytest tests/`
- **After every plan wave:** Run `pytest tests/`
- **Before `/gsd-verify-work`:** Full suite must be green
- **Max feedback latency:** 5 seconds

---

## Per-Task Verification Map

| Task ID | Plan | Wave | Requirement | Threat Ref | Secure Behavior | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|------------|-----------------|-----------|-------------------|-------------|--------|
| 01-01-01 | 01 | 1 | SEC-05 | T-01-01 | Test set/get/delete, keyring failure behavior (raising `RuntimeError`), and no plaintext/DB fallback | unit | `pytest tests/test_keyring.py` | ✅ | ✅ green |
| 01-01-02 | 01 | 1 | SEC-01 | T-01-02 | Test `redact_secret(None)` / `""` returning `"****"`, case-insensitive dictionary masking, and non-mutating copy | unit | `pytest tests/test_redaction.py` | ✅ | ✅ green |
| 01-02-01 | 02 | 1 | SEC-01 | T-01-03 | Test synthetic detections (GitHub/OpenAI/Anthropic/AWS/Slack), token entropy, true negatives, and zero raw secret exposure in logs/errors | unit | `pytest tests/test_secret_scanner.py` | ✅ | ✅ green |
| 01-03-01 | 03 | 1 | SEC-02 | T-01-04 | Test breach parsing, 404=>`[]`, 401/403/429/5xx error propagation, keyring API key retrieval, and non-logging of full email/key | unit | `pytest tests/test_breach_watch.py` | ✅ | ✅ green |
| 01-03-02 | 03 | 1 | SEC-04 | T-01-05 | Test GitHub 2FA field parsing (`two_factor_authentication`) via keyring token; verify Google 2FA is explicitly deferred/blocked | unit | `pytest tests/test_twofa_audit.py` | ✅ | ✅ green |
| 01-04-01 | 04 | 1 | SEC-03 | T-01-06 | Test Drive public/anyone permissions, external-domain permissions via config, `nextPageToken` pagination & failure propagation, and Calendar ACL auditing without reading event contents | unit | `pytest tests/test_exposure_watch.py` | ✅ | ✅ green |

---

## Wave 0 Requirements

- [x] `tests/test_keyring.py` — OS keyring wrapper tests (set/get/delete, error handling, mock backend)
- [x] `tests/test_redaction.py` — String redaction and dictionary masking tests
- [x] `tests/test_secret_scanner.py` — Secret scanner pattern & candidate entropy tests
- [x] `tests/test_breach_watch.py` — HIBP breach monitor unit tests (HTTP status codes, keyring, PII sanitization)
- [x] `tests/test_twofa_audit.py` — 2FA auditor tests (GitHub 2FA parsing & Google 2FA deferral assertion)
- [x] `tests/test_exposure_watch.py` — Drive permissions & Calendar ACL auditor tests (pagination, trusted domains, ACL scope)

---

## Manual-Only Verifications

*All phase behaviors have automated verification.*

---

## Validation Sign-Off

- [x] All tasks have `<automated>` verify or Wave 0 dependencies
- [x] Sampling continuity: no 3 consecutive tasks without automated verify
- [x] Wave 0 covers all MISSING references (dependencies created & passed 31/31)
- [x] No watch-mode flags
- [x] Feedback latency < 5s
- [x] `nyquist_compliant: true` set in frontmatter

**Approval:** approved 2026-09-19
