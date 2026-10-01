# Evie

## What This Is

Evie is a personal, single-user conversational assistant designed to protect an individual's digital identity (API keys, code leaks, credential breaches, calendar/drive exposure, 2FA status) while serving as a long-term journal and RAG memory system, daily briefing generator, and evidence-based decision/research support system (DRS). It runs locally as a unified system sharing a single backend, database, and LLM budget.

## Core Value

Provide a unified, single-user personal security and productivity assistant that runs cost-effectively and guarantees credentials are never logged or stored insecurely.

## Requirements

### Validated

- ✓ [Codebase architecture & document structure] — existing
- ✓ [GSD planning framework & codebase map] — existing

### Active

- [ ] [GitHub Secret Scanner] — Commit scanner using pattern and entropy detection without storing or logging raw secret values
- [ ] [Breach Monitoring] — Have I Been Pwned (HIBP) scheduled email breach integration
- [ ] [Public Exposure Checker] — Google Drive and Google Calendar sharing exposure auditor
- [ ] [2FA Audit] — Account 2FA status inspector for Google and GitHub accounts
- [ ] [Daily Briefing Engine] — Unified briefing engine supporting instant webhook notifications, wake-triggered full reports, and on-demand queries
- [ ] [Calendar Write with Confirmation] — Natural language scheduling with explicit user confirmation before mutation
- [ ] [Journal & RAG Recall] — Daily logging and long-term vector/keyword memory search across arbitrary timeframes
- [ ] [Security Health Score] — Aggregated security metrics and trending health score over time
- [ ] [Remediation Guidance] — Actionable step-by-step fix instructions for flagged security risks
- [ ] [Dependency Alerts] — GitHub Dependabot vulnerability alerts integrated conversationally
- [ ] [Phishing Pattern Detection] — Read-only Gmail pattern analyzer for suspicious emails
- [ ] [DRS Research Agent] — Evidence-based web research agent producing sourced, non-definitive research support
- [ ] [Tone Configuration] — User-configurable conversational personality and tone settings

### Out of Scope

- Multi-tenant / public multi-user deployment — Evie is built strictly for a single user on local infrastructure.
- Real secret storage in SQLite — Credentials must always be stored in OS keyring or encrypted local file.
- Unconfirmed calendar mutations — Calendar writes require explicit user confirmation via `/calendar/propose` -> `/calendar/confirm`.
- Recurring paid APIs beyond HIBP — LLM and API costs must remain near zero (under ~$20/month total).
- Hardware / Raspberry Pi voice integration (Phase 3) — Deferred until core software features are complete.

## Context

Evie is being built as one unified system serving both academic (GCSP submission focus on security tools) and personal goals (memory, briefings, research assistant). The backend uses FastAPI, Python 3.11+, and SQLite (via sqlite3/sqlmodel). Secret detection requires zero secret leakage in logs or persistence.

## Constraints

- **Security**: Never log, print, store, or return a full secret value (redact to first/last 2 chars). Never store API keys or tokens in SQLite (use OS keyring).
- **Control**: Calendar write actions require explicit user confirmation before execution (`/calendar/propose` -> user confirms -> `/calendar/confirm`).
- **Architecture**: Single codebase and backend; all briefing triggers (webhook, wake, on-demand) must call one shared aggregation function.
- **Cost**: Cost-aware LLM routing; cheap models for routine tasks/briefings, stronger models reserved for DRS queries.
- **Google OAuth**: Must use "Production" publishing status, not "Testing", to avoid 7-day token expiration.

## Key Decisions

| Decision | Rationale | Outcome |
|----------|-----------|---------|
| Single Unified System | Avoids doubling LLM costs and infrastructure duplication between security features and personal assistant features | ✓ Good |
| OS Keyring for Credentials | Prevents leaking credentials or storing sensitive tokens in SQLite database | ✓ Good |
| Explicit Confirmation for Calendar Write | Protects user's real-world schedule from accidental or wrong LLM mutations | ✓ Good |
| Single Briefing Aggregation Logic | Prevents drift across real-time, wake-triggered, and on-demand briefing paths | ✓ Good |

## Evolution

This document evolves at phase transitions and milestone boundaries.

**After each phase transition** (via `/gsd-transition`):
1. Requirements invalidated? → Move to Out of Scope with reason
2. Requirements validated? → Move to Validated with phase reference
3. New requirements emerged? → Add to Active
4. Decisions to log? → Add to Key Decisions
5. "What This Is" still accurate? → Update if drifted

**After each milestone** (via `/gsd-complete-milestone`):
1. Full review of all sections
2. Core Value check — still the right priority?
3. Audit Out of Scope — reasons still valid?
4. Update Context with current state

---
*Last updated: 2026-09-19 after initialization*
