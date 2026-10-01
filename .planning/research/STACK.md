# Stack Research

**Domain:** Single-User Personal Security & AI Assistant
**Researched:** 2026-09-19
**Confidence:** HIGH

## Recommended Stack

### Core Technologies

| Technology | Version | Purpose | Why Recommended |
|------------|---------|---------|-----------------|
| Python | 3.11+ | Runtime environment | Standard ecosystem for security scripts, AI integration, and light backend APIs |
| FastAPI | ^0.109.0 | Web application framework | Asynchronous, lightweight HTTP server for webhook endpoints and local client API |
| SQLite / SQLModel | ^0.0.14 | Database & ORM | Low overhead, zero maintenance, ideal for single-user local storage |
| keyring | ^24.3.0 | OS Keyring interface | Prevents raw secret storage in SQLite database by relying on macOS Keychain / OS Vault |

### Supporting Libraries

| Library | Version | Purpose | When to Use |
|---------|---------|---------|-------------|
| APScheduler | ^3.10.4 | Background job scheduling | Cron-like periodic execution of breach monitoring, 2FA audits, exposure checks |
| google-api-python-client | ^2.115.0 | Google Drive, Calendar, Gmail API | Exposure checker, 2FA audit, calendar write, read-only phishing scan |
| PyGithub | ^2.1.1 | GitHub REST API integration | Secret scanner commit watching, 2FA audit, Dependabot alerts |
| httpx | ^0.26.0 | Async HTTP client | HIBP breach queries, webhook delivery, LLM API calls |
| chromadb / sentence-transformers | ^0.4.22 | RAG Vector Store & Embeddings | Long-term memory indexing and natural language recall |

### Development Tools

| Tool | Purpose | Notes |
|------|---------|-------|
| pytest | Unit & integration testing | Required for true positive/negative test fixtures |
| ruff | Linting & formatting | Fast Python linter for code health |

## Installation

```bash
python -m venv .venv
source .venv/bin/activate
pip install fastapi uvicorn sqlmodel keyring apscheduler google-api-python-client PyGithub httpx chromadb pytest ruff
```

## Alternatives Considered

| Recommended | Alternative | When to Use Alternative |
|-------------|-------------|-------------------------|
| SQLModel / SQLite | PostgreSQL | Multi-tenant or distributed deployments (out of scope) |
| keyring | dotenv (.env file) | Quick dev prototyping, but unsafe for actual production tokens |
| FastAPI | Flask / Django | Django is overly heavy for a single-user local micro-assistant |

## What NOT to Use

| Avoid | Why | Use Instead |
|-------|-----|-------------|
| Heavy ORMs (SQLAlchemy full stack) | High complexity and overhead for single-user SQLite | SQLModel or raw sqlite3 |
| Hardcoded .env for tokens | Risk of accidental git commits and secret leakage | OS Keyring / `keyring` library |
| Multiple backend services | Duplicates LLM costs and memory infrastructure | Single unified FastAPI application |

## Version Compatibility

| Package A | Compatible With | Notes |
|-----------|-----------------|-------|
| FastAPI | Pydantic v2 | Ensure SQLModel version matches Pydantic v2 support |

## Sources

- `01_PRD.md` & `02_TRD.md` — Project specification documents
- `AGENTS.md` — Agent rules and stack guidelines

---
*Stack research for: Evie Personal Security & AI Assistant*
*Researched: 2026-09-19*
