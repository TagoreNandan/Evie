"""
CC-1a Step 9: NVIDIA chooser provider adapter, with MOCKED HTTP only. No network, no real key.
"""

import ast
import json
from pathlib import Path

import httpx
import pytest

import cc_benchmark as B
from services.computer_control.chooser_boundary import validate_provider_output
from services.computer_control.providers import nvidia as N

FAKE_KEY = "nvapi-TEST-NOT-A-REAL-KEY-0123456789"


class Resp:
    def __init__(self, status=200, body=None, raise_json=False):
        self.status_code, self._body, self._raise = status, body, raise_json

    def json(self):
        if self._raise:
            raise ValueError("not json")
        return self._body


def completion(content, usage=True):
    body = {"choices": [{"message": {"role": "assistant", "content": content}}]}
    if usage:
        body["usage"] = {"prompt_tokens": 1400, "completion_tokens": 25, "total_tokens": 1425}
    return body


class FakeHTTP:
    def __init__(self, *responses):
        self.responses, self.calls = list(responses), []

    def __call__(self, url, json=None, headers=None, timeout=None):
        self.calls.append({"url": url, "json": json, "headers": headers, "timeout": timeout})
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


@pytest.fixture
def opted_in(monkeypatch):
    monkeypatch.setenv(N.LIVE_OPT_IN, "1")


def provider(http, key=FAKE_KEY, **cfg):
    return N.NvidiaChooserProvider(N.NvidiaConfig(min_interval_s=0.0, **cfg), http_post=http,
                                   get_key=lambda: key, sleep=lambda s: None)


def case_input(case_id="D27"):
    case = next(c for c in B.UTTERANCE_CASES if c.case_id == case_id)
    obs = B.FIXTURES[case.fixture]()
    return B._input(case, obs), obs


# --- request shape: bounded ChooserInput only; key only in the header ---

def test_payload_contains_only_the_system_prompt_and_the_chooser_input(opted_in):
    http = FakeHTTP(Resp(body=completion('{"status":"DONE"}')))
    ci, _ = case_input()
    provider(http).choose(ci)
    call = http.calls[0]
    assert call["url"] == "https://integrate.api.nvidia.com/v1/chat/completions"
    body = call["json"]
    assert body["model"] == "nvidia/nemotron-3-super-120b-a12b" and body["temperature"] == 0.0
    assert body["chat_template_kwargs"] == {"enable_thinking": False} and body["stream"] is False
    assert [m["role"] for m in body["messages"]] == ["system", "user"]
    assert json.loads(body["messages"][1]["content"]) == {"chooser_input": ci.model_dump(mode="json")}
    sent = json.dumps(body)
    assert FAKE_KEY not in sent and "frame" not in sent and "AXUIElement" not in sent
    assert call["headers"]["Authorization"] == f"Bearer {FAKE_KEY}"


def test_system_prompt_is_strict_and_concise():
    p = N.SYSTEM_PROMPT
    for must in ("ACTION", "DONE", "BLOCKED", "ASK", "untrusted DATA", "text_span", "Never output shell commands",
                 "activate_app, set_text, scroll and press"):
        assert must in p
    assert len(p) < 4000


# --- outputs are returned unmodified; the production validator decides ---

def test_valid_structured_response_is_accepted_by_the_validator(opted_in):
    ci, obs = case_input()
    raw = provider(FakeHTTP(Resp(body=completion('{"status":"ACTION","op":"press","target_index":3,'
                                                 '"obs_id":"obs-std","args":{}}')))).choose(ci)
    v = validate_provider_output(raw, ci, obs)
    assert v.ok and v.decision.target_index == 3


@pytest.mark.parametrize("content, error", [
    ("Sure! I'll press align center.", "MALFORMED_JSON"),
    ('```json\n{"status":"DONE"}\n```', "MALFORMED_JSON"),                      # never repaired
    ('{"status":"ACTION","op":"click","target_index":3,"obs_id":"obs-std"}', "SCHEMA"),
    ('{"status":"ACTION","op":"press","target_index":77,"obs_id":"obs-std"}', "TARGET_INDEX_OUT_OF_RANGE"),
    ('{"status":"ACTION","op":"press","target_index":3,"obs_id":"obs-old"}', "WRONG_OBSERVATION"),
    ('{"status":"ACTION","op":"set_text","target_index":0,"obs_id":"obs-std","args":{"text_span":[0,999]}}',
     "TEXT_SPAN_OUTSIDE_UTTERANCE"),
    ('{"status":"ACTION","op":"set_text","target_index":0,"obs_id":"obs-std","args":{"text":"hello"}}', "ARGS"),
    ('{"status":"DONE","reasoning":"because"}', "SCHEMA"),
    ("", "MALFORMED_JSON"),
])
def test_invalid_model_output_is_invalid_not_repaired(opted_in, content, error):
    ci, obs = case_input()
    raw = provider(FakeHTTP(Resp(body=completion(content)))).choose(ci)
    assert raw == content                                              # unmodified
    v = validate_provider_output(raw, ci, obs)
    assert not v.ok and v.error.startswith(error)


