# Phase 6 Context: Hardware & Voice Integration ("Taz")

## Goals
Implement local wake-word audio detection, speech-to-text (STT) and text-to-speech (TTS) audio processing pipeline, and Raspberry Pi hardware display controller integration (`HW-01`, `HW-02`).

## Requirements
- **`HW-01`**: Physical Raspberry Pi device integration with microphone, speaker, and small reactive display controller.
- **`HW-02`**: Local wake-word detection, STT/TTS pipeline, and seamless audio reasoning connection to Evie FastAPI backend.

## Design Decisions
1. **Audio Pipeline (`voice_pipeline.py`):**
   - Wake-word detection engine with fallback audio capture simulation for headless/test environments.
   - STT audio transcription wrapper converting recorded speech to text query.
   - TTS speech synthesis engine converting assistant responses into audio output.
   - Integration with Evie endpoints (`/briefing/wake`, `/drs/ask`, `/calendar/propose`).
2. **Display Controller (`hardware_display.py`):**
   - State machine rendering assistant status (`IDLE`, `LISTENING`, `THINKING`, `SPEAKING`).
   - Mock/GPIO display interface supporting physical hardware & headless CLI simulation modes.

## Scope Fences
- Voice processing runs locally or calls cost-efficient cloud STT/TTS models (< $20/mo cost envelope).
- All voice-triggered calendar mutations still require explicit confirmation flow (`/calendar/propose` -> `/calendar/confirm`).
- Zero secret leakage in voice logs or audio transcript buffers.
