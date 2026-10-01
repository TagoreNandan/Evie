# Phase 7 Context: Evie Web UI & Voice Interface

## Goals
Build a unified, local web application user interface for Evie Assistant with integrated browser-based voice input/output capabilities.

## Requirements
- **`UI-01`**: Local web application providing a conversational chat interface alongside real-time security score status, briefing summaries, open security findings, remediation guides, calendar proposal confirmation flows, journal RAG recall, and tone configuration.
- **`UI-02`**: Browser-based speech-to-text (STT) and text-to-speech (TTS) voice interface adapter designed with a pluggable architecture for future hardware/Pi audio drivers.

## Design Decisions & Invariants
1. **Conversational-First Dashboard (`ui/`):**
   - Central conversational assistant chat interface connected to all existing backend endpoints.
   - Rich dark aesthetic (glassmorphism, vibrant accents, smooth CSS micro-animations, Inter/Outfit typography).
   - Sidebar cards for Security Score (0-100), Quick Briefing Triggers, Active Findings, Calendar Proposals, Memory Recall, and Tone Config.
2. **Calendar Safety Protocol:**
   - Visual two-step proposal confirmation card in UI (displays token, event summary, start/end times, and explicit "Confirm" vs "Cancel" action).
   - Zero calendar mutations executed without explicit user confirmation (`/calendar/confirm`).
3. **Pluggable Browser Voice Interface (`ui/voice.js`):**
   - Uses Web Speech API (`SpeechRecognition` / `SpeechSynthesis`) for browser audio.
   - Encapsulated in a `VoiceAdapter` interface class so future hardware (Taz / Phase 9) can substitute hardware STT/TTS without altering UI code.
4. **Backend Integration (`main.py`):**
   - Serves local web application assets via FastAPI `StaticFiles`.
   - Zero rewrite of existing Phase 1–6 backend logic or database schemas.

## Scope Fences
- No general-purpose DRS expansion in this phase.
- No Raspberry Pi, GPIO, wake-word hardware, or Taz implementation.
- No authentication or multi-user changes.
