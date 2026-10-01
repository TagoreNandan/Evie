# Evie — UI/UX Design Document
*Revision 2 — dashboard-first, structured rendering, voice as incident-response tool*

## 1. Core Principle: Dashboard First, Chat/Voice Second
Opening Evie must immediately answer "what's happening right now" — the user should never have to ask a question to learn their current state. Chat and voice exist for exploration and specific requests ("what did I work on yesterday," "revoke that key"), not as the primary way to understand system state. This reverses the original chat-first framing from Revision 1 and matches what's actually been built.

## 2. Dashboard Layout

### 2.1 Top-level state indicator
One immediately-visible overall status: **Good / Needs Attention / Critical** — computed from the canonical security score (TRD Section 4). This is the single most important pixel on the page.

### 2.2 Security section
- Current security score (canonical, single source)
- Active findings (secrets, breaches, exposures, missing 2FA), each as a distinct card, not a flat list
- A microphone/voice-command button lives here specifically, since voice is scoped to security incident response — not floating globally as a generic assistant icon

### 2.3 GitHub section
- Repository health cards (good / needs-attention / critical indicator per repo)
- Recent activity (commits, PRs) as a compact timeline, not raw JSON-like text
- Security findings surfaced per-repository, linked to the security section

### 2.4 Gmail section
- Counts by category: important, transactional, promotional, phishing-alert
- Notable/flagged messages surfaced as cards with sender, subject, category, and a clear "heuristic, not proof" label on phishing alerts
- Cleanup proposals (promotional senders) shown with a Confirm action, never auto-executed

## 3. Chat — Secondary Interface, Structured Output
Chat is for specific natural-language questions ("what changed today," "why is my score low," "show me this week's commits"). Output must render as structured UI, not raw text blocks:

| Content type | Rendering |
|---|---|
| Activity summary | Short natural-language line + a compact table (repo / changes / time) |
| Alerts | Status badge (🔴/🟡/🟢) + short description, expandable for detail |
| Time-based queries | An actual event timeline for the resolved time window, not an aggregate count |
| Security findings | Finding cards with a resolve/false-positive action |

The model generates the explanation text; the UI layer renders the structured payload alongside it. Avoid long bullet-list text dumps — this was an explicitly identified problem in the current build.

## 4. Voice Control — Incident Response, Not General Chat
- Entry point: a dedicated push-to-talk control in the Security section of the dashboard (see 2.2), not an always-listening ambient mode.
- Primary framing: acting on security findings by voice — "revoke that leaked key," "resolve this finding," "enable 2FA on my GitHub."
- Secondary: a small allow-list of general commands (open specific apps) for demo impact.
- **First-use enrollment flow:** before any sensitive voice action is available, the user is asked to record a short reference voice sample (a few seconds), used only to generate a local speaker-verification embedding. Stored locally, never transmitted, clearly explained in-UI why this step exists.
- **Sensitive action flow:** user speaks command → transcribed → tool-calling model determines intent and proposes the action → **if flagged sensitive, speaker verification runs before showing the confirmation** → user confirms (voice or click) → action executes → spoken + visual confirmation.
- **Non-sensitive actions** (e.g., "open Safari") skip speaker verification and confirmation, executing directly for demo fluidity.

### 4.1 Computer control session (PLANNED — `07_Computer_Control.md`)
- A computer-control request starts a session. The UI shows a session card with:
  - its state (observing, acting, asking, done, blocked, stopped);
  - each step's outcome in plain language, e.g. "Switched to TextEdit", "Couldn't verify that click", or "Blocked: sending messages isn't allowed yet";
  - a permanently visible **Stop** control.
- **Stop** cancels the session before its next action.
- An **ASK** result is shown as a question, never as a silent guess.
- "Done" is shown as confirmed only when it was verified; otherwise it's shown as "probably done, not verified".
- If Accessibility permission is missing, the card explains which app needs it. Evie never triggers the system prompt silently.

## 5. Taz (Pi Zero) — Physical Embodiment
Taz mirrors the voice-control experience above on dedicated hardware (square display, mic, speaker) rather than introducing new interaction patterns:

| State | Trigger | Visual |
|---|---|---|
| Idle | No active interaction | Calm neutral animation |
| Listening | Push-to-talk equivalent (physical button or wake phrase) | Pulse/brighten |
| Thinking | Awaiting tool-calling response | Simple processing animation |
| Alert | A security finding was just flagged | Distinct concerned expression |
| Verifying | Speaker verification in progress for a sensitive command | Distinct "checking" animation — this state should be visibly different from "thinking," since it's a security-relevant moment worth surfacing to the user |
| Confirmed/positive | Action completed successfully (e.g. key revoked, score improved) | Happy/reassuring expression |

## 6. Accessibility & Plain-Language Requirement (ties to Multiculturalism/Social Consciousness competency work)
- No jargon in user-facing alert text — never "entropy score" or "canonical security score" in copy the user reads; plain language only ("this looks like a leaked password").
- Voice control is documented as a genuine accessibility feature (hands-free computer control), not solely a demo gimmick — this is the honest, specific answer to accessibility questions raised in review, stronger than a generic "we considered rural users" gesture.

## 7. Explicit Non-Requirements (unchanged)
- No multi-user accounts/login screen.
- No mobile app in current phase.
- DRS and personality/tone UI surfaces are deferred — do not build UI for these now even though backend modules exist.

## 8. Known UI Issues / Follow-ups
- **Conversational assistant panel scrolling/overflow (open).** At normal browser zoom the chat panel does not scroll/contain its content correctly. Recorded during Protected Area #3 validation. It did not prevent voice enrollment, verification, or click confirmation from completing. It is to be fixed as a separate UI task, not as part of speaker verification.
