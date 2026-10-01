"""
Live read-only observation script for Phase 1 verification.
Observes TextEdit, Photo Booth, and Calculator. No mutations performed.
"""

import subprocess
import time
from services.computer_control.macos_ax import PyObjCAXBackend, observe_request
from services.computer_control.models import SystemObservation


def ensure_running(app_name: str):
    subprocess.run(["open", "-a", app_name], check=True)
    time.sleep(1.0)


def observe_app(bundle_id: str, app_name: str):
    ensure_running(app_name)
    res = observe_request(bundle_id=bundle_id, settle=False)
    print(f"\n==================================================")
    print(f"OBSERVATION FOR: {app_name} ({bundle_id})")
    print(f"Status: {res.status}, Reason: {res.reason}")
    if res.observation:
        obs: SystemObservation = res.observation
        print(f"Obs ID: {obs.obs_id}")
        print(f"App PID: {obs.app.pid}, Name: {obs.app.name}")
        print(f"Windows count: {len(obs.windows)}")
        print(f"Total Semantic Nodes: {len(obs.nodes)}")
        print(f"Targets count: {len(obs.targets)}")

        # Print top 15 nodes summary
        print("\nTop Semantic Nodes Sample:")
        for idx, (node_id, node) in enumerate(list(obs.nodes.items())[:15]):
            parent = f" -> parent: {node.parent_id}" if node.parent_id else ""
            label_str = f" label='{node.label}'" if node.label else ""
            val_str = f" val='{node.safe_value_summary}'" if node.safe_value_summary else ""
            children_str = f" children={len(node.child_ids)}"
            print(f"  [{node.node_id}] role={node.role}{label_str}{val_str}{children_str}{parent}")


if __name__ == "__main__":
    observe_app("com.apple.TextEdit", "TextEdit")
    observe_app("com.apple.PhotoBooth", "Photo Booth")
    observe_app("com.apple.calculator", "Calculator")
