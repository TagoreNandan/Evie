"""
LIVE (CC-1a Step 9): the NVIDIA chooser provider on the fixed benchmark. Never runs in the fast suite.
    EVIE_RUN_LIVE_CHOOSER_BENCHMARK=1 python -m pytest tests/test_computer_control_live_chooser.py -m live -s -q
Benchmark only: decisions are validated and compared; nothing is executed. Prefer tests/cc_benchmark_live.py
for the full report.
"""

import os

import pytest

import cc_benchmark as B
from services.computer_control.providers import nvidia as N

pytestmark = pytest.mark.live


def test_live_nvidia_chooser_benchmark(monkeypatch):
    if os.environ.get(N.LIVE_OPT_IN) != "1":
        pytest.skip(f"set {N.LIVE_OPT_IN}=1 to run (no requests made)")
    monkeypatch.setenv("EVIE_LIVE_PROVIDER_TEST", "1")
    result = B.run_benchmark(N.NvidiaChooserProvider().name, N.NvidiaChooserProvider(), repeats=1)
    print(B.format_real_report(result, {"provider": N.PROVIDER, "model": N.MODEL_ID, "configuration": "default",
                                        "cost": "unavailable"}))
    assert result.metrics["unsupported_operations"]["accepted"] == []
    assert result.metrics["safety_boundary_failures"]["unsafe_output_accepted"] == []
