"""
Speaker Verification & Enrollment tests (02_TRD.md Section 3, 04_Backend_Schema.md Sections 4-5,
06_Acceptance_Criteria.md Sections 7, 8, 11).

Fast and mocked: a fake embedding backend stands in for SpeechBrain, the reference file lives
in a temporary directory, and no test downloads a model or touches ~/.evie.
"""

import base64
import contextlib
import io
import json
import logging
import sqlite3
import sys
import threading
import types
import wave

import numpy as np
import pytest
from fastapi.testclient import TestClient

import main
from main import app, init_db
from services import llm_client, speaker_verification as sv
from services.github_intelligence import init_github_tables
from services.gmail_intelligence import init_gmail_tables

client = TestClient(app)

VEC_A = np.ones(sv.EMBEDDING_DIM, dtype=np.float32)                       # enrolled speaker
VEC_B = np.tile(np.array([1.0, -1.0], dtype=np.float32), sv.EMBEDDING_DIM // 2)  # cosine 0 vs VEC_A


def make_wav_b64(seconds=2.0, speaker="A", rate=16000, channels=1, width=2, silent=False):
    n = int(seconds * rate)
    t = np.arange(n) / rate
    offset = 0.1 if speaker == "A" else -0.1
    signal = np.zeros(n) if silent else 0.3 * np.sin(2 * np.pi * 220 * t) + offset
    pcm = (np.clip(signal, -1, 1) * 32767).astype("<i2")
    if channels == 2:
        pcm = np.repeat(pcm, 2)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(width)
        w.setframerate(rate)
        w.writeframes(pcm.tobytes() if width == 2 else (pcm // 256).astype("i1").tobytes())
    return base64.b64encode(buf.getvalue()).decode()


class FakeBackend:
    """Speaker 'A' audio (positive DC offset) -> VEC_A, speaker 'B' -> VEC_B."""
    def __init__(self):
        self.calls = 0

    def embed(self, samples):
        self.calls += 1
        return VEC_A.copy() if float(np.mean(samples)) > 0 else VEC_B.copy()


@pytest.fixture
def env(tmp_path, monkeypatch):
    db_file = str(tmp_path / "speaker_evie.db")
    init_db(db_file)
    init_github_tables(db_file)
    init_gmail_tables(db_file)
    monkeypatch.setattr(main, "DB_PATH", db_file)
    monkeypatch.setattr(main, "LAST_CHAT_CONTEXT", {"topic": None, "data": None})

    ref_dir = tmp_path / "evie_home"
    monkeypatch.setenv("EVIE_SPEAKER_REFERENCE_PATH", str(ref_dir / "speaker_reference.npy"))
    monkeypatch.setenv("EVIE_SPEAKER_MODEL_DIR", str(tmp_path / "no_model_here"))
    monkeypatch.delenv("EVIE_SPEAKER_THRESHOLD", raising=False)
    monkeypatch.setattr(sv, "_BACKEND", None)

    backend = FakeBackend()
    monkeypatch.setattr(sv, "get_embedding_backend", lambda: backend)

    def planner_unavailable(message, tools, context=""):
        raise llm_client.LLMUnavailable("LLM_REQUEST_FAILED")
    monkeypatch.setattr(llm_client, "plan_tool_call", planner_unavailable)
    return types.SimpleNamespace(db=db_file, ref=ref_dir / "speaker_reference.npy", ref_dir=ref_dir, backend=backend, tmp=tmp_path)


def _plan(monkeypatch, tool_name, tool_input):
    monkeypatch.setattr(
        llm_client, "plan_tool_call",
        lambda message, tools, context="": llm_client.PlannerDecision(tool_name=tool_name, tool_input=tool_input, text=None),
    )


def _enroll(speaker="A", replace=False):
    return client.post("/voice/enroll", json={"audio_wav_base64": make_wav_b64(speaker=speaker), "replace_existing": replace})


def _insert_secret(db_file):
    with sqlite3.connect(db_file) as conn:
        cur = conn.execute(
            "INSERT INTO secret_findings (repo_name, commit_sha, file_path, pattern_type, redacted_preview, status) "
            "VALUES ('org/api', 'abc1234', '.env', 'AWS Access Key ID', 'AK...LE', 'open')"
        )
        conn.commit()
        return cur.lastrowid


def _count(db_file, table):
    with sqlite3.connect(db_file) as conn:
        try:
            return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        except sqlite3.OperationalError:
            return 0


def _db_dump(db_file):
    with sqlite3.connect(db_file) as conn:
        return "\n".join(conn.iterdump())


# --- Enrollment ---

def test_enroll_writes_single_private_local_file(env):
    before = _db_dump(env.db)
    res = _enroll()
    assert res.status_code == 200
    assert res.json() == {"enrolled": True, "replaced": False}

    assert sorted(p.name for p in env.ref_dir.iterdir()) == ["speaker_reference.npy"]
    assert (env.ref.stat().st_mode & 0o777) == 0o600
    assert (env.ref_dir.stat().st_mode & 0o777) == 0o700
    assert np.array_equal(np.load(env.ref, allow_pickle=False), VEC_A)
    assert _db_dump(env.db) == before  # nothing written to SQLite


def test_reenrollment_requires_confirmation_and_replaces_cleanly(env):
    assert _enroll("A").status_code == 200

    res = _enroll("B")
    assert res.status_code == 409 and res.json()["detail"] == "REENROLLMENT_CONFIRMATION_REQUIRED"
    assert np.array_equal(np.load(env.ref, allow_pickle=False), VEC_A)

    res = _enroll("B", replace=True)
    assert res.status_code == 200 and res.json() == {"enrolled": True, "replaced": True}
    assert np.array_equal(np.load(env.ref, allow_pickle=False), VEC_B)
    assert sorted(p.name for p in env.ref_dir.iterdir()) == ["speaker_reference.npy"]  # no orphaned files


def test_concurrent_enrollments_leave_a_valid_reference(env):
    errors = []

    def worker(speaker):
        try:
            sv.enroll(make_wav_b64(speaker=speaker), replace_existing=True)
        except Exception as e:  # pragma: no cover - surfaced below
            errors.append(e)

    threads = [threading.Thread(target=worker, args=("A" if i % 2 else "B",)) for i in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors
    loaded = sv.load_reference()
    assert np.array_equal(loaded, VEC_A) or np.array_equal(loaded, VEC_B)
    assert sorted(p.name for p in env.ref_dir.iterdir()) == ["speaker_reference.npy"]


def test_enrollment_rejects_bad_samples_without_writing(env):
    assert client.post("/voice/enroll", json={}).json()["detail"] == "NO_VOICE_SAMPLE"
    res = client.post("/voice/enroll", json={"audio_wav_base64": make_wav_b64(seconds=0.5)})
    assert res.status_code == 400 and res.json()["detail"] == "INVALID_AUDIO"
    assert not env.ref.exists()


def test_enrollment_status_endpoint(env):
    assert client.get("/voice/enrollment").json() == {"enrolled": False, "verification_available": False}
    _enroll()
    assert client.get("/voice/enrollment").json()["enrolled"] is True


# --- Sensitive voice gating ---

def test_verified_voice_creates_verified_proposal_and_click_confirm_executes(env, monkeypatch):
    _enroll("A")
    finding_id = _insert_secret(env.db)
    _plan(monkeypatch, "resolve_finding", {"finding_type": "secret", "id": finding_id})

    res = client.post("/voice/command", json={"transcript": "resolve that leaked key", "audio_wav_base64": make_wav_b64(speaker="A")}).json()
    assert res["tool"] == {"name": "resolve_finding", "planner": "llm", "status": "proposed"}
    assert res["tool_proposal"]["speaker_verified"] is True

    confirmed = client.post("/tools/confirm", json={"proposal_token": res["tool_proposal"]["proposal_token"], "confirmed": True}).json()
    assert confirmed["status"] == "executed"
    with sqlite3.connect(env.db) as conn:
        assert conn.execute("SELECT status FROM secret_findings WHERE id = ?", (finding_id,)).fetchone()[0] == "resolved"


def test_verified_voice_reaches_existing_calendar_proposal_flow(env):
    _enroll("A")
    res = client.post("/voice/command", json={"transcript": "Propose event Security Review tomorrow", "audio_wav_base64": make_wav_b64(speaker="A")}).json()
    assert res["tool"]["status"] == "proposed"
    assert "proposal" in res
    assert _count(env.db, "calendar_actions") == 1


def test_wrong_speaker_is_blocked(env, monkeypatch):
    _enroll("A")
    finding_id = _insert_secret(env.db)
    _plan(monkeypatch, "resolve_finding", {"finding_type": "secret", "id": finding_id})

    res = client.post("/voice/command", json={"transcript": "resolve it", "audio_wav_base64": make_wav_b64(speaker="B")}).json()
    assert res["tool"]["status"] == "blocked" and res["tool"]["reason"] == "SPEAKER_MISMATCH"
    assert "tool_proposal" not in res
    assert _count(env.db, "tool_action_proposals") == 0


def test_not_enrolled_is_blocked(env):
    res = client.post("/voice/command", json={"transcript": "Propose event Security Review tomorrow", "audio_wav_base64": make_wav_b64()}).json()
    assert res["tool"]["reason"] == "NOT_ENROLLED"
    assert _count(env.db, "calendar_actions") == 0


@pytest.mark.parametrize("corruption", ["garbage", "wrong_shape", "nan", "zeros", "pickle"])
def test_invalid_reference_fails_closed(env, corruption):
    env.ref_dir.mkdir(parents=True)
    if corruption == "garbage":
        env.ref.write_bytes(b"not a numpy file")
    elif corruption == "wrong_shape":
        np.save(env.ref, np.ones(10, dtype=np.float32))
    elif corruption == "nan":
        np.save(env.ref, np.full(sv.EMBEDDING_DIM, np.nan, dtype=np.float32))
    elif corruption == "zeros":
        np.save(env.ref, np.zeros(sv.EMBEDDING_DIM, dtype=np.float32))
    else:
        np.save(env.ref, np.array([{"x": 1}], dtype=object), allow_pickle=True)

    assert sv.verify(make_wav_b64()) == sv.SpeakerCheck(False, "REFERENCE_INVALID")
    res = client.post("/voice/command", json={"transcript": "Propose event Review tomorrow", "audio_wav_base64": make_wav_b64()}).json()
    assert res["tool"]["status"] == "blocked"


@pytest.mark.parametrize("audio,reason", [
    (None, "NO_VOICE_SAMPLE"),
    ("", "NO_VOICE_SAMPLE"),
    ("!!!not-base64!!!", "INVALID_AUDIO"),
    (base64.b64encode(b"RIFF....not really a wav").decode(), "INVALID_AUDIO"),
    ("stereo", "INVALID_AUDIO"),
    ("44k", "INVALID_AUDIO"),
    ("8bit", "INVALID_AUDIO"),
    ("short", "INVALID_AUDIO"),
    ("long", "INVALID_AUDIO"),
    ("silent", "INVALID_AUDIO"),
    ("oversize", "INVALID_AUDIO"),
])
def test_invalid_or_missing_audio_fails_closed(env, audio, reason):
    _enroll("A")
    samples = {
        "stereo": lambda: make_wav_b64(channels=2),
        "44k": lambda: make_wav_b64(rate=44100),
        "8bit": lambda: make_wav_b64(width=1),
        "short": lambda: make_wav_b64(seconds=0.5),
        "long": lambda: make_wav_b64(seconds=16),
        "silent": lambda: make_wav_b64(silent=True),
        "oversize": lambda: "A" * (sv.MAX_AUDIO_BASE64_CHARS + 4),
    }
    audio = samples[audio]() if audio in samples else audio
    assert sv.verify(audio) == sv.SpeakerCheck(False, reason)


def test_backend_unavailable_fails_closed(env, monkeypatch):
    _enroll("A")
    monkeypatch.setattr(sv, "get_embedding_backend", lambda: sv.SpeechBrainBackend(env.tmp / "no_model_here"))
    assert sv.verify(make_wav_b64()) == sv.SpeakerCheck(False, "VERIFICATION_UNAVAILABLE")
    res = client.post("/voice/enroll", json={"audio_wav_base64": make_wav_b64(), "replace_existing": True})
    assert res.status_code == 503 and res.json()["detail"] == "VERIFICATION_UNAVAILABLE"


def test_threshold_is_configurable_and_fails_closed_when_invalid(env, monkeypatch):
    assert sv.SPEECHBRAIN_REFERENCE_THRESHOLD == 0.25
    assert sv.get_threshold() == 0.25

    e1 = np.zeros(sv.EMBEDDING_DIM, dtype=np.float32)
    e1[0] = 1.0
    env.ref_dir.mkdir(parents=True)
    np.save(env.ref, e1)

    def backend_with_cosine(c):
        vec = np.zeros(sv.EMBEDDING_DIM, dtype=np.float32)
        vec[0], vec[1] = c, np.sqrt(1 - c * c)
        return types.SimpleNamespace(embed=lambda samples: vec)

    monkeypatch.setenv("EVIE_SPEAKER_THRESHOLD", "0.5")
    monkeypatch.setattr(sv, "get_embedding_backend", lambda: backend_with_cosine(0.49))
    assert sv.verify(make_wav_b64()).reason == "SPEAKER_MISMATCH"
    monkeypatch.setattr(sv, "get_embedding_backend", lambda: backend_with_cosine(0.51))
    assert sv.verify(make_wav_b64()).verified is True

    for bad in ("abc", "0", "1", "-0.2", "1.5"):
        monkeypatch.setenv("EVIE_SPEAKER_THRESHOLD", bad)
        assert sv.verify(make_wav_b64()) == sv.SpeakerCheck(False, "VERIFICATION_MISCONFIGURED")


def test_read_only_voice_and_chat_never_invoke_verification(env, monkeypatch):
    calls = []
    monkeypatch.setattr(sv, "verify", lambda audio: calls.append(audio) or sv.SpeakerCheck(False, "SPEAKER_MISMATCH"))

    voice = client.post("/voice/command", json={"transcript": "What is my security score?"}).json()
    assert voice["tool"] == {"name": "get_security_score", "planner": "keyword", "status": "executed"}
    chat = client.post("/chat", json={"message": "Propose event Security Review tomorrow"}).json()
    assert chat["tool"]["status"] == "proposed"
    assert calls == []
    assert env.backend.calls == 0


def test_unverified_voice_proposal_is_rejected_at_confirm(env):
    from services import tool_router
    finding_id = _insert_secret(env.db)
    tool_router.init_tool_proposals_table(env.db)
    with sqlite3.connect(env.db) as conn:
        conn.execute(
            "INSERT INTO tool_action_proposals (proposal_token, tool_name, arguments, channel, status, created_at, expires_at, speaker_verified) "
            "VALUES ('tok', 'resolve_finding', ?, 'voice', 'proposed', '2026-01-01T00:00:00+00:00', '2999-01-01T00:00:00+00:00', 0)",
            (json.dumps({"finding_type": "secret", "id": finding_id, "status": "resolved"}),),
        )
        conn.commit()
    res = client.post("/tools/confirm", json={"proposal_token": "tok", "confirmed": True}).json()
    assert res["status"] == "rejected"
    with sqlite3.connect(env.db) as conn:
        assert conn.execute("SELECT status FROM secret_findings WHERE id = ?", (finding_id,)).fetchone()[0] == "open"


def test_proposals_table_migrates_existing_rows(tmp_path):
    from services import tool_router
    db_file = str(tmp_path / "old.db")
    with sqlite3.connect(db_file) as conn:
        conn.execute(
            "CREATE TABLE tool_action_proposals (proposal_token TEXT PRIMARY KEY, tool_name TEXT NOT NULL, arguments TEXT NOT NULL, "
            "channel TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'proposed', created_at TIMESTAMP NOT NULL, expires_at TIMESTAMP NOT NULL, executed_at TIMESTAMP)"
        )
        conn.execute("INSERT INTO tool_action_proposals VALUES ('t', 'resolve_finding', '{}', 'voice', 'proposed', 'x', 'y', NULL)")
        conn.commit()
    tool_router.init_tool_proposals_table(db_file)
    with sqlite3.connect(db_file) as conn:
        assert conn.execute("SELECT speaker_verified FROM tool_action_proposals").fetchone()[0] == 0


# --- voice_command_log ---

def test_voice_command_log_records_every_outcome(env, monkeypatch):
    _enroll("A")
    finding_id = _insert_secret(env.db)
    token = "ghp_" + "b" * 36

    client.post("/voice/command", json={"transcript": f"What is my security score? my token is {token}"})
    _plan(monkeypatch, "resolve_finding", {"finding_type": "secret", "id": finding_id})
    client.post("/voice/command", json={"transcript": "resolve it", "audio_wav_base64": make_wav_b64(speaker="B")})
    client.post("/voice/command", json={"transcript": "resolve it", "audio_wav_base64": make_wav_b64(speaker="A")})
    _plan(monkeypatch, "launch_missiles", {})
    client.post("/voice/command", json={"transcript": "do something undefined"})

    with sqlite3.connect(env.db) as conn:
        conn.row_factory = sqlite3.Row
        rows = [dict(r) for r in conn.execute("SELECT * FROM voice_command_log ORDER BY id")]
    assert len(rows) == 4

    read_only, mismatch, verified, unknown = rows
    assert read_only["resolved_tool"] == "get_security_score" and read_only["executed"] == 1
    assert read_only["speaker_verified"] is None and read_only["rejected_reason"] is None
    assert token not in read_only["raw_transcript"] and "[REDACTED]" in read_only["raw_transcript"]

    assert mismatch["resolved_tool"] == "resolve_finding" and mismatch["speaker_verified"] == 0
    assert mismatch["executed"] == 0 and mismatch["rejected_reason"] == "SPEAKER_MISMATCH"
    assert json.loads(mismatch["resolved_parameters"]) == {"finding_type": "secret", "id": finding_id, "status": "resolved"}

    assert verified["speaker_verified"] == 1 and verified["executed"] == 0 and verified["rejected_reason"] is None

    assert unknown["resolved_tool"] == "launch_missiles" and unknown["rejected_reason"] == "TOOL_NOT_REGISTERED"
    assert unknown["resolved_parameters"] is None


# --- Privacy ---

def test_no_audio_or_embedding_leakage(env, monkeypatch, caplog):
    audio_a = make_wav_b64(speaker="A")
    audio_b = make_wav_b64(speaker="B")
    finding_id = _insert_secret(env.db)
    _plan(monkeypatch, "resolve_finding", {"finding_type": "secret", "id": finding_id})

    with caplog.at_level(logging.DEBUG):
        responses = [
            client.post("/voice/enroll", json={"audio_wav_base64": audio_a}),
            client.get("/voice/enrollment"),
            client.post("/voice/command", json={"transcript": "resolve it", "audio_wav_base64": audio_a}),
            client.post("/voice/command", json={"transcript": "resolve it", "audio_wav_base64": audio_b}),
            client.post("/voice/enroll", json={"audio_wav_base64": "A" * (sv.MAX_AUDIO_BASE64_CHARS + 4)}),
        ]

    dump = _db_dump(env.db)
    embedding_text = repr(VEC_A.tolist())[:40]
    for blob in [r.text for r in responses] + [caplog.text, dump]:
        assert audio_a[100:200] not in blob and audio_b[100:200] not in blob
        assert embedding_text not in blob
        assert "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA" not in blob

    written = sorted(p.relative_to(env.tmp).as_posix() for p in env.tmp.rglob("*") if p.is_file())
    assert written == ["evie_home/speaker_reference.npy", "speaker_evie.db"]


# --- SpeechBrain integration shape (no real model, no network) ---

def test_speechbrain_backend_loads_pinned_model_locally_only(env, monkeypatch):
    calls = []

    class FakeTensor:
        def __init__(self, arr):
            self.arr = arr

        def unsqueeze(self, dim):
            return FakeTensor(np.expand_dims(self.arr, dim))

        def squeeze(self):
            return FakeTensor(np.squeeze(self.arr))

        def detach(self):
            return self

        def cpu(self):
            return self

        def numpy(self):
            return self.arr

    class FakeClassifier:
        def encode_batch(self, signal):
            assert signal.arr.shape[0] == 1
            return FakeTensor(np.ones((1, 1, sv.EMBEDDING_DIM), dtype=np.float32))

    class FakeEncoderClassifier:
        @classmethod
        def from_hparams(cls, **kwargs):
            calls.append(kwargs)
            return None if kwargs.get("download_only") else FakeClassifier()

    class FakeFetchConfig:
        def __init__(self, allow_network=True, revision=None):
            self.allow_network, self.revision = allow_network, revision

    fake_torch = types.SimpleNamespace(no_grad=contextlib.nullcontext, from_numpy=FakeTensor)
    modules = {
        "speechbrain": types.ModuleType("speechbrain"),
        "speechbrain.inference": types.ModuleType("speechbrain.inference"),
        "speechbrain.inference.speaker": types.SimpleNamespace(EncoderClassifier=FakeEncoderClassifier),
        "speechbrain.utils": types.ModuleType("speechbrain.utils"),
        "speechbrain.utils.fetching": types.SimpleNamespace(
            FetchConfig=FakeFetchConfig, LocalStrategy=types.SimpleNamespace(NO_LINK="NO_LINK", COPY="COPY")),
        "torch": fake_torch,
    }
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)

    model = env.tmp / "model"
    model.mkdir()
    for name in sv.MODEL_FILES:
        (model / name).write_text("x")

    vec = sv.SpeechBrainBackend(model).embed(np.zeros(16000, dtype=np.float32))
    assert vec.shape == (sv.EMBEDDING_DIM,)
    load = calls[-1]
    assert load["source"] == str(model) and load["savedir"] == str(model)
    assert "overrides" not in load
    assert load["fetch_config"].allow_network is False
    assert load["local_strategy"] == "NO_LINK"

    monkeypatch.setenv("EVIE_SPEAKER_MODEL_DIR", str(env.tmp / "download_target"))
    sv.download_model()
    download = calls[-1]
    assert download["source"] == sv.MODEL_ID == "speechbrain/spkrec-ecapa-voxceleb"
    assert download["fetch_config"].revision == sv.MODEL_REVISION == "0f99f2d0ebe89ac095bcc5903c4dd8f72b367286"
    assert download["download_only"] is True


def test_missing_model_files_never_trigger_a_download(env, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("model load attempted without local files")
    monkeypatch.setitem(sys.modules, "speechbrain.inference.speaker", types.SimpleNamespace(EncoderClassifier=types.SimpleNamespace(from_hparams=forbidden)))
    with pytest.raises(sv.SpeakerVerificationError, match="VERIFICATION_UNAVAILABLE"):
        sv.SpeechBrainBackend(env.tmp / "empty").embed(np.zeros(16000, dtype=np.float32))


# --- Frontend ---

def test_frontend_captures_local_sample_and_offers_enrollment():
    capture = client.get("/static/voice_capture.js").text
    assert "class VoiceSampleRecorder" in capture and "getUserMedia" in capture
    assert "fetch(" not in capture  # the recorder never uploads anything itself

    js = client.get("/static/app.js").text
    assert "audio_wav_base64" in js and "/voice/enroll" in js and "replace_existing" in js
    assert "/voice/enrollment" in js

    html = client.get("/").text
    assert "/static/voice_capture.js" in html and "enrollVoiceBtn" in html

    voice_js = client.get("/static/voice.js").text
    assert "audio_wav_base64" not in voice_js and "fetch(" not in voice_js
