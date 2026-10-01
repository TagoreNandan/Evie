"""
Local Speaker Verification & Enrollment for Evie (02_TRD.md Section 3, 04_Backend_Schema.md Section 5).

- Model: SpeechBrain ECAPA-TDNN speaker encoder "speechbrain/spkrec-ecapa-voxceleb",
  pinned to Hugging Face revision MODEL_REVISION. 192-dim embeddings, 16 kHz mono input.
- Runtime is fully local: the model is loaded only from a local directory with
  FetchConfig(allow_network=False). Downloading it is a separate, explicit, one-time step:
      python -m services.speaker_verification download-model
- Enrollment stores one reference embedding as a .npy file (never SQLite, never transmitted),
  default ~/.evie/speaker_reference.npy, override with EVIE_SPEAKER_REFERENCE_PATH.
- Raw audio is decoded in memory and discarded; it is never written to disk or logged.
- Every failure path fails closed: verify() returns verified=False with a reason code.

SpeechBrain is an optional dependency (requirements-voice.txt / the "voice" extra). Without it,
verification reports VERIFICATION_UNAVAILABLE and sensitive voice actions stay blocked.
"""

import base64
import binascii
import io
import logging
import os
import sys
import tempfile
import threading
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)

MODEL_ID = "speechbrain/spkrec-ecapa-voxceleb"
MODEL_REVISION = "0f99f2d0ebe89ac095bcc5903c4dd8f72b367286"
# Files as saved by SpeechBrain's Pretrainer into the model directory (each collected as <name>.ckpt).
MODEL_FILES = ("hyperparams.yaml", "embedding_model.ckpt", "mean_var_norm_emb.ckpt", "classifier.ckpt", "label_encoder.ckpt")
EMBEDDING_DIM = 192

# SpeechBrain's published reference decision threshold for this cosine score:
# speechbrain.inference.speaker.SpeakerRecognition.verify_batch(..., threshold=0.25),
# which decides "same speaker" when score > threshold. It is not calibrated for any
# individual user; override with EVIE_SPEAKER_THRESHOLD (raise it to reduce false accepts).
SPEECHBRAIN_REFERENCE_THRESHOLD = 0.25

