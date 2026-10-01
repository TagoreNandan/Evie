// Evie identity launcher (docs/07_Computer_Control.md Section 34; CC-1a Step 13).
//
// The ONLY job of Evie.app: give the computer-control worker a real, signed macOS identity. Launched by
// LaunchServices, this process is its own responsible process, so everything it starts is attributed to
// Evie (its Accessibility grant), not to an IDE or terminal.
//
// It runs one code-owned program, chosen from a CLOSED set of modes (CC-1a Step 14A):
//     (no argument) | identity_check  ->  evie-python -s -B -m services.computer_control.identity_check
//     live_harness                    ->  evie-python -s -B -m services.computer_control.live_harness
//     live_validation_14b             ->  evie-python -s -B -m services.cc_live_validation.step14b
//     live_validation_14b_final       ->  evie-python -s -B -m services.cc_live_validation.step14b_final
//     activation_boundary_check       ->  evie-python -s -B -m services.cc_live_validation.activation_boundary
//     keyboard_event_preflight        ->  evie-python -s -B -m services.cc_live_validation.keyboard_preflight
//     keyboard_probe_15b              ->  evie-python -s -B -m services.cc_live_validation.keyboard_probe
//     press_key_15c                   ->  evie-python -s -B -m services.cc_live_validation.press_key_15c
//     cc1a_benchmark                  ->  evie-python -s -B -m services.cc_live_validation.cc1a_benchmark
//     two_paragraph_1b1               ->  evie-python -s -B -m services.cc_live_validation.two_paragraph_1b1
// with cwd = Contents/Resources/python (the worker code, sealed by the bundle signature). Any other argument,
// or more than one, is refused - the argument is a mode NAME, never a command, path or module. No shell.
// The environment is explicit (nothing inherited). The interpreter home comes from Info.plist
// (EviePythonHome), which the signature also seals.
// It contains no Accessibility, UI or computer-control logic.
//
// Step 14B launcher fix: the launcher is a real accessory NSApplication (no Dock icon, no menu bar, no
// windows) running the AppKit main loop. As a Foundation-only agent it was dropped from LaunchServices when
// its child (the worker) activated another app (docs/07 Section 36); an NSApplication agent was not.

import AppKit
import Foundation

let expectedBundleID = "com.evie-assistant.evie"
let modes: [String: String] = [
    "identity_check": "services.computer_control.identity_check",
    "voice_automation_service": "services.computer_control.voice_automation_service",
    "live_harness": "services.computer_control.live_harness",
    "live_validation_14b": "services.cc_live_validation.step14b",
    "live_validation_14b_final": "services.cc_live_validation.step14b_final",
    "activation_boundary_check": "services.cc_live_validation.activation_boundary",
    "keyboard_event_preflight": "services.cc_live_validation.keyboard_preflight",
    "keyboard_probe_15b": "services.cc_live_validation.keyboard_probe",
    "press_key_15c": "services.cc_live_validation.press_key_15c",
    "cc1a_benchmark": "services.cc_live_validation.cc1a_benchmark",
    "two_paragraph_1b1": "services.cc_live_validation.two_paragraph_1b1",
]

func fail(_ message: String) -> Never {
    FileHandle.standardError.write(("EVIE_LAUNCHER_ERROR " + message + "\n").data(using: .utf8)!)
    exit(78)   // EX_CONFIG
}

let requested = Array(CommandLine.arguments.dropFirst())
let mode: String
if requested.isEmpty {
    mode = "identity_check"
} else if requested.count == 1, modes[requested[0]] != nil {
    mode = requested[0]
} else {
    fail("unsupported launch arguments (allowed modes: \(modes.keys.sorted()))")
}
let program = ["-s", "-B", "-m", modes[mode]!]

let bundle = Bundle.main
guard bundle.bundleIdentifier == expectedBundleID else { fail("unexpected bundle identifier") }
guard let pythonHome = bundle.object(forInfoDictionaryKey: "EviePythonHome") as? String,
      pythonHome.hasPrefix("/") else { fail("EviePythonHome missing") }

let contents = bundle.bundleURL.appendingPathComponent("Contents")
let python = contents.appendingPathComponent("MacOS/evie-python")
let code = contents.appendingPathComponent("Resources/python")

let child = Process()
child.executableURL = python
child.arguments = program
child.currentDirectoryURL = code
child.environment = [
    "PYTHONHOME": pythonHome,
    "PYTHONDONTWRITEBYTECODE": "1",
    "PYTHONNOUSERSITE": "1",
    "PATH": "/usr/bin:/bin",
    "HOME": NSHomeDirectory(),
    "LANG": "en_US.UTF-8",
]

// Propagate termination to the child, then exit with its status.
var signalSources: [DispatchSourceSignal] = []
for sig in [SIGTERM, SIGINT, SIGHUP] {
    signal(sig, SIG_IGN)
    let source = DispatchSource.makeSignalSource(signal: sig, queue: .main)
    source.setEventHandler { if child.isRunning { child.terminate() } }
    source.resume()
    signalSources.append(source)
}

child.terminationHandler = { p in exit(p.terminationStatus) }
NSApplication.shared.setActivationPolicy(.accessory)          // registered before the child starts (as validated)
do { try child.run() } catch { fail("could not start evie-python: \(error)") }
NSApplication.shared.run()
