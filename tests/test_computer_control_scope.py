"""
Scope guard for the computer-control package (CC-1a Steps 1-7).

- Pure core modules (incl. observer.py): no AppKit/PyObjC, OS, subprocess, network, LLM or dynamic execution.
- PyObjC/AppKit appear ONLY in macos_probe.py / macos_ax.py (run inside the worker process), lazily,
  and every AX function they reference is on a read-only allowlist - no action API exists.
- subprocess appears ONLY in worker.py: one Popen with a fixed argv, shell=False.
- No module reaches the router, FastAPI app, LLM client, os_adapter or voice pipeline.
"""

import ast
from pathlib import Path

import services.computer_control as cc

PACKAGE = Path(cc.__file__).parent
PURE = {"__init__", "models", "actions", "provenance", "resolver", "gate", "chooser", "fast_path",
        "verification", "session", "runtime", "observer", "frontmost", "executor", "orchestrator",
        "chooser_boundary", "hardening", "audit", "planner", "goal_orchestrator", "voice_adapter",
        "chat_adapter", "hardware_adapter", "container", "classifier", "context", "response_policy", "goal_memory", "assistant_context", "assistant_intelligence"}

PYOBJC_MODULES = {"macos_probe", "macos_ax"}
MUTATING = "macos_actions"            # the ONLY module allowed to mutate UI (inherits PyObjC access from macos_ax)
BOUNDARY = PYOBJC_MODULES | {"worker", MUTATING, "macos_signature", "identity_check", "live_harness"}
SERVICE_BOUNDARY = {"voice_automation_service", "voice_automation_client", "llm_planner", "visual_observer"}
ALL_MODULES = PURE | BOUNDARY | SERVICE_BOUNDARY

# The only AX functions the package may reference: reads, queries, element creation, client timeout.
AX_READ_ALLOWLIST = {
    "AXUIElementCopyAttributeValue", "AXUIElementCopyAttributeValues", "AXUIElementGetAttributeValueCount",
    "AXUIElementCopyAttributeNames", "AXUIElementCopyActionNames", "AXUIElementIsAttributeSettable",
    "AXUIElementSetMessagingTimeout", "AXUIElementCreateApplication", "AXUIElementCreateSystemWide",
    "AXUIElementGetPid", "AXUIElementGetTypeID", "AXValueGetTypeID", "AXValueGetType", "AXValueGetValue",
    "AXIsProcessTrustedWithOptions", "AXUIElementCopyParameterizedAttributeValue", "AXValueCreate",
}
# The complete CC-1a live mutation surface: (function, pinned attribute/action) - exactly these seven calls.
ALLOWED_MUTATIONS = sorted([
    ("AXUIElementPerformAction", "AXRaise"),
    ("AXUIElementPerformAction", "AXPress"),
    ("AXUIElementPerformAction", "AXPress"),
    ("AXUIElementSetAttributeValue", "AXMain"),
    ("AXUIElementSetAttributeValue", "AXFocused"),
    ("AXUIElementSetAttributeValue", "AXSelectedText"),
    ("AXUIElementSetAttributeValue", "AXSelectedTextRange"),          # select_text (select all)
    ("AXUIElementSetAttributeValue", "AXValue"),
    ("AXUIElementSetAttributeValue", "AXValue"),
    ("AXUIElementSetAttributeValue", "AXSelected"),
    ("AXUIElementPerformAction", "AXCancel"),
])
MUTATION_FUNCTIONS = {"AXUIElementSetAttributeValue", "AXUIElementPerformAction"}
FORBIDDEN_EVERYWHERE = ("PostKeyboardEvent", "CGEventPost(", "CGEventTap", "CGEventSource",
                        "CGEventKeyboardSetUnicodeString", "CGEventCreateMouseEvent", "CGEventCreateScrollWheelEvent",
                        "CGWarpMouse", "openApplication", "launchApplication",
                        "launchedApplication", "openURL", "openFile", "NSTask", "forceTerminate", "terminateApp",
                        "unhide", "setFrontmost", "activateFromApplication", "yieldActivation", "activateIgnoring",
                        "NSAppleScript", "osascript", "AXUIElementSetAttribute(")