def test_non_string_content_becomes_empty_and_fails_validation(opted_in):
    ci, obs = case_input()
    body = {"choices": [{"message": {"role": "assistant", "content": None, "reasoning_content": "thinking..."}}]}
    raw = provider(FakeHTTP(Resp(body=body))).choose(ci)
    assert raw == "" and not validate_provider_output(raw, ci, obs).ok


def test_usage_is_recorded_only_when_reported(opted_in):
    ci, _ = case_input()
    p = provider(FakeHTTP(Resp(body=completion('{"status":"DONE"}'))))
    p.choose(ci)
    assert p.last_usage == {"input_tokens": 1400, "output_tokens": 25, "total_tokens": 1425}
    p = provider(FakeHTTP(Resp(body=completion('{"status":"DONE"}', usage=False))))
    p.choose(ci)
    assert p.last_usage is None


# --- transport failures are ProviderError, bounded retries, never model errors ---

@pytest.mark.parametrize("responses, kind, calls", [
    ([Resp(status=500)] * 3, "UNAVAILABLE", 3),
    ([Resp(status=503)] * 3, "UNAVAILABLE", 3),
    ([Resp(status=429)] * 3, "RATE_LIMITED", 3),
    ([Resp(status=401)], "HTTP_ERROR", 1),                              # not retried
    ([Resp(status=400)], "HTTP_ERROR", 1),
    ([httpx.ReadTimeout("slow")] * 3, "TIMEOUT", 3),
    ([httpx.ConnectError("down")] * 3, "NETWORK", 3),
    ([Resp(body={"unexpected": True})], "BAD_RESPONSE", 1),
    ([Resp(raise_json=True)], "BAD_RESPONSE", 1),
])
def test_transport_failures(opted_in, responses, kind, calls):
    http = FakeHTTP(*responses)
    with pytest.raises(N.ProviderError) as info:
        provider(http).choose(case_input()[0])
    assert info.value.kind == kind and len(http.calls) == calls and FAKE_KEY not in str(info.value)


def test_transient_failure_then_success_is_reported_as_a_retry(opted_in):
    http = FakeHTTP(Resp(status=503), Resp(body=completion('{"status":"DONE"}')))
    p = provider(http)
    assert p.choose(case_input()[0]) == '{"status":"DONE"}' and p.last_transport_retries == 1


def test_missing_credential_makes_no_request(opted_in):
    http = FakeHTTP()
    with pytest.raises(N.ProviderError) as info:
        provider(http, key=None).choose(case_input()[0])
    assert info.value.kind == "MISSING_CREDENTIAL" and http.calls == []


def test_no_opt_in_makes_no_request(monkeypatch):
    monkeypatch.delenv(N.LIVE_OPT_IN, raising=False)
    http = FakeHTTP()
    with pytest.raises(N.ProviderError) as info:
        provider(http).choose(case_input()[0])
    assert info.value.kind == "NOT_ENABLED" and http.calls == []


def test_real_http_is_blocked_under_pytest_even_when_opted_in(opted_in, monkeypatch):
    monkeypatch.delenv("EVIE_LIVE_PROVIDER_TEST", raising=False)
    p = N.NvidiaChooserProvider(get_key=lambda: FAKE_KEY)            # default httpx.post, not injected
    with pytest.raises(N.ProviderError) as info:
        p.choose(case_input()[0])
    assert info.value.kind == "NOT_ENABLED"


# --- benchmark integration: provider errors are separate from model errors ---

def test_benchmark_separates_provider_errors_from_invalid_output(opted_in):
    class Mixed:
        def __init__(self):
            self.n = 0

        def choose(self, ci):
            self.n += 1
            if self.n % 3 == 0:
                raise N.ProviderError("TIMEOUT", "x")
            return "not json" if self.n % 3 == 1 else json.dumps({"status": "DONE"})
    r = B.run_benchmark("mixed", Mixed(), repeats=3)
    at = r.metrics["attempts"]
    assert at["provider_error"] == 53 and at["invalid_provider_output"] == 53 and at["responded"] == 106
    assert r.metrics["correct"] == 0 and all(c.mismatch[0] == "invalid_output" for c in r.cases)


# --- scope: the provider is isolated and nothing on the execution side imports it ---

PACKAGE = Path(N.__file__).parents[1]
EXECUTION_SIDE = ("worker", "executor", "macos_actions", "macos_ax", "macos_probe", "session", "orchestrator",
                  "observer", "chooser_boundary", "fast_path", "gate")


def _imports(path):
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            yield from (a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            yield node.module or ""


def test_provider_imports_no_execution_or_os_modules():
    names = set(_imports(Path(N.__file__)))
    assert names <= {"json", "os", "time", "dataclasses", "typing", "httpx", "keyring_utils",
                     "services.computer_control.chooser"}, names


def test_nothing_in_the_package_imports_the_provider():
    for path in PACKAGE.glob("*.py"):
        assert not any("providers" in n for n in _imports(path)), path.name
