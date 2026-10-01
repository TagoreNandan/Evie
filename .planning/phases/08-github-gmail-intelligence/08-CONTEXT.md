# Phase 8 Context: GitHub + Gmail Personal & Work Intelligence Layer

## Goals
Build the complete GitHub and Gmail personal/work intelligence layer for Evie Assistant, providing repository discovery, PR review tracking, commit analytics, Gmail read-only message classification, sender analytics, inbox cleanup recommendations, and factual conversational queries over stored activity.

## Requirements
- **`SEC-07`**: GitHub account repository discovery (personal, private, and organization repos), commit/push monitoring, open PR and unreviewed PR detection, historical analytics (commits by month/year), secret scanning persistence with actionable URLs, and factual conversational query routing.
- **`MAIL-01`**: Read-only Gmail inbox intelligence, major email identification, phishing analysis, promotional/newsletter classification, sender frequency analytics, cleanup/unsubscribe recommendations with explicit confirmation protocol, and factual conversational queries over stored email metadata.

## Design Decisions & Invariants
1. **GitHub Intelligence Engine (`services/github_intelligence.py`):**
   - Auto-discovers personal, private, and organization repositories accessible to the configured GitHub PAT.
   - Collects commits, push events, open PRs, and PR review status; flags unreviewed PRs needing attention.
   - Computes historical analytics (commits per month/year, top repositories, activity breakdown).
   - Persists all activity to SQLite `github_events` and secret findings to `secret_findings` with actionable diff links (`https://github.com/{repo}/commit/{sha}`).
2. **Read-Only Gmail Intelligence Engine (`services/gmail_intelligence.py`):**
   - Strictly READ-ONLY metadata fetching (`format=metadata` with headers `From`, `Return-Path`, `Subject`, `Authentication-Results`, `List-Unsubscribe`).
   - Categorizes emails into: `important`, `phishing_alert`, `newsletter_promotional`, `transactional`.
   - Computes sender frequency metrics and generates inbox cleanup / unsubscribe recommendations.
   - Proposal-confirmation safety boundary: Unsubscribe or cleanup recommendations generate a proposal card (`/gmail/propose_cleanup`) requiring explicit user confirmation (`/gmail/confirm_cleanup`) before performing any action.
3. **Conversational Intelligence Engine (`main.py`):**
   - Replaces fragile keyword/regex handlers with structured LLM intent parsing & grounded tool retrieval over local SQLite tables (`github_repo_cache`, `github_activity_log`, `secret_findings`, `gmail_metadata`).
   - Extracts query intent (`github_commits`, `github_secrets`, `github_prs`, `gmail_promotional`, `gmail_phishing`, `combined_briefing`, `greeting`, `unknown`) and entities (`repo_name`, `time_range`).
   - Handles natural-language variations, repository-specific queries (*"Show me commits in TagoreNandan/TagoreNandan"*), and flexible time expressions (*today, yesterday, this week, last month, recently*).
   - Resolves generic greetings (*"yo"*, *"hello"*, *"what can you do?"*) immediately with a concise capability summary without falling through to DRS.
   - Guarantees 100% factual responses grounded in local DB data — zero hallucinated data.
4. **24/7 GCSP Incremental & Asynchronous Monitoring Engine:**
   - Non-blocking execution: `POST /integrations/sync` launches sync in the background and returns `202 Accepted` immediately.
   - Incremental $O(Δ)$ scanning: Tracks `last_pushed_at` per repository in `github_sync_state`; skips repository diff fetching if `pushed_at <= last_pushed_at`.
   - Scalable to hundreds/thousands of repositories.
   - Incremental Gmail ingestion: Tracks `last_internal_date` in `gmail_sync_state` to process only new messages.
5. **Security & Redaction Invariants:**
   - Zero raw secrets, PATs, OAuth tokens, client secrets, Authorization headers, or full email bodies stored or logged.

## Scope Fences
- No modification of Phase 9 General-Purpose DRS engine or Phase 10 Taz hardware.
- No automated email deletion or automated unsubscribing without explicit two-step user confirmation card.
- All storage remains in SQLite and native OS keyring.