# The ONE approved activation mechanism (validated in Phase 0b): pinned call and options.
ACTIVATION_CALL = "activateWithOptions_"
ACTIVATION_ARGS = "self.appkit.NSApplicationActivateAllWindows | self.appkit.NSApplicationActivateIgnoringOtherApps"
ALLOWED_ACTIVATE_NAMES = {ACTIVATION_CALL, "activate_running_app"}

PYOBJC = {"AppKit", "Foundation", "objc", "Quartz", "ApplicationServices", "CoreFoundation", "HIServices", "Cocoa"}
NETWORK_LLM = {"socket", "httpx", "requests", "urllib", "http", "keyring", "keyring_utils"}
EVIE_EXTERNAL = {"fastapi", "main", "services.llm_client", "services.os_adapter", "services.tool_router",
                 "services.tool_registry", "services.voice_pipeline", "services.speaker_verification",
                 "services.conversational_router"}
PURE_FORBIDDEN = PYOBJC | NETWORK_LLM | EVIE_EXTERNAL | {
    "subprocess", "asyncio", "threading", "multiprocessing", "os", "shutil", "ctypes", "select", "sys"}
BOUNDARY_FORBIDDEN = NETWORK_LLM | EVIE_EXTERNAL | {"asyncio", "threading", "multiprocessing", "shutil"}
FORBIDDEN_BUILTINS = {"eval", "exec", "compile", "__import__"}
FORBIDDEN_METHODS = {"system", "popen", "spawn", "run", "call", "check_call", "check_output", "execv", "execvp"}


def _tree(stem):
    path = PACKAGE / f"{stem}.py"
    return ast.parse(path.read_text(), filename=str(path))


