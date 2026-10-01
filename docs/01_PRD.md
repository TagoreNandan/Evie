# Evie — Product Requirements Document (PRD)
*Revision 2 — updated against real implementation status (Antigravity/ChatGPT build), post voice-control decision*

## 1. Vision
Evie is a personal, dashboard-first assistant that continuously monitors a user's GitHub activity, Gmail inbox, and account security, surfaces what matters without being asked, and lets the user act on findings — including by voice — without ever taking silent or irreversible action. Security monitoring is one capability of Evie, not the entire identity of the product.

## 2. Problem Statement
- Developers leak API keys/credentials in code without noticing.
- Personal accounts appear in breaches for months before the owner finds out.
- Files/calendar events get shared more publicly than intended.
- Inboxes accumulate important, promotional, and phishing mail with no automatic triage.
- Acting on any of this (revoking a key, enabling 2FA) still requires manually navigating multiple dashboards.
- There is no single, conversational, personal-scale tool unifying detection *and* response.

## 3. Target User
A single technical individual (the builder). Single-tenant by design for the current build. Multi-user/GitHub App distribution is a documented future direction, not a current requirement (see Section 9).

## 4. Scope Strategy: Full Evie vs. GCSP Submission
One codebase, one running system. The GCSP submission is the security-facing subset of it:
- **GCSP-presented scope (final, locked):** GitHub monitoring, Gmail monitoring, voice-controlled security incident response, speaker verification gate, Taz (Pi Zero) as the physical embodiment of all of the above.
- **Explicitly deferred, not abandoned:** DRS (research/decision support), personality/tone/humor layer, full "Jarvis" conversational mode, Taz-on-Pi-4 upgrade. These exist as foundational code already (DRS module, tone config) but are not part of the current push.
- **Why:** running two systems would double cost and duplicate the memory/intelligence infrastructure both need. The current implementation already reflects this — DRS and tone modules exist in code but are intentionally paused per the team's own status report.

## 5. Final GCSP Feature Set (locked)
| # | Feature | Status |
|---|---|---|
| 1 | GitHub monitoring (secret scanning, activity, PRs, webhooks) | Live, implemented, needs performance/scale work |
| 2 | Gmail monitoring (classification + phishing, Laya-assisted) | Live, classification implemented; Laya integration and phishing/spam separation pending |
| 3 | Voice-controlled security incident response (tool-calling based) | Not yet built — this pass defines it |
| 4 | Speaker verification gate before sensitive voice actions | Not yet built |
| 5 | Taz (Pi Zero) as physical embodiment | Hardware in hand; software integration pending, depends on 3–4 |

## 6. Product Philosophy (carried over from team's own status report — these are now binding product principles)
1. **Dashboard first.** Evie tells the user what matters without requiring a question.
2. **Chat/voice second.** Used for exploration and specific requests, not as the only way to understand system state.
3. **Grounded answers.** Never invent information; always answer from retrieved data.
4. **Incremental processing.** Never rescan unchanged data — process deltas only.
5. **Event-driven monitoring.** React to webhooks/events, not just manual sync.
6. **Safe actions.** All sensitive actions follow Propose → Confirm → Execute. Voice actions additionally require speaker verification before execution.
7. **Local-first.** Credentials and data stay on the user's system wherever practical.
8. **Cost-conscious intelligence.** A small/cheap model determines what data is needed; only that data is retrieved and passed to the model generating the final answer. Never send the whole database as context.
9. **Structured presentation.** UI renders cards/tables/badges, not raw text dumps.

## 7. Non-Goals (unchanged, reconfirmed)
- Public multi-tenant deployment in the current phase.
- Social media integration.
- Any "infallible final answer" framing (applies to DRS, when resumed).
- Any voice-command execution without going through the tool-calling + allow-boundary + speaker-verification pipeline (see TRD Section 5).
- Real-time (webhook) GitHub delivery to a public endpoint — acknowledged as blocked by localhost during development; not the current priority.

## 8. Success Criteria (updated)
- Dashboard answers "what's happening right now" without the user asking a question.
- Security score is canonical — one calculation, consistent across dashboard, chat, and briefing (currently a known bug, see Acceptance Criteria).
- GitHub sync processes deltas, not full rescans, and scales toward hundreds/thousands of repos without a proportional time increase.
- A voice command such as "revoke that leaked key" or "enable 2FA on my GitHub" is understood in free-form phrasing (not fixed phrases), routed through a tool-calling LLM, and only executes after passing the allow-boundary and, for sensitive actions, speaker verification.
- Total recurring cost stays near-zero (small/cheap model calls only; no continuous audio-streaming API).

## 9. Roadmap / Deferred Work (explicitly out of current scope, tracked for later)
- GitHub App distribution model replacing personal access tokens (needed before any real multi-user onboarding; PAT remains fine for personal/dev use).
- Gmail multi-user OAuth verification (Google's sensitive-scope review process) — realistic given the extended April–June submission timeline, but not started yet.
- DRS resumption, personality/tone layer, full conversational "ask anything" mode.
- Taz on Raspberry Pi 4, far-field microphone upgrade.
- **Hands-free computer control (separate capability track).** Hands-free Mac control through natural language, for users who cannot or prefer not to use a keyboard or mouse. It is specified in `07_Computer_Control.md`. Phase CC-1a is a bounded native-macOS foundation: validated mechanisms only, a code-owned safety gate, no destructive actions. It is **not** part of the locked GCSP feature set in Section 5. Whether it is presented as part of GCSP is an open decision.
- True LLM tool-use architecture extended to the full chatbot (currently partially built for voice; chat still uses an intent router — see TRD Section 3).
