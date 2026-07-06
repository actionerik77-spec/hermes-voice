#!/usr/bin/env bash
# Build + bundle + install HermesVoice.app
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
APP="$ROOT/dist/HermesVoice.app"

cd "$ROOT"
swift build -c release

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS"
cp "$ROOT/.build/release/HermesVoice" "$APP/Contents/MacOS/HermesVoice"

cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleIdentifier</key><string>sh.hermes.voicepill</string>
  <key>CFBundleName</key><string>HermesVoice</string>
  <key>CFBundleExecutable</key><string>HermesVoice</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>1.0.0</string>
  <key>CFBundleVersion</key><string>1</string>
  <key>LSMinimumSystemVersion</key><string>13.0</string>
  <key>LSUIElement</key><true/>
  <key>NSMicrophoneUsageDescription</key>
  <string>HermesVoice records your voice commands for Hermes and transcribes them locally with Parakeet.</string>
</dict>
</plist>
PLIST

codesign --force --deep -s - "$APP"
mkdir -p "$HOME/Applications"
rm -rf "$HOME/Applications/HermesVoice.app"
cp -R "$APP" "$HOME/Applications/HermesVoice.app"
echo "installed: $HOME/Applications/HermesVoice.app"
