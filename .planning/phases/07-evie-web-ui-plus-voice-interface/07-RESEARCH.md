# Phase 7 Research: Evie Web UI & Voice Interface

## Architectural Blueprint

### 1. Technology Selection
- **Frontend Stack:** Vanilla HTML5, CSS3 (Design Tokens, Glassmorphism, CSS Grid/Flexbox), and ES6+ JavaScript.
- **Backend Serving:** FastAPI `StaticFiles` mounting `ui/` folder at `/` and `/static`.
- **Voice Interface:** Web Speech API (`window.SpeechRecognition` / `window.webkitSpeechRecognition` & `window.speechSynthesis`).

### 2. UI Layout & Component Architecture
- `Header`: Status indicator (API online, sync status, active tone).
- `Main Layout`:
  - `Left Sidebar`: Security Score Gauge (`/score`), Briefing Triggers (`/briefing/wake`, `/briefing/on-demand`), Integration Sync (`/integrations/sync`), Tone Switcher (`/config/tone`).
  - `Center Stage`: Conversational Assistant Chat Window (messages, Markdown rendering, voice mic button, proposal cards).
  - `Right Drawer`: Active Findings & Remediation Guides (`/findings`, `/remediation`), Journal/RAG Recall (`/journal/search`).

### 3. Voice Adapter Interface (`VoiceAdapter`)
```javascript
class VoiceAdapter {
  constructor(options) { ... }
  startListening(onTranscriptCallback) { ... }
  stopListening() { ... }
  speak(text, onEndCallback) { ... }
}
```
Provides a clean abstraction so future Phase 9 hardware/audio streaming drivers can implement `HardwareVoiceAdapter` with identical method signatures.
