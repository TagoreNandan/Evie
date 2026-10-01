# Phase 4: Security Score, Remediation Guidance & Dependabot Alerts — Research & Architecture

## Executive Summary & Goals

Phase 4 implements Evie's security scoring, remediation guidance, Dependabot alert tracking, and phishing pattern scanner (`SCORE-01`, `SCORE-02`, `SCORE-03`, `SCORE-04`).

The key objectives are:
1. **Dynamic Security Score (`SCORE-01`)**: Compute a deterministic 0–100 Security Health Score by aggregating open findings across secrets, breaches, exposures, 2FA, and dependencies, and record historical trends in SQLite `security_score_history`.
2. **Remediation Guidance (`SCORE-02`)**: Provide clear, step-by-step instructions for revoking leaked secrets, acknowledging breaches, tightening Drive permissions, and upgrading vulnerable packages.
3. **Dependabot Alert Monitor (`SCORE-03`)**: Query GitHub Dependabot alerts API for monitored repositories and store vulnerability findings in SQLite `dependency_alerts`.
4. **Read-Only Gmail Phishing Scanner (`SCORE-04`)**: Inspect email headers/subjects via `gmail.readonly` for common phishing indicators (urgent action requests, mismatched sender domains, suspicious link patterns).

---

## Component Architecture & System Design

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                            FastAPI Application                              │
│                                 (main.py)                                   │
│                                                                             │
│      ┌──────────────┐     ┌───────────────────────┐    ┌─────────────────┐  │
│      │ GET /score   │     │ GET /findings/remedi- │    │ GET /findings   │  │
│      │              │     │     ate/{type}/{id}   │    │                 │  │
│      └──────┬───────┘     └───────────┬───────────┘    └────────┬────────┘  │
│             │                         │                         │           │
│             ▼                         ▼                         ▼           │
│    security_score.py            remediation.py         Phase 1-4 Watchers   │
│ (compute & log trend)       (step-by-step guides)      (Dependabot/Phish)   │
└─────────────┬─────────────────────────┬─────────────────────────┬───────────┘
              │                         │                         │
              └─────────────────────────┼─────────────────────────┘
                                        │
                                        ▼
                             ┌────────────────────┐
                             │ SQLite: evie.db    │
                             │ (score_history,    │
                             │  dependency_alerts)│
                             └────────────────────┘
```

---

## Technical Specifications & Scoring Algorithm

### 1. Security Score Engine (`security_score.py`)

`compute_security_score(db_path: str = "evie.db") -> dict`

**Deduction Weights**:
- Baseline: `100`
- Secret findings (`open`): `-15` points per open finding (max deduction `-45`)
- Breach checks (`unacknowledged`): `-10` points per breach (max deduction `-30`)
- Exposure findings (`open`): `-10` points per item (max deduction `-30`)
- 2FA audit (`twofa_enabled = 0`): `-15` points per unenabled provider (max deduction `-30`)
- Dependabot alerts (`critical` / `high`): `-10` points per vulnerability (max deduction `-30`)

**Score Range**: Clamped between `0` and `100`.

**Output**:
```json
{
  "score": 75,
  "trend": "stable",
  "breakdown": {
    "secrets": -15,
    "breaches": -10,
    "exposures": 0,
    "twofa": 0,
    "dependencies": 0
  },
  "computed_at": "2026-09-19T17:00:00Z"
}
```

### 2. Remediation Guide Generator (`remediation.py`)

`get_remediation_guide(finding_type: str, finding_id: int, db_path: str = "evie.db") -> dict`

Supports finding types: `'secret'`, `'breach'`, `'exposure'`, `'twofa'`, `'dependency'`.

Returns step-by-step remediation dict:
```json
{
  "finding_type": "secret",
  "finding_id": 1,
  "title": "Remediate Leaked AWS Secret Key",
  "steps": [
    "1. Log in to AWS IAM Console immediately.",
    "2. Revoke and delete the compromised Access Key ID.",
    "3. Generate a new Access Key pair and save it strictly in OS Keyring.",
    "4. Invalidate git history or push a clean commit."
  ],
  "severity": "critical"
}
```

### 3. Dependabot Watcher (`integrations/dependency_watch.py`)

`check_dependency_alerts(repo_name: str, github_pat: str | None = None, db_path: str = "evie.db") -> list[dict]`

Queries `https://api.github.com/repos/{owner}/{repo}/dependabot/alerts`.
Stores open alerts in `dependency_alerts` SQLite table.

### 4. Gmail Phishing Pattern Scanner (`integrations/phishing_scan.py`)

`scan_phishing_patterns(messages: list[dict]) -> list[dict]`

Inspects message headers (`From`, `Subject`, `Return-Path`) for phishing heuristics (DKIM failure flags, urgent financial keywords, spoofed domain patterns). Does **NOT** alter emails.

---

## Database Schemas

From `04_Backend_Schema.md`:

```sql
CREATE TABLE IF NOT EXISTS security_score_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    score INTEGER NOT NULL,            -- 0-100
    breakdown TEXT,                    -- JSON string
    computed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS dependency_alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_name TEXT NOT NULL,
    package_name TEXT,
    severity TEXT,                     -- 'low' | 'moderate' | 'high' | 'critical'
    advisory_url TEXT,
    detected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    status TEXT DEFAULT 'open'
);
```

---

## Risk Analysis & Mitigation

1. **Risk**: External API outage crashing score calculation.
   - **Mitigation**: Score calculation reads directly from local SQLite findings tables (`secret_findings`, `breach_checks`, etc.) rather than calling external APIs synchronously.
2. **Risk**: Unintended email modification or deletion.
   - **Mitigation**: Scanner strictly uses read-only analysis functions; zero write/delete endpoints exist.
