# Testing Strategy & Test Plan

**Analysis Date:** 2026-09-19

## Overview
Evie's test suite guarantees feature accuracy, security isolation, RAG recall performance, and zero-leakage safety criteria per `docs/06_Acceptance_Criteria.md`.

---

## Test Framework & Tools
- **Test Framework**: `pytest` with `pytest-asyncio` for asynchronous FastAPI router tests.
- **HTTP Mocking**: `respx` or `unittest.mock` for external API responses (GitHub, Google, HIBP).
- **Synthetic Test Data**: Strictly fake/synthetic API keys and credentials in test fixtures. Real API tokens or secrets are explicitly banned from test code.

---

## Acceptance Test Suite Matrix

| Component | Target Behavior | Test Verification Method |
|---|---|---|
| **Secret Scanner** | True Positive Detection | Pass synthetic AWS key `AKIA_SYNTHETIC_MOCK_KEY` in fake commit; verify scanner flags finding within cycle. |
| **Secret Scanner** | True Negative Verification | Pass clean code commit; verify zero findings produced. |
| **Secret Scanner** | Redaction Safety | Verify `redacted_preview` contains maximum 4 visible characters (`AKIA****...**LE`). |
| **Secret Scanner** | False Positive Ignore | Mark finding `false_positive`; verify ignored on re-scan. |
| **Breach Watch** | Known Breach Detection | Query test account `test@example.com`; verify breach record stored without duplication. |
| **Public Exposure** | Drive & Calendar Audit | Flag items marked "anyone with the link" or "public"; ignore private items. |
| **2FA Audit** | Status Check | Mock Google/GitHub 2FA responses; verify database accurately updates status. |
| **Daily Briefing** | Shared Aggregation | Verify real-time, wake-triggered, and on-demand paths execute shared `get_full_report()` without state drift. |
| **Calendar Write** | Two-Phase Execution | Verify `/calendar/propose` generates confirmation token and `/calendar/confirm` executes write only when confirmed. |
| **Memory / RAG** | Temporal & Topic Search | Verify natural language queries ("what did I do 6 months ago") return correct Chroma embeddings. |
| **Security Score** | Dynamic Calculation | Verify score recalculation after resolving a finding matches historical trend. |
| **DRS System** | Citation & Framing | Verify all DRS responses contain URL sources and non-absolute recommendation framing. |

---

## Post-Test Security Inspection Protocol

After running test suites, a post-execution security sweep must verify zero secret leaks:

```bash
# Automated secret sweep across test DB and output logs
grep -E '(sk-[a-zA-Z0-9]{20,}|AKIA[A-Z0-9]{16}|ghp_[a-zA-Z0-9]{36})' test_run.log evie_test.db 2>/dev/null && echo "FAIL: Secret Leak Detected" || echo "PASS: Zero Secrets Leaked"
```

---

*Testing analysis: 2026-09-19*
<!-- refreshed: 2026-09-19 -->
