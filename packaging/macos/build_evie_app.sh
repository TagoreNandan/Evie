#!/bin/bash
# Build + sign the minimal Evie identity app (docs/07_Computer_Control.md Section 34; CC-1a Step 13).
#
#   EVIE_SIGNING_IDENTITY=<SHA-1 of a codesigning identity> packaging/macos/build_evie_app.sh
#
# Produces ~/Applications/Evie.app:
#   Contents/Info.plist                  CFBundleIdentifier com.evie-assistant.evie, LSUIElement (no UI)
#   Contents/MacOS/Evie                  the Swift launcher (EvieLauncher.swift) - identity + fixed launch only
#   Contents/MacOS/evie-python           a signed copy of the current interpreter (the worker's executable)
#   Contents/Resources/python/services/  copies of services/computer_control and services/cc_live_validation
#                                        (live-validation infrastructure, Step 14B), sealed by the signature
# Signed with the given identity (no hardened runtime: conda's unsigned extension modules must load).
# Nothing is granted: Accessibility permission must be given by the user in System Settings.
# The identity must be a real Apple-issued certificate; the script refuses ad-hoc signing ("-").
set -euo pipefail

BUNDLE_ID="com.evie-assistant.evie"
WORKER_ID="com.evie-assistant.evie.python"
APP_NAME="Evie"
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
DEST_DIR="${HOME}/Applications"
DEST="${DEST_DIR}/${APP_NAME}.app"
PYTHON="${EVIE_PYTHON:-python}"

IDENTITY="${EVIE_SIGNING_IDENTITY:?set EVIE_SIGNING_IDENTITY to the SHA-1 of a codesigning identity}"
if [[ "$IDENTITY" == "-" ]]; then echo "ad-hoc signing is not an identity; refusing" >&2; exit 2; fi
security find-identity -v -p codesigning | grep -q "$IDENTITY" || { echo "identity not found/valid" >&2; exit 2; }

PY_EXE="$("$PYTHON" -c 'import os, sys; print(os.path.realpath(sys.executable))')"
PY_HOME="$("$PYTHON" -c 'import sys; print(sys.base_prefix)')"

STAGE="$(mktemp -d)/${APP_NAME}.app"
mkdir -p "$STAGE/Contents/MacOS" "$STAGE/Contents/Resources/python/services"

cat > "$STAGE/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleIdentifier</key><string>${BUNDLE_ID}</string>
  <key>CFBundleName</key><string>${APP_NAME}</string>
  <key>CFBundleExecutable</key><string>${APP_NAME}</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>0.1.0</string>
  <key>CFBundleVersion</key><string>1</string>
  <key>CFBundleInfoDictionaryVersion</key><string>6.0</string>
  <key>LSMinimumSystemVersion</key><string>13.0</string>
  <key>LSUIElement</key><true/>
  <key>EviePythonHome</key><string>${PY_HOME}</string>
</dict>
</plist>
PLIST

swiftc -O -o "$STAGE/Contents/MacOS/${APP_NAME}" "$REPO/packaging/macos/EvieLauncher.swift"
cp "$PY_EXE" "$STAGE/Contents/MacOS/evie-python"
cp "$REPO/services/__init__.py" "$STAGE/Contents/Resources/python/services/"
rsync -a --exclude '__pycache__' --exclude 'providers' "$REPO/services/computer_control" \
  "$STAGE/Contents/Resources/python/services/"
rsync -a --exclude '__pycache__' "$REPO/services/cc_live_validation" "$STAGE/Contents/Resources/python/services/"
# Step 17: the CC-1a Section 18 benchmark runs the real router in-process, so the backend modules it imports are
# bundled too (still sealed by the signature). No UI, tests, databases or credentials are copied.
rsync -a --exclude '__pycache__' --exclude 'computer_control' --exclude 'cc_live_validation' \
  "$REPO/services/" "$STAGE/Contents/Resources/python/services/"
rsync -a --exclude '__pycache__' "$REPO/integrations" "$STAGE/Contents/Resources/python/"
for m in main keyring_utils redaction drs tone memory remediation security_score; do
  cp "$REPO/$m.py" "$STAGE/Contents/Resources/python/"
done

# Inner code first, then the bundle (never --deep).
codesign --force --timestamp=none --sign "$IDENTITY" --identifier "$WORKER_ID" "$STAGE/Contents/MacOS/evie-python"
codesign --force --timestamp=none --sign "$IDENTITY" "$STAGE"
codesign --verify --strict --verbose=2 "$STAGE"

if [[ -e "$DEST" ]]; then
  existing="$(defaults read "$DEST/Contents/Info" CFBundleIdentifier 2>/dev/null || true)"
  [[ "$existing" == "$BUNDLE_ID" ]] || { echo "$DEST exists and is not Evie; refusing to replace" >&2; exit 2; }
  rm -rf "$DEST"
fi
mkdir -p "$DEST_DIR"
ditto "$STAGE" "$DEST"
codesign --verify --strict --verbose=2 "$DEST"
codesign -dv "$DEST" 2>&1 | grep -E "^(Identifier|TeamIdentifier|Authority=Apple Development|Authority=Developer ID)"
echo "BUILT $DEST"
