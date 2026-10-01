/**
 * Local Voice Sample Capture for Speaker Verification.
 *
 * Records the microphone with getUserMedia + Web Audio entirely inside the browser and
 * encodes it as a 16 kHz mono 16-bit WAV (1-15 seconds) in base64. The sample is sent
 * only to the local Evie backend (127.0.0.1) for enrollment or verification, where it is
 * processed in memory and discarded. Nothing here is uploaded anywhere else.
 *
 * Separate from VoiceAdapter (voice.js), which is unchanged and handles speech-to-text.
 */

const VOICE_SAMPLE_RATE = 16000;
const VOICE_MIN_SECONDS = 1;
const VOICE_MAX_SECONDS = 15;

class VoiceSampleRecorder {
  constructor() {
    this._reset();
  }

  _reset() {
    this.stream = null;
    this.ctx = null;
    this.source = null;
    this.node = null;
    this.chunks = [];
    this.captured = 0;
    this.inputRate = 0;
    this.recording = false;
  }

  isSupported() {
    return !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia &&
      (window.AudioContext || window.webkitAudioContext) && window.OfflineAudioContext);
  }

  async start() {
    if (this.recording) return;
    this._reset();
    this.stream = await navigator.mediaDevices.getUserMedia({
      audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true },
    });
    const Ctx = window.AudioContext || window.webkitAudioContext;
    this.ctx = new Ctx();
    this.inputRate = this.ctx.sampleRate;
    this.source = this.ctx.createMediaStreamSource(this.stream);
    this.node = this.ctx.createScriptProcessor(4096, 1, 1);
    const maxSamples = this.inputRate * VOICE_MAX_SECONDS;
    this.node.onaudioprocess = (event) => {
      if (!this.recording || this.captured >= maxSamples) return; // hard cap at 15 s
      const data = event.inputBuffer.getChannelData(0);
      const take = Math.min(data.length, maxSamples - this.captured);
      this.chunks.push(new Float32Array(data.subarray(0, take)));
      this.captured += take;
    };
    this.source.connect(this.node);
    this.node.connect(this.ctx.destination);
    this.recording = true;
  }

  async _release() {
    this.recording = false;
    try { if (this.node) this.node.disconnect(); } catch (e) { /* already disconnected */ }
    try { if (this.source) this.source.disconnect(); } catch (e) { /* already disconnected */ }
    if (this.stream) this.stream.getTracks().forEach(t => t.stop());
    if (this.ctx) { try { await this.ctx.close(); } catch (e) { /* already closed */ } }
  }

  /** Discard anything recorded. */
  async cancel() {
    await this._release();
    this._reset();
  }

  /** Stop and return a base64 WAV, or null if under 1 second or unavailable. */
  async stop() {
    if (!this.recording) return null;
    await this._release();
    const pcm = new Float32Array(this.captured);
    let offset = 0;
    this.chunks.forEach(c => { pcm.set(c, offset); offset += c.length; });
    const inputRate = this.inputRate;
    this._reset();
    if (pcm.length < inputRate * VOICE_MIN_SECONDS) return null;
    const resampled = await resampleTo16k(pcm, inputRate);
    return encodeWavBase64(resampled);
  }
}

async function resampleTo16k(pcm, inputRate) {
  if (inputRate === VOICE_SAMPLE_RATE) return pcm;
  const length = Math.ceil(pcm.length * VOICE_SAMPLE_RATE / inputRate);
  const offline = new OfflineAudioContext(1, length, VOICE_SAMPLE_RATE);
  const buffer = offline.createBuffer(1, pcm.length, inputRate);
  buffer.copyToChannel(pcm, 0);
  const src = offline.createBufferSource();
  src.buffer = buffer;
  src.connect(offline.destination);
  src.start();
  const rendered = await offline.startRendering();
  return rendered.getChannelData(0);
}

function encodeWavBase64(samples) {
  const bytes = new Uint8Array(44 + samples.length * 2);
  const view = new DataView(bytes.buffer);
  const writeStr = (pos, str) => { for (let i = 0; i < str.length; i++) view.setUint8(pos + i, str.charCodeAt(i)); };
  writeStr(0, 'RIFF');
  view.setUint32(4, 36 + samples.length * 2, true);
  writeStr(8, 'WAVE');
  writeStr(12, 'fmt ');
  view.setUint32(16, 16, true);                      // PCM chunk size
  view.setUint16(20, 1, true);                       // PCM format
  view.setUint16(22, 1, true);                       // mono
  view.setUint32(24, VOICE_SAMPLE_RATE, true);
  view.setUint32(28, VOICE_SAMPLE_RATE * 2, true);   // byte rate
  view.setUint16(32, 2, true);                       // block align
  view.setUint16(34, 16, true);                      // bits per sample
  writeStr(36, 'data');
  view.setUint32(40, samples.length * 2, true);
  for (let i = 0; i < samples.length; i++) {
    const s = Math.max(-1, Math.min(1, samples[i]));
    view.setInt16(44 + i * 2, s < 0 ? s * 0x8000 : s * 0x7fff, true);
  }
  let binary = '';
  const step = 0x8000;
  for (let i = 0; i < bytes.length; i += step) {
    binary += String.fromCharCode.apply(null, bytes.subarray(i, i + step));
  }
  return btoa(binary);
}

window.voiceSampleRecorder = new VoiceSampleRecorder();
