# Phase 7 Validation Plan: Evie Web UI & Voice Interface

## Verification Strategy
Verify local static UI asset delivery, endpoint API connectivity, calendar proposal modal safety flow, tone application, voice adapter interface abstraction, and automated pytest coverage.

## Test Cases

### 1. Static Asset Serving (`GET /`)
- Verify `GET /` returns HTML application index with 200 OK.
- Verify static JavaScript and CSS bundles load correctly.

### 2. Conversational API Integration Flow
- Verify UI calls `/briefing/on-demand`, `/drs/ask`, `/journal/search`, `/score`, `/findings`, `/integrations/sync` cleanly.

### 3. Calendar Confirmation Protocol
- Verify `/calendar/propose` returns proposal card data to UI.
- Verify explicit user action triggers `/calendar/confirm` with `confirmed=True` or `confirmed=False`.

### 4. Pluggable Voice Interface Abstraction
- Verify `VoiceAdapter` interface handles STT transcript callbacks and TTS audio speech generation.

## Verification Command
```bash
python3 -m pytest tests/test_ui_voice.py
```
