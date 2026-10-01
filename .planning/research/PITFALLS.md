# Pitfalls Research

**Domain:** Single-User Personal Security & AI Assistant
**Researched:** 2026-09-19
**Confidence:** HIGH

## Critical Pitfalls

### Pitfall 1: Secret Leakage in Logs, Database, or Terminal Output

**What goes wrong:** Detected secret values (API keys, OAuth tokens) get printed to stdout, logged to logfiles, or saved in plain text in the SQLite database.
**Why it happens:** Print statements or default ORM logger output secret scanner results without redacting sensitive strings.
**How to avoid:** Enforce strict string redaction (first 2 and last 2 characters visible only, e.g. `sk-12...9x`). Never save unredacted secrets anywhere in logs or DB.
**Warning signs:** Log files or database columns containing long alphanumeric strings matching API key patterns.
**Phase to address:** Phase 1 (GitHub Secret Scanner).

---

### Pitfall 2: Google OAuth Token Expiry (Testing vs. Production Status)

**What goes wrong:** Google API tokens expire after 7 days, forcing frequent re-authentication.
**Why it happens:** The Google Cloud Console OAuth consent screen is left in "Testing" publishing status rather than "Production".
**How to avoid:** Set OAuth App publishing status to "Production" in Google Cloud Console, even for personal single-user apps.
**Warning signs:** `invalid_grant` or `Token expired` errors every 7 days.
**Phase to address:** Phase 1 (OAuth & API integration setup).

---

### Pitfall 3: Unconfirmed Calendar Mutations (Hallucination Damage)

**What goes wrong:** The LLM creates, overwrites, or deletes calendar entries directly without user confirmation based on ambiguous user prompts.
**Why it happens:** Directly calling Google Calendar API endpoints from an LLM tool call loop.
**How to avoid:** Mandate a two-step confirmation flow (`/calendar/propose` -> User Approval -> `/calendar/confirm`).
**Warning signs:** Calendar events being created without prior approval prompts in the chat UI.
**Phase to address:** Phase 1 (Calendar Write).

---

### Pitfall 4: Fabricating Mock External API Responses

**What goes wrong:** System silently returns mock/synthetic data when an external API (HIBP, Google, GitHub) fails or returns an error code.
**Why it happens:** Over-defensive exception handling returning dummy fallbacks.
**How to avoid:** Explicitly catch and surface external API errors instead of masking them.
**Warning signs:** Briefing or audit reports claiming zero issues when API network requests are failing.
**Phase to address:** Phase 1 & 2 across all integrations.

---

## Technical Debt Patterns

| Shortcut | Immediate Benefit | Long-term Cost | When Acceptable |
|----------|-------------------|----------------|-----------------|
| Storing tokens in `.env` or DB | Easier setup | Risk of committing keys or leaking database | NEVER — use OS Keyring |
| Hardcoding mock responses | Fast UI testing | Hides API breakage and fails hard rule #4 | Test fixtures only |
| Duplicating briefing logic | Quick prototype | Code paths drift out of sync | NEVER — use shared `services/briefing.py` |

## Security Mistakes

| Mistake | Risk | Prevention |
|---------|------|------------|
| Unredacted secret logging | Credential exposure in logs/DB | Mandate `redact_secret()` helper everywhere |
| Plaintext DB credential storage | Key theft on local disk compromise | Rely on macOS Keychain / `keyring` |
| Over-scoped OAuth permissions | Unnecessary full account access | Request minimal required scopes (e.g., read-only for Gmail) |

## "Looks Done But Isn't" Checklist

- [ ] **Secret Scanner:** Often missing synthetic test fixtures — verify true positive and true negative tests pass with synthetic data.
- [ ] **Briefing Generator:** Often missing webhook / wake-triggered aggregation — verify all 3 paths call the single shared function.
- [ ] **Calendar Write:** Often missing confirmation step — verify mutation fails without `/calendar/confirm`.

---
*Pitfalls research for: Evie Personal Security & AI Assistant*
*Researched: 2026-09-19*