SAMPLE_RATE = 16000
MIN_SECONDS = 1.0
MAX_SECONDS = 15.0
MAX_AUDIO_BYTES = 1_000_000
MAX_AUDIO_BASE64_CHARS = 4 * ((MAX_AUDIO_BYTES + 2) // 3)
SILENCE_RMS = 1e-4

# Reason codes (returned to the router / logged; never contain audio or embeddings)
VERIFIED = "VERIFIED"
NO_VOICE_SAMPLE = "NO_VOICE_SAMPLE"
INVALID_AUDIO = "INVALID_AUDIO"
NOT_ENROLLED = "NOT_ENROLLED"
REFERENCE_INVALID = "REFERENCE_INVALID"
VERIFICATION_UNAVAILABLE = "VERIFICATION_UNAVAILABLE"
VERIFICATION_MISCONFIGURED = "VERIFICATION_MISCONFIGURED"
VERIFICATION_ERROR = "VERIFICATION_ERROR"
SPEAKER_MISMATCH = "SPEAKER_MISMATCH"


class SpeakerVerificationError(Exception):
    """Carries a safe reason code as its message."""


@dataclass(frozen=True)
class SpeakerCheck:
    verified: bool
    reason: str


# --- Configuration ---

def reference_path() -> Path:
    return Path(os.environ.get("EVIE_SPEAKER_REFERENCE_PATH", "~/.evie/speaker_reference.npy")).expanduser()


def model_dir() -> Path:
    return Path(os.environ.get("EVIE_SPEAKER_MODEL_DIR", "~/.evie/models/spkrec-ecapa-voxceleb")).expanduser()


def get_threshold() -> float:
    raw = os.environ.get("EVIE_SPEAKER_THRESHOLD")
    if raw is None:
        return SPEECHBRAIN_REFERENCE_THRESHOLD
    try:
        value = float(raw)
    except ValueError:
        raise SpeakerVerificationError(VERIFICATION_MISCONFIGURED)
    if not (0.0 < value < 1.0):
        raise SpeakerVerificationError(VERIFICATION_MISCONFIGURED)
    return value


# --- Audio ---

def decode_wav_base64(audio_b64: Optional[str]) -> np.ndarray:
    """
    Decode a base64 16 kHz mono 16-bit PCM WAV (1-15 s, <= ~1 MB) into float32 samples in memory.
    """
    if not audio_b64:
        raise SpeakerVerificationError(NO_VOICE_SAMPLE)
    if not isinstance(audio_b64, str) or len(audio_b64) > MAX_AUDIO_BASE64_CHARS:
        raise SpeakerVerificationError(INVALID_AUDIO)
    try:
        raw = base64.b64decode(audio_b64, validate=True)
    except (binascii.Error, ValueError):
        raise SpeakerVerificationError(INVALID_AUDIO)
    if len(raw) > MAX_AUDIO_BYTES:
        raise SpeakerVerificationError(INVALID_AUDIO)
    try:
        with wave.open(io.BytesIO(raw), "rb") as wav:
            if (wav.getnchannels(), wav.getsampwidth(), wav.getframerate(), wav.getcomptype()) != (1, 2, SAMPLE_RATE, "NONE"):
                raise SpeakerVerificationError(INVALID_AUDIO)
            frames = wav.readframes(wav.getnframes())
    except (wave.Error, EOFError):
        raise SpeakerVerificationError(INVALID_AUDIO)

    samples = np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0
    duration = samples.size / SAMPLE_RATE
    if duration < MIN_SECONDS or duration > MAX_SECONDS:
        raise SpeakerVerificationError(INVALID_AUDIO)
    if float(np.sqrt(np.mean(samples ** 2))) < SILENCE_RMS:
        raise SpeakerVerificationError(INVALID_AUDIO)
    return samples


# --- Embedding backend ---

class SpeechBrainBackend:
    """Loads the pinned ECAPA model from the local model directory only (no network)."""

    def __init__(self, directory: Path):
        self.directory = directory
        self._classifier = None
        self._lock = threading.Lock()

    def _load(self):
        if not all((self.directory / name).is_file() for name in MODEL_FILES):
            raise SpeakerVerificationError(VERIFICATION_UNAVAILABLE)
        try:
            from speechbrain.inference.speaker import EncoderClassifier
            from speechbrain.utils.fetching import FetchConfig, LocalStrategy
        except ImportError:
            raise SpeakerVerificationError(VERIFICATION_UNAVAILABLE)
        # source == savedir: hyperparams.yaml and every collected <name>.ckpt already exist there,
        # so SpeechBrain's fetch returns the local files; allow_network=False forbids any fallback.
        local = str(self.directory)
        return EncoderClassifier.from_hparams(
            source=local,
            savedir=local,
            local_strategy=LocalStrategy.NO_LINK,
            fetch_config=FetchConfig(allow_network=False),
            run_opts={"device": "cpu"},
        )

    def embed(self, samples: np.ndarray) -> np.ndarray:
        with self._lock:
            if self._classifier is None:
                self._classifier = self._load()
            classifier = self._classifier
        import torch
        with torch.no_grad():
            signal = torch.from_numpy(np.ascontiguousarray(samples, dtype=np.float32)).unsqueeze(0)
            embedding = classifier.encode_batch(signal)
        return embedding.squeeze().detach().cpu().numpy().astype(np.float32)


_BACKEND: Optional[SpeechBrainBackend] = None
_BACKEND_LOCK = threading.Lock()


def get_embedding_backend():
    global _BACKEND
    with _BACKEND_LOCK:
        directory = model_dir()
        if _BACKEND is None or _BACKEND.directory != directory:
            _BACKEND = SpeechBrainBackend(directory)
        return _BACKEND


def _validated_embedding(vector) -> np.ndarray:
    if not isinstance(vector, np.ndarray) or vector.shape != (EMBEDDING_DIM,):
        raise SpeakerVerificationError(REFERENCE_INVALID)
    vector = vector.astype(np.float32)
    if not np.all(np.isfinite(vector)) or float(np.linalg.norm(vector)) == 0.0:
        raise SpeakerVerificationError(REFERENCE_INVALID)
    return vector


def _embed(samples: np.ndarray) -> np.ndarray:
    try:
        vector = get_embedding_backend().embed(samples)
    except SpeakerVerificationError:
        raise
    except Exception as e:
        logger.warning("Speaker embedding failed: %s", type(e).__name__)
        raise SpeakerVerificationError(VERIFICATION_UNAVAILABLE)
    try:
        return _validated_embedding(np.asarray(vector))
    except SpeakerVerificationError:
        raise SpeakerVerificationError(VERIFICATION_ERROR)


# --- Reference storage ---

_ENROLL_LOCK = threading.Lock()


def load_reference() -> np.ndarray:
    path = reference_path()
    if not path.is_file():
        raise SpeakerVerificationError(NOT_ENROLLED)
    try:
        vector = np.load(path, allow_pickle=False)
    except Exception:
        raise SpeakerVerificationError(REFERENCE_INVALID)
    return _validated_embedding(vector)


def enrollment_status() -> dict:
    try:
        load_reference()
        enrolled = True
    except SpeakerVerificationError:
        enrolled = False
    import importlib.util
    model_available = (
        importlib.util.find_spec("speechbrain") is not None
        and all((model_dir() / name).is_file() for name in MODEL_FILES)
    )
    return {"enrolled": enrolled, "verification_available": model_available}


def _write_reference(vector: np.ndarray) -> None:
    path = reference_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".speaker_reference.", suffix=".tmp", dir=str(path.parent))
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as f:
            np.save(f, vector, allow_pickle=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
        raise


def enroll(audio_b64: Optional[str], replace_existing: bool = False) -> dict:
    """
    Create (or, with explicit replace_existing, overwrite) the reference embedding.
    Returns only status flags - never the embedding.
    Raises SpeakerVerificationError with REENROLLMENT_CONFIRMATION_REQUIRED, NO_VOICE_SAMPLE,
    INVALID_AUDIO, VERIFICATION_UNAVAILABLE, or VERIFICATION_ERROR.
    """
    with _ENROLL_LOCK:
        already_enrolled = reference_path().is_file()
        if already_enrolled and not replace_existing:
            raise SpeakerVerificationError("REENROLLMENT_CONFIRMATION_REQUIRED")
        samples = decode_wav_base64(audio_b64)
        vector = _embed(samples)
        _write_reference(vector)
    return {"enrolled": True, "replaced": already_enrolled}


# --- Verification ---

def verify(audio_b64: Optional[str]) -> SpeakerCheck:
    """
    Compare the command's voice sample against the enrolled reference.
    Verified only when cosine similarity > threshold; everything else fails closed.
    """
    try:
        if not audio_b64:
            return SpeakerCheck(False, NO_VOICE_SAMPLE)
        threshold = get_threshold()
        reference = load_reference()
        samples = decode_wav_base64(audio_b64)
        candidate = _embed(samples)
        denom = max(float(np.linalg.norm(reference)) * float(np.linalg.norm(candidate)), 1e-6)
        score = float(np.dot(reference, candidate)) / denom
        return SpeakerCheck(True, VERIFIED) if score > threshold else SpeakerCheck(False, SPEAKER_MISMATCH)
    except SpeakerVerificationError as e:
        return SpeakerCheck(False, str(e))
    except Exception as e:
        logger.warning("Speaker verification error: %s", type(e).__name__)
        return SpeakerCheck(False, VERIFICATION_ERROR)


# --- One-time explicit model download ---

def download_model() -> Path:
    """
    Explicit, one-time network step: fetch the pinned model revision into model_dir().
    Never called at request time.
    """
    from speechbrain.inference.speaker import EncoderClassifier
    from speechbrain.utils.fetching import FetchConfig, LocalStrategy
    directory = model_dir()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    EncoderClassifier.from_hparams(
        source=MODEL_ID,
        savedir=str(directory),
        download_only=True,
        local_strategy=LocalStrategy.COPY,
        fetch_config=FetchConfig(revision=MODEL_REVISION),
    )
    return directory


if __name__ == "__main__":
    if sys.argv[1:] == ["download-model"]:
        print(f"Model saved to {download_model()}")
    else:
        print("usage: python -m services.speaker_verification download-model")
        sys.exit(2)
