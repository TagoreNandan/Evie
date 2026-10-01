"""
LIVE real-chooser benchmark (CC-1a Step 9): NVIDIA Nemotron 3 Super through the Step 8 fixed suite.

    EVIE_RUN_LIVE_CHOOSER_BENCHMARK=1 python tests/cc_benchmark_live.py [--repeats 3] [--out report.json]

LIVE BENCHMARK ONLY - NO COMPUTER EXECUTION. The provider only produces decisions; each one goes through
the production validator and is compared with the fixed expected results. Nothing imports the worker,
executor, session or any macOS API. Without the opt-in variable, no request is made. The API key is read
from the OS keyring by the adapter and never printed or saved.
"""

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import cc_benchmark as B  # noqa: E402
from services.computer_control.providers import nvidia  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--out", type=str, default=None, help="optional JSON report path (contains no credentials)")
    args = parser.parse_args()

    print(f"Provider: {nvidia.PROVIDER}\nModel: {nvidia.MODEL_ID}\nMode: LIVE BENCHMARK ONLY - NO COMPUTER EXECUTION")
    if os.environ.get(nvidia.LIVE_OPT_IN) != "1":
        print(f"Not run: set {nvidia.LIVE_OPT_IN}=1 to allow live provider requests (none were made).")
        return 0
    if not nvidia._keyring_key():
        print(f"Not run: keyring {nvidia.KEYRING_SERVICE}/{nvidia.KEYRING_KEY} is not set. No results were produced.")
        return 2

    provider = nvidia.NvidiaChooserProvider()
    config = provider.describe()
    configuration = {k: config[k] for k in ("temperature", "max_tokens", "enable_thinking", "response_format",
                                            "max_transport_retries", "min_interval_s")}
    total = len(B.UTTERANCE_CASES)
    done = []

    def progress(case_id):
        done.append(case_id)
        print(f"  [{len(done)}/{total}] {case_id}", file=sys.stderr, flush=True)

    baseline = B.run_benchmark("deterministic-fast-path", B.FastPathChooserProvider())
    result = B.run_benchmark(provider.name, provider, repeats=args.repeats, progress=progress)
    meta = {"provider": nvidia.PROVIDER, "model": nvidia.MODEL_ID, "configuration": configuration,
            "cost": "unavailable (no verified pricing for this endpoint in the project)"}
    print(B.format_real_report(result, meta, baseline))
    failures = B.failure_table(result)
    print(f"\nNon-correct attempts: {len(failures)}")
    for row in failures:
        print(json.dumps(row, default=str))
    if args.out:
        Path(args.out).write_text(json.dumps({"meta": meta, "result": result.model_dump(mode="json"),
                                              "failures": failures}, indent=1, default=str))
        print(f"\nSaved: {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
