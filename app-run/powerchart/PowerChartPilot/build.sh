#!/usr/bin/env bash
# Build a production .app + drag-to-Applications .dmg for PowerChart Pilot.
#
# Output (mirrors clawagents_desktop):
#   dist-release/PowerChart Pilot.app
#   dist-release/PowerChart-Pilot_<ver>_<arch>.dmg
#
# Usage:
#   ./build.sh              # host architecture
#   SKIP_NOTARIZE=1 ./build.sh
#   REQUIRE_SIGN=1 ./build.sh

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

APP_NAME="PowerChart Pilot"
BUNDLE_ID="ai.openadapt.powerchartpilot"
VERSION="$(python3 -c "print(open('VERSION').read().strip())" 2>/dev/null || echo "1.0.0")"
ARCH="$(uname -m)"
case "$ARCH" in
  arm64) ARCH_LABEL=aarch64 ;;
  x86_64) ARCH_LABEL=x86_64 ;;
  *) ARCH_LABEL="$ARCH" ;;
esac

DIST="$ROOT/dist-release"
APP="$DIST/$APP_NAME.app"
DMG_OUT="$DIST/PowerChart-Pilot_${VERSION}_${ARCH_LABEL}.dmg"
SIGN_SCRIPT="$ROOT/scripts/macos_sign_notarize.sh"

echo "[build] Version $VERSION ($ARCH_LABEL)"
echo "[build] Compiling release binary…"
swift build -c release

# Icon (Pillow required once)
if [ ! -f "$ROOT/assets/AppIcon.icns" ]; then
  echo "[build] Generating app icon…"
  if python3 -c "import PIL" 2>/dev/null; then
    python3 "$ROOT/scripts/gen_icon.py"
  else
    echo "[build] WARN: Pillow missing — icon will be blank. pip install pillow && re-run."
  fi
fi

echo "[build] Assembling $APP"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

cp "$ROOT/.build/release/PowerChartPilot" "$APP/Contents/MacOS/PowerChartPilot"
chmod +x "$APP/Contents/MacOS/PowerChartPilot"

if [ -f "$ROOT/assets/AppIcon.icns" ]; then
  cp "$ROOT/assets/AppIcon.icns" "$APP/Contents/Resources/AppIcon.icns"
fi

cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
	<key>CFBundleDevelopmentRegion</key>
	<string>en</string>
	<key>CFBundleDisplayName</key>
	<string>$APP_NAME</string>
	<key>CFBundleExecutable</key>
	<string>PowerChartPilot</string>
	<key>CFBundleIconFile</key>
	<string>AppIcon</string>
	<key>CFBundleIdentifier</key>
	<string>$BUNDLE_ID</string>
	<key>CFBundleInfoDictionaryVersion</key>
	<string>6.0</string>
	<key>CFBundleName</key>
	<string>$APP_NAME</string>
	<key>CFBundlePackageType</key>
	<string>APPL</string>
	<key>CFBundleShortVersionString</key>
	<string>$VERSION</string>
	<key>CFBundleVersion</key>
	<string>$VERSION</string>
	<key>LSMinimumSystemVersion</key>
	<string>13.0</string>
	<key>NSHighResolutionCapable</key>
	<true/>
	<key>NSPrincipalClass</key>
	<string>NSApplication</string>
	<key>LSApplicationCategoryType</key>
	<string>public.app-category.productivity</string>
	<key>NSAppleEventsUsageDescription</key>
	<string>PowerChart Pilot raises and focuses the Citrix / PowerChart window during replay.</string>
	<key>NSAppleScriptEnabled</key>
	<true/>
</dict>
</plist>
PLIST

echo -n "APPL????" > "$APP/Contents/PkgInfo"

# Sign
HAS_DEVELOPER_ID=0
if security find-identity -v -p codesigning 2>/dev/null | grep -q 'Developer ID Application:'; then
  HAS_DEVELOPER_ID=1
fi

chmod +x "$SIGN_SCRIPT" 2>/dev/null || true
if [ "$HAS_DEVELOPER_ID" = "1" ] && [ -x "$SIGN_SCRIPT" ]; then
  echo "[build] Developer ID found — signing .app …"
  SIGN_REQUIRED=1 SKIP_NOTARIZE=1 "$SIGN_SCRIPT" "$APP"
elif [ "${REQUIRE_SIGN:-0}" = "1" ]; then
  echo "[build] ERROR: REQUIRE_SIGN=1 but no Developer ID Application identity."
  exit 1
else
  echo "[build] No Developer ID — ad-hoc signing (Gatekeeper will block downloaded DMGs)."
  codesign --force --deep --sign - \
    --entitlements "$ROOT/Resources/entitlements.plist" \
    "$APP"
fi

# DMG with Applications symlink (same UX as ClawAgents Desktop)
echo "[build] Creating DMG …"
rm -f "$DMG_OUT"
STAGE=$(mktemp -d)
cp -R "$APP" "$STAGE/"
ln -sf /Applications "$STAGE/Applications"
# Hide background clutter; Finder opens a clean drag-to-Applications window.
hdiutil create \
  -volname "$APP_NAME" \
  -srcfolder "$STAGE" \
  -ov -format UDZO \
  "$DMG_OUT"
rm -rf "$STAGE"

if [ "$HAS_DEVELOPER_ID" = "1" ] && [ -x "$SIGN_SCRIPT" ]; then
  echo "[build] Signing/notarizing DMG …"
  SIGN_REQUIRED=1 "$SIGN_SCRIPT" --notarize-dmg "$DMG_OUT" "$APP"
fi

echo ""
echo "[build] Done. Output:"
echo "  $APP"
echo "  $DMG_OUT"
echo ""
echo "[build] Install: open the DMG and drag \"$APP_NAME\" into Applications."
echo "        Or:      open \"$DMG_OUT\""
