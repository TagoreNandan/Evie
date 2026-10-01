# Architecture Research

**Domain:** Single-User Personal Security & AI Assistant
**Researched:** 2026-09-19
**Confidence:** HIGH

## Standard Architecture

### System Overview

```
┌─────────────────────────────────────────────────────────────┐
│                    API / Webhook / CLI                      │
│                  (FastAPI Router Modules)                   │
├─────────────────────────────────────────────────────────────┤
│                    Core Business Services                   │
│  ┌────────────┐  ┌────────────┐  ┌────────────┐  ┌───────┐  │
│  │github_watch│  │breach_watch│  │exposure_w..│  │twofa..│  │
│  └─────┬──────┘  └─────┬──────┘  └─────┬──────┘  └───┬───┘  │
│        │               │               │             │      │
│  ┌─────┴──────┐  ┌─────┴──────┐  ┌─────┴──────┐  ┌───┴───┐  │
│  │ briefing   │  │ calendar   │  │ journal RAG│  │ drs   │  │
│  └────────────┘  └────────────┘  └────────────┘  └───────┘  │
├─────────────────────────────────────────────────────────────┤
│                      Shared Subsystems                      │
│  ┌───────────────────────────────────────────────────────┐  │
│  │   scheduler.py (APScheduler background job engine)    │  │
│  └───────────────────────────────────────────────────────┘  │
├─────────────────────────────────────────────────────────────┤
│                     Persistence & Vault                     │
│  ┌────────────────────┐   ┌──────────────────────────────┐  │
│  │ SQLite DB (Data)   │   │ OS Keyring (Secrets/Tokens)  │  │
│  └────────────────────┘   └──────────────────────────────┘  │
└─────────────────────────────────────────────────────────────┘
```

### Component Responsibilities

| Component | Responsibility | Typical Implementation |
|-----------|----------------|------------------------|
| `github_watch.py` | Scans commit diffs for leaked secrets (entropy + regex patterns). Redacts secrets. | PyGithub / Git subprocess + Regex/Entropy |
| `breach_watch.py` | Polls Have I Been Pwned (HIBP) API on schedule for user email breaches | `httpx` async GET requests |
| `exposure_watch.py` | Audits Drive files & Calendar events shared publicly/externally | Google API Client |
| `twofa_audit.py` | Inspects 2FA setup status for Google and GitHub accounts | PyGithub + Google Admin/User APIs |
| `briefing.py` | Aggregates overnight events, schedule, security alerts into single report | Centralized function called by webhook/wake/query |
| `calendar_actions.py` | Handles `/calendar/propose` and `/calendar/confirm` flows | Google Calendar API |
| `journal.py` | Daily diary entry ingestion, vector indexing, and RAG recall | ChromaDB / SQLite + sentence-transformers |
| `drs.py` | Decision Research Support engine (web search + LLM synthesis + source attribution) | DuckDuckGo/Exa/Brave search + LLM |
| `scheduler.py` | Schedules periodic watches (breach checks, exposure scans) | APScheduler BackgroundScheduler |
| `config.py` | Non-secret system configuration | YAML or Pydantic BaseSettings |

## Recommended Project Structure

```
evie/
├── main.py                     # FastAPI entry point & CLI commands
├── config.py                   # Non-secret configuration settings
├── scheduler.py                # Centralized background task scheduler
├── database.py                 # SQLite database setup & SQLModel sessions
├── keyring_utils.py            # OS keyring wrapper for token storage/retrieval
├── integrations/
│   ├── github_watch.py         # GitHub secret scanner & commit watcher
│   ├── breach_watch.py         # HIBP breach monitoring integration
│   ├── exposure_watch.py       # Drive & Calendar sharing exposure auditor
│   ├── twofa_audit.py          # 2FA account security inspector
│   ├── calendar_actions.py     # Calendar propose & confirm write handlers
│   ├── journal.py              # Journal ingestion & RAG recall engine
│   └── drs.py                  # DRS evidence-based research engine
├── services/
│   └── briefing.py             # Single aggregation function for briefings
└── models/                     # SQLModel database schemas & Pydantic DTOs
```

### Structure Rationale

- **One module per integration:** Keeps security tools and assistant capabilities strictly isolated.
- **Centralized `scheduler.py`:** Prevents fragmented `while True` loops across modules.
- **Dedicated `services/briefing.py`:** Ensures webhook, wake-triggered, and on-demand paths call one shared function.
- **No secrets in DB:** `keyring_utils.py` handles token lookup from OS Vault.

## Architectural Patterns

### Pattern 1: Redacted Secret Handling
**What:** All secret scanner outputs mask matching tokens (showing only first/last 2 chars).
**When to use:** In all logging, database writes, terminal prints, and API responses.

### Pattern 2: Confirmation-Gated Mutations
**What:** Two-step write flow: `/calendar/propose` returns proposal token; `/calendar/confirm` executes mutation.
**When to use:** Any external mutation action (calendar additions/edits).

## Anti-Patterns

### Anti-Pattern 1: Duplicated Briefing Logic
**What people do:** Writing separate briefing builders for webhooks, morning wake voice, and chat query.
**Why it's wrong:** The three paths drift out of sync over time.
**Do this instead:** Call a single shared aggregation function in `services/briefing.py`.

### Anti-Pattern 2: Storing Credentials in Database / Environment Files
**What people do:** Putting API keys into SQLite table or committing `.env`.
**Why it's wrong:** High risk of accidental secret leak or git commit exposure.
**Do this instead:** Read/write credentials using OS keyring library (`keyring`).

---
*Architecture research for: Evie Personal Security & AI Assistant*
*Researched: 2026-09-19*
