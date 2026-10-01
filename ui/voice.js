/**
 * Browser Voice Interface Adapter for Evie Assistant.
 * 
 * Implements UI-02:
 * Encapsulates browser Web Speech API (STT & TTS) under a clean VoiceAdapter interface.
 * Designed with a pluggable architecture so future hardware/Raspberry Pi audio drivers (Phase 9)
 * can implement an identical interface without altering UI application code.
 */

class VoiceAdapter {
  constructor(options = {}) {
    this.lang = options.lang || 'en-US';
    this.continuous = options.continuous || false;
    this.interimResults = options.interimResults || false;
    
    // Check SpeechRecognition browser support
    const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (SpeechRecognition) {
      this.recognition = new SpeechRecognition();
      this.recognition.lang = this.lang;
      this.recognition.continuous = this.continuous;
      this.recognition.interimResults = this.interimResults;
    } else {
      this.recognition = null;
    }

    // Check SpeechSynthesis browser support
    this.synthesis = window.speechSynthesis || null;
    this.isListening = false;
    this._restartTimer = null;
    this._restartCount = 0;
    this._lastRestartTime = 0;
  }

  isSTTSupported() {
    return !!this.recognition;
  }

  isTTSSupported() {
    return !!this.synthesis;
  }

  startListening(onTranscript, onError) {
    if (!this.recognition) {
      if (onError) onError('SpeechRecognition is not supported in this browser.');
      return;
    }

    if (this._restartTimer) {
      clearTimeout(this._restartTimer);
      this._restartTimer = null;
    }

    this.recognition.continuous = this.continuous;
    this.recognition.interimResults = this.interimResults;
    this.isListening = true;
    this._restartCount = 0;
    this._lastRestartTime = Date.now();

    this.recognition.onresult = (event) => {
      this._restartCount = 0; // Successful result resets error retry counter
      const lastIndex = event.resultIndex !== undefined ? event.resultIndex : event.results.length - 1;
      const result = event.results[lastIndex];
      const isFinal = result ? (result.isFinal !== undefined ? result.isFinal : true) : true;
      const transcript = (result && result[0] ? result[0].transcript : '').trim();
      if (transcript && onTranscript) {
        onTranscript(transcript, isFinal, lastIndex);
      }
    };

    this.recognition.onerror = (event) => {
      if (event.error === 'no-speech') return; // ignore silence timeouts in continuous mode
      if (event.error === 'aborted' && !this.isListening) return; // intentional stop
      if (event.error === 'not-allowed' || event.error === 'service-not-allowed') {
        this.isListening = false;
      }
      if (onError) onError(event.error);
    };

    this.recognition.onend = () => {
      if (!this.isListening) return;

      const now = Date.now();
      if (now - this._lastRestartTime < 10000) {
        this._restartCount++;
        if (this._restartCount > 5) {
          this.isListening = false;
          if (onError) onError('SpeechRecognition stopped after repeated restart attempts.');
          return;
        }
      } else {
        this._restartCount = 1;
        this._lastRestartTime = now;
      }

      // Asynchronous non-blocking restart prevents Chrome InvalidStateError race condition
      this._restartTimer = setTimeout(() => {
        this._restartTimer = null;
        if (!this.isListening) return;
        try {
          this.recognition.start();
        } catch (e) {
          if (e.name === 'InvalidStateError') {
            // Already active or starting
            return;
          }
          this.isListening = false;
          if (onError) onError(e.message || 'SpeechRecognition restart failed');
        }
      }, 150);
    };

    try {
      this.recognition.start();
    } catch (e) {
      if (e.name !== 'InvalidStateError') {
        this.isListening = false;
        if (onError) onError(e.message);
      }
    }
  }

  stopListening() {
    this.isListening = false;
    if (this._restartTimer) {
      clearTimeout(this._restartTimer);
      this._restartTimer = null;
    }
    if (this.recognition) {
      try {
        this.recognition.stop();
      } catch (e) {
        // ignore
      }
    }
  }

  speak(text, onEnd) {
    if (!this.synthesis || !text) {
      if (onEnd) onEnd();
      return;
    }

    // Cancel any ongoing speech
    this.synthesis.cancel();

    // Clean Markdown tags before speaking
    const cleanText = text.replace(/[*_#`~[\]()]/g, '').trim();
    const utterance = new SpeechSynthesisUtterance(cleanText);
    utterance.lang = this.lang;

    if (onEnd) {
      utterance.onend = () => onEnd();
      utterance.onerror = () => onEnd();
    }

    this.synthesis.speak(utterance);
  }
}

// Global instance export
window.voiceAdapter = new VoiceAdapter();
