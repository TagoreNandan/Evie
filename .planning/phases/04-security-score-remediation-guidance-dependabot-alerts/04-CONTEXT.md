# Phase 4: Security Score, Remediation Guidance & Dependabot Alerts — Context & Decisions

## Phase Scope

Phase 4 aggregates Evie's security signals into a trending health score, provides step-by-step remediation guides, tracks Dependabot alerts, and scans Gmail headers for phishing patterns:
- **`SCORE-01`**: Dynamic 0–100 Security Health Score aggregator and trending engine (`security_score.py`, SQLite `security_score_history` table, `GET /score` endpoint).
- **`SCORE-02`**: Step-by-step remediation guide generator (`remediation.py`).
- **`SCORE-03`**: GitHub Dependabot alert monitor (`integrations/dependency_watch.py`, SQLite `dependency_alerts` table).
- **`SCORE-04`**: Read-only Gmail phishing pattern analyzer (`integrations/phishing_scan.py`).

## Key Design Decisions & Fences

1. **Deterministic 0–100 Security Score**:
   - Starts at baseline 100 points.
   - Deducts fixed penalty weights for open findings:
     - Open secrets: -15 pts per secret finding
     - Unacknowledged breaches: -10 pts per breach check
     - Public exposures: -10 pts per public Drive/Calendar item
     - Missing 2FA: -15 pts per unenabled account
     - Critical/High Dependabot alerts: -10 pts per high/critical vulnerability
   - Minimum floor: 0. Breakdown stored as JSON in `security_score_history`.
2. **Actionable Remediation Guidance**: Returns step-by-step resolution instructions for each finding category without executing automatic destructive fixes.
3. **Read-Only Email & Dependabot Access**:
   - Dependabot watcher uses GitHub API read-only scope (`security_events`).
   - Phishing analyzer uses Gmail API `gmail.readonly` scope. No email send/modify capabilities.
4. **Credential Safety**: All API keys and OAuth tokens retrieved from OS Keyring (`keyring_utils`).

## Deliverables

- `security_score.py` — Security health score calculation & trend logger
- `remediation.py` — Remediation guide generator
- `integrations/dependency_watch.py` — Dependabot vulnerability alert fetcher
- `integrations/phishing_scan.py` — Gmail phishing header pattern analyzer
- `main.py` — Endpoints (`GET /score`, `GET /remediation/{finding_type}/{id}`, `GET /findings`)
- `tests/test_security_score.py`, `tests/test_remediation.py`, `tests/test_phishing_scan.py`
