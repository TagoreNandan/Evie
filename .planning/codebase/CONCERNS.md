# Technical Debt & System Concerns

**Analysis Date:** 2026-09-19

## Overview
This document tracks critical technical debt, potential architectural risks, fragile integrations, and operational concerns identified across the Evie codebase specifications.

---

## High-Priority Concerns & Risks

### 1. Secret Scanner False Positive Rate & Pattern Tuning
- **Risk**: Shannon entropy scoring and regex matching on commit diffs can flag false positives on synthetic hashes, UUIDs, or build artifacts.
- **Mitigation**: Maintain an explicit user allow-list in SQLite (`false_positive` status in `secret_findings`) to prevent re-flagging ignored entries.

### 2. Google OAuth Token Expiry (Publishing Status Risk)
- **Risk**: Google Cloud OAuth apps created in "Testing" mode expire refresh tokens after 7 days, forcing repetitive re-authentication.
- **Mitigation**: Ensure the Google OAuth app publishing status is set to **Production** during setup.

### 3. LLM API Cost Overhead
- **Risk**: Unrestricted calls to high-tier LLM models for routine daily briefings or summarization could exceed the target $20/month budget.
- **Mitigation**: Enforce dual-model tiering in `config.py` — route routine queries to low-cost text models and reserve premium models exclusively for DRS research synthesis.

### 4. Logic Drift Across Notification Delivery Paths
- **Risk**: Duplicating reporting logic between real-time webhooks, wake-triggered greetings, and on-demand queries can lead to inconsistent report states.
- **Mitigation**: Enforce a strict single-entry-point architecture where all three paths consume the unified `get_full_report()` function in the orchestrator.

### 5. Architectural Splitting Risk (GCSP vs Personal Evie)
- **Risk**: Attempting to split security/monitoring features into a separate codebase for academic evaluation creates dual maintenance overhead and doubled API costs.
- **Mitigation**: Maintain Evie as **one single, unified codebase** per `AGENTS.md` and `PRD.md`. Security features represent a presentation lens of the unified platform.

---

## Operational & Architectural Roadmap Concerns

| Issue / Area | Impact | Priority | Recommended Action |
|---|---|---|---|
| **Notes Platform Choice** | Storage integration dependency | Medium | Finalize platform choice (Notion vs Keep) early in Phase 1 before journal module completion. |
| **HIBP Rate Limiting** | Background job stalls | Low | Ensure `breach_watch.py` respects HIBP API rate limits and logs errors gracefully without process failure. |
| **Pi Hardware Portability** | Latency & environment drift | Medium | Keep backend dependencies portable and lightweight so the exact same FastAPI process runs on Raspberry Pi 4. |

---

*Concerns analysis: 2026-09-19*
<!-- refreshed: 2026-09-19 -->
