# Phase 4: Security Score, Remediation Guidance & Dependabot Alerts — Validation Strategy

## Overview & Automated Test Commands

Phase 4 validation ensures accurate 0–100 security health scoring (`SCORE-01`), step-by-step remediation guide generation (`SCORE-02`), Dependabot alert monitoring (`SCORE-03`), and phishing pattern detection (`SCORE-04`).

### Quick Test Execution

```bash
pytest tests/test_security_score.py tests/test_remediation.py tests/test_phishing_scan.py
```

### Full System Test Execution

```bash
pytest tests/
```

---

## Test Suite & Requirement Mapping Matrix

| Test Module | Target Function / Endpoint | Tested Behavior / Scenario | Requirement |
|---|---|---|---|
| `tests/test_security_score.py` | `compute_security_score()` | Calculates 0–100 score from active findings and logs history in `security_score_history`. | `SCORE-01` |
| `tests/test_security_score.py` | `compute_security_score()` | Floor boundary test: score cannot drop below 0 despite multiple active findings. | `SCORE-01` |
| `tests/test_remediation.py` | `get_remediation_guide()` | Returns actionable step-by-step resolution steps for secret, breach, exposure, 2FA, and dependency findings. | `SCORE-02` |
| `tests/test_remediation.py` | `check_dependency_alerts()` | Fetches GitHub Dependabot alerts and persists entries in `dependency_alerts`. | `SCORE-03` |
| `tests/test_phishing_scan.py` | `scan_phishing_patterns()` | Identifies spoofed headers, financial urgency keywords, and suspicious links in email test fixtures. | `SCORE-04` |
| `tests/test_security_score.py` | `GET /score` | FastAPI endpoint returns current score, breakdown, and historical trend. | `SCORE-01` |
| `tests/test_remediation.py` | `GET /remediation/{type}/{id}` | FastAPI endpoint returns structured remediation guide for specified finding. | `SCORE-02` |

---

## Hard Security & Operational Fences

1. **Zero Unsafe Mutations**: Remediation guides provide instructional steps only; no automated destructive actions or secret modifications are executed.
2. **Read-Only Scope**: Dependabot and Gmail phishing watchers run with read-only scopes.
3. **No Secret Leaks**: Raw secret values are never returned in remediation guides or score outputs.

---

## Verification Status

- Wave 0 dependencies: Identified (Pending Plan Execution)
- Wave 1 unit tests (`test_security_score.py`, `test_remediation.py`, `test_phishing_scan.py`): Pending
- Overall Phase 4 status: Ready for Execution
