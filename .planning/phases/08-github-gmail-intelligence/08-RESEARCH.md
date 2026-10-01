# Phase 8 Research: GitHub + Gmail Personal & Work Intelligence Layer

## Domain Analysis
- **GitHub API Scope**:
  - `g.get_user().get_repos()` discovers all personal, private, and org repos accessible to the PAT.
  - `repo.get_commits()` retrieves commit history; author name, timestamp, message summary, and html_url.
  - `repo.get_pulls(state='open')` retrieves open PRs. `pr.get_reviews()` checks review count and flags unreviewed PRs (`len(reviews) == 0`).
  - Analytics aggregation: SQL date functions (`strftime('%Y-%m', received_at)`) on `github_events` calculate commits by month/year.
- **Gmail API Scope**:
  - Metadata fetch requested headers: `From`, `Return-Path`, `Subject`, `Authentication-Results`, `List-Unsubscribe`.
  - Classification heuristics:
    - `phishing_alert`: Failed DKIM/SPF or domain mismatch or urgency keywords.
    - `newsletter_promotional`: Presence of `List-Unsubscribe` header or marketing keywords.
    - `important`: High-priority sender or direct personal email without marketing headers.
  - Sender frequency analytics: Count emails per sender domain over time. Senders exceeding threshold (e.g. > 5 newsletters) generate cleanup/unsubscribe recommendations.
  - Two-Step Confirmation Safety: `propose_cleanup` generates a proposal token stored in `gmail_cleanup_proposals` table. Execution requires explicit POST to `confirm_cleanup`.

## SQLite Database Schemas
- `gmail_metadata` table:
  ```sql
  CREATE TABLE IF NOT EXISTS gmail_metadata (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      message_id TEXT UNIQUE NOT NULL,
      sender TEXT,
      sender_domain TEXT,
      subject TEXT,
      category TEXT, -- 'important', 'phishing_alert', 'newsletter_promotional', 'transactional'
      unsubscribe_header TEXT,
      received_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
  );
  ```
- `gmail_cleanup_proposals` table:
  ```sql
  CREATE TABLE IF NOT EXISTS gmail_cleanup_proposals (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      proposal_token TEXT UNIQUE NOT NULL,
      action_type TEXT NOT NULL, -- 'unsubscribe_recommendation', 'archive_recommendation'
      sender_domain TEXT NOT NULL,
      email_count INTEGER,
      status TEXT DEFAULT 'proposed', -- 'proposed', 'confirmed', 'cancelled'
      created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
  );
  ```

## Verification Strategy
- Synthetic unit tests in `tests/test_github_gmail_intelligence.py` verifying repo discovery, unreviewed PR detection, commit analytics, email categorization, sender frequency analytics, proposal confirmation safety, and `/chat` intent queries.
