#!/bin/bash
# Scratch app used ONLY by the Evie `activation_boundary_check` diagnostic (docs/07 Section 36).
# A regular NSApplication with no windows that exits by itself after 90 s. Ad-hoc signed; not Evie.
#   packaging/macos/diagnostics/build_scratch_activation_target.sh
# Installs to ~/Library/Caches/com.evie-assistant.evie/activation-boundary/EvieScratchTarget.app (the fixed path
# the diagnostic checks). It is launched by the live test infrastructure with `open -g -F`, never by Evie.
set -euo pipefail
DIR="${HOME}/Library/Caches/com.evie-assistant.evie/activation-boundary"
APP="${DIR}/EvieScratchTarget.app"
SRC="$(mktemp -d)/target.swift"
cat > "$SRC" <<'SWIFT'
import AppKit
let app = NSApplication.shared
app.setActivationPolicy(.regular)
DispatchQueue.main.asyncAfter(deadline: .now() + 90) { exit(0) }
app.run()
SWIFT
rm -rf "$APP" && mkdir -p "$APP/Contents/MacOS"
cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleIdentifier</key><string>com.evie-assistant.scratch.activation-target</string>
  <key>CFBundleExecutable</key><string>EvieScratchTarget</string>
  <key>CFBundlePackageType</key><string>APPL</string>
</dict></plist>
PLIST
swiftc -O -o "$APP/Contents/MacOS/EvieScratchTarget" "$SRC"
codesign --force --sign - "$APP"
echo "BUILT $APP"
