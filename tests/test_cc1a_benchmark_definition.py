"""The benchmark harness runs exactly the Section 18 task list of docs/07 (same 13 tasks, same pass criteria)."""

import re
from pathlib import Path

from services.cc_live_validation import cc1a_benchmark as B


def test_harness_tasks_match_section_18_verbatim():
    doc = (Path(__file__).resolve().parents[1] / "docs/07_Computer_Control.md").read_text()
    section = doc[doc.index("## 18. CC-1a benchmark"):doc.index("## 19.")]
    rows = re.findall(r"^\| (\d+) \| (.+?) \| (.+?) \|$", section, re.M)
    assert [(int(n), t, c) for n, t, c in rows] == B.TASKS
    assert len(B.TASKS) == 13 and all(hasattr(B.Benchmark, f"t{n}") for n, _, _ in B.TASKS)


def test_benchmark_is_offline_and_guarded():
    import inspect
    assert 'os.environ["EVIE_LLM_PLANNER"] = "off"' in inspect.getsource(B.main)      # set in the Evie run only
    assert B.ALLOWED_ACTIVATION == {"com.apple.TextEdit", "com.google.Chrome", "com.apple.finder"}