def _imports(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            yield from (a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            yield node.module or ""


def _hit(name, forbidden):
    return name in forbidden or name.split(".")[0] in forbidden


def test_package_modules_are_classified():
    assert {p.stem for p in PACKAGE.glob("*.py")} == ALL_MODULES


def test_pure_modules_have_no_platform_or_io_imports():
    for stem in PURE:
        for name in _imports(_tree(stem)):
            assert not _hit(name, PURE_FORBIDDEN), f"{stem}.py imports {name}"


def test_pyobjc_only_in_macos_modules_and_only_lazily():
    for stem in PURE | {"worker"}:
        assert not any(_hit(n, PYOBJC) for n in _imports(_tree(stem))), f"{stem}.py imports PyObjC"
    for stem in PYOBJC_MODULES:
        tree = _tree(stem)
        top_level = [n for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
        for node in top_level:
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module]
            assert not any(_hit(n, PYOBJC) for n in names), f"{stem}.py must import PyObjC lazily"
        assert any(_hit(n, PYOBJC) for n in _imports(tree))


def test_only_allowlisted_ax_functions_are_referenced():
    for stem in ALL_MODULES:
        for node in ast.walk(_tree(stem)):
            name = node.attr if isinstance(node, ast.Attribute) else node.id if isinstance(node, ast.Name) else None
            if name and name.startswith("AX") and name[2:3].isupper() and name not in {"AXBackend", "AXErrorRecord"}:
                allowed = AX_READ_ALLOWLIST | (MUTATION_FUNCTIONS if stem == MUTATING else set())
                assert name in allowed, f"{stem}.py references {name}"


def test_live_mutation_surface_is_exactly_the_validated_seven_calls():
    found = []
    for node in ast.walk(_tree(MUTATING)):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in MUTATION_FUNCTIONS:
            arg = node.args[1] if len(node.args) > 1 else None
            assert isinstance(arg, ast.Constant) and isinstance(arg.value, str), "attribute/action must be a literal"
            found.append((node.func.attr, arg.value))
    assert set(found) == set(ALLOWED_MUTATIONS), "expanding the live action surface needs an intentional test update"


def test_pasteboard_is_read_only_and_lives_only_in_the_two_metadata_reads():
    """Clipboard (final completion task): the worker READS the general pasteboard's change count and, for
    verification, its plain text (concealed/transient items are never read). It never writes the pasteboard."""
    for stem in ALL_MODULES:
        source = (PACKAGE / f"{stem}.py").read_text()
        if stem != MUTATING:
            assert "NSPasteboard" not in source, f"{stem}.py touches the pasteboard"
    tree = _tree(MUTATING)
    users = sorted(f.name for f in ast.walk(tree) if isinstance(f, ast.FunctionDef) and "NSPasteboard" in ast.unparse(f))
    assert users == ["pasteboard_change_count", "pasteboard_text"]
    source = (PACKAGE / f"{MUTATING}.py").read_text()
    for write in ("setString_", "setData_", "clearContents", "writeObjects_", "setPropertyList_", "declareTypes_"):
        assert write not in source


def test_no_forbidden_mechanisms_anywhere():
    for stem in ALL_MODULES:
        source = (PACKAGE / f"{stem}.py").read_text()
        for fragment in FORBIDDEN_EVERYWHERE:
            assert fragment not in source, f"{stem}.py mentions {fragment}"
        if stem != MUTATING:
            assert "PerformAction" not in source and "SetAttributeValue" not in source, f"{stem}.py mutates"
    for stem in PYOBJC_MODULES | {"observer", "executor", MUTATING}:   # worker.py may stop ITS OWN process
        source = (PACKAGE / f"{stem}.py").read_text()
        assert "kill(" not in source and "os.kill" not in source


def test_boundary_modules_have_no_network_llm_or_router():
    for stem in BOUNDARY:
        for name in _imports(_tree(stem)):
            assert not _hit(name, BOUNDARY_FORBIDDEN), f"{stem}.py imports {name}"
    for stem in SERVICE_BOUNDARY:
        for name in _imports(_tree(stem)):
            assert not _hit(name, EVIE_EXTERNAL), f"{stem}.py imports {name}"


def test_no_dynamic_execution_or_shell_anywhere():
    for stem in ALL_MODULES:
        for node in ast.walk(_tree(stem)):
            if isinstance(node, ast.Call):
                fn = node.func
                if isinstance(fn, ast.Name):
                    assert fn.id not in FORBIDDEN_BUILTINS, f"{stem}.py calls {fn.id}"
                if isinstance(fn, ast.Attribute):
                    assert fn.attr not in FORBIDDEN_METHODS, f"{stem}.py calls .{fn.attr}"
                for kw in node.keywords:
                    if kw.arg == "shell":
                        assert isinstance(kw.value, ast.Constant) and kw.value.value is False, f"{stem}.py shell="


def test_subprocess_only_launches_the_worker_itself():
    assert "subprocess" not in set(_imports(_tree("macos_probe")))
    popens = [n for n in ast.walk(_tree("worker")) if isinstance(n, ast.Call)
              and isinstance(n.func, ast.Attribute) and n.func.attr == "Popen"]
    assert len(popens) == 1
    argv = popens[0].args[0]
    assert isinstance(argv, ast.List) and ast.unparse(argv) == "[sys.executable, '-m', WORKER_MODULE]"


def test_worker_protocol_is_closed():
    source = (PACKAGE / "worker.py").read_text()
    assert "_TO_WORKER = TypeAdapter(Union[ProbeCommand, InstalledAppsCommand, ObserveCommand, ActCommand, StopCommand])" in source


def test_activation_is_exactly_the_validated_call_in_the_action_module():
    sites = []
    for stem in PURE | BOUNDARY:
        for node in ast.walk(_tree(stem)):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                    and node.func.attr.lower().startswith("activate"):
                assert node.func.attr in ALLOWED_ACTIVATE_NAMES, f"{stem}.py calls {node.func.attr}"
                if node.func.attr == ACTIVATION_CALL:
                    sites.append((stem, ast.unparse(node.args[0]) if node.args else None))
    assert sites == [(MUTATING, ACTIVATION_ARGS)], sites


def test_activation_cannot_start_a_process():
    source = (PACKAGE / f"{MUTATING}.py").read_text()
    body = source[source.index("def activate_running_app"):]
    body = body[:body.index("\n\n", body.index("return bool"))]
    assert "runningApplicationWithProcessIdentifier_" in source and "self._running(pid)" in body
    assert "if app is None:\n            return False" in body          # no running process -> nothing happens


def test_orchestrator_is_pure_sequencing_with_injected_ports():
    names = set(_imports(_tree("orchestrator")))
    assert not any(n.endswith(("worker", "macos_ax", "macos_actions", "macos_probe", "executor")) for n in names), names
    source = (PACKAGE / "orchestrator.py").read_text()
    assert "self._act(" in source and source.count("self._act(") == 1      # exactly one execution call site


def test_chooser_side_cannot_reach_execution():
    """chooser / fast_path / chooser_boundary produce data only: no worker, executor, session, AX or IO."""
    reachable = {"worker", "executor", "macos_ax", "macos_actions", "macos_probe", "session", "orchestrator", "observer"}
    for stem in ("chooser", "fast_path", "chooser_boundary", "hardening"):
        names = set(_imports(_tree(stem)))
        hits = {n for n in names if n.split(".")[-1] in reachable}
        # chooser_boundary may import the observation *models* only, never the observer/session/executor
        assert not hits, f"{stem}.py imports {hits}"


def test_hardening_is_a_separate_pure_layer_not_the_gate():
    """Step 10: hardening may only use the chooser contract, the fixed grammar and the models - never the gate,
    resolver, session, observer, executor, worker, macOS, network or an LLM provider."""
    names = set(_imports(_tree("hardening")))
    assert names <= {"typing", "pydantic", "services.computer_control.chooser",
                     "services.computer_control.fast_path", "services.computer_control.models"}, names
    assert "hardening" not in {n.split(".")[-1] for n in _imports(_tree("gate"))}


# --- Step 15C: the ONE pinned keyboard mechanism (replaces the former blanket "CGEvent" ban) ---

ARROW_KEYCODES_EXPECTED = {"RIGHT_ARROW": 124, "LEFT_ARROW": 123}
PINNED_KEYBOARD_CALLS = {"CGEventCreateKeyboardEvent", "CGEventPostToPid", "CGEventSetFlags"}


def _cg_names(tree):
    for node in ast.walk(tree):
        name = node.attr if isinstance(node, ast.Attribute) else node.id if isinstance(node, ast.Name) else None
        if name and name.startswith("CGEvent"):
            yield node, name


def test_cgevent_appears_only_in_the_pinned_press_key_mechanism():
    for stem in PURE | BOUNDARY:
        found = set(name for _, name in _cg_names(_tree(stem)))
        if stem == MUTATING:
            assert found == PINNED_KEYBOARD_CALLS, f"{stem}.py CGEvent surface changed: {found}"
        else:
            assert not found, f"{stem}.py references {found}"


def test_pinned_keyboard_calls_live_only_in_post_arrow_key_with_the_two_keycodes():
    tree = _tree(MUTATING)
    funcs = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "post_arrow_key"]
    assert len(funcs) == 1
    inside = [name for _, name in _cg_names(funcs[0])]
    assert set(inside) == {"CGEventCreateKeyboardEvent", "CGEventPostToPid"}
    # the keycode comes only from the closed two-entry literal, via ARROW_KEYCODES[key]
    assigns = [n for n in ast.walk(tree) if isinstance(n, ast.Assign)
               and any(isinstance(t, ast.Name) and t.id == "ARROW_KEYCODES" for t in n.targets)]
    assert len(assigns) == 1 and ast.literal_eval(assigns[0].value) == ARROW_KEYCODES_EXPECTED
    src = ast.unparse(funcs[0])
    assert "code = ARROW_KEYCODES[key]" in src
    create = [n for n in ast.walk(funcs[0]) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
              and n.func.attr == "CGEventCreateKeyboardEvent"][0]
    assert ast.unparse(create.args[0]) == "None" and ast.unparse(create.args[1]) == "code" and len(create.args) == 3
    assert "for key_down in (True, False)" in src                               # exactly one down + one up


def test_press_key_is_the_only_new_keyboard_capability():
    from services.computer_control.models import PressKeyArgs
    assert set(PressKeyArgs.model_fields) == {"key"}
    assert set(PressKeyArgs.model_fields["key"].annotation.__args__) == {"RIGHT_ARROW", "LEFT_ARROW"}
