#!/usr/bin/env bash
# Deep-sign PowerChart Pilot.app (Developer ID) and optionally notarize the DMG.
#
# Prerequisites (one-time), same as ClawAgents Desktop:
#   1. Developer ID Application certificate in Keychain
#      Xcode → Settings → Accounts → Manage Certificates → + → Developer ID Application
#   2. Notary credentials:
#      xcrun notarytool store-credentials powerchart-pilot-notary \
#        --apple-id "YOUR_APPLE_ID" --team-id SK58FV375Z --password "app-specific-password"
#
# Usage:
#   ./scripts/macos_sign_notarize.sh "/path/to/PowerChart Pilot.app"
#   ./scripts/macos_sign_notarize.sh --notarize-dmg "/path/to.dmg" ["/path/to/App.app"]
# Env:
#   APPLE_SIGNING_IDENTITY  override identity string
#   NOTARY_PROFILE          default: powerchart-pilot-notary
#   SKIP_NOTARIZE=1         sign only
#   SIGN_REQUIRED=1         fail if no Developer ID identity

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENTITLEMENTS="${ENTITLEMENTS:-$ROOT/Resources/entitlements.plist}"
NOTARY_PROFILE="${NOTARY_PROFILE:-powerchart-pilot-notary}"
TEAM_ID="${APPLE_TEAM_ID:-SK58FV375Z}"

MODE="sign"
APP=""
DMG=""

if [ "${1:-}" = "--notarize-dmg" ]; then
  MODE="notarize-dmg"
  DMG="${2:-}"
  APP="${3:-}"
else
  APP="${1:-}"
  DMG="${2:-}"
fi

pick_identity() {
  if [ -n "${APPLE_SIGNING_IDENTITY:-}" ]; then
    echo "$APPLE_SIGNING_IDENTITY"
    return
  fi
  security find-identity -v -p codesigning 2>/dev/null \
    | sed -n 's/.*"\(Developer ID Application: [^"]*\)".*/\1/p' \
    | head -1 || true
}

require_identity() {
  IDENTITY="$(pick_identity)"
  if [ -n "$IDENTITY" ]; then
    return 0
  fi
  echo "[sign] ERROR: no \"Developer ID Application\" identity in Keychain."
  echo "  Create one: Xcode → Settings → Accounts → Manage Certificates → + → Developer ID Application"
  echo "  Team ID: $TEAM_ID"
  if [ "${SIGN_REQUIRED:-0}" = "1" ]; then
    exit 1
  fi
  exit 0
}

sign_app() {
  local app="$1"
  require_identity
  echo "[sign] Identity: $IDENTITY"
  echo "[sign] Entitlements: $ENTITLEMENTS"
  codesign --force --deep --options runtime \
    --entitlements "$ENTITLEMENTS" \
    --sign "$IDENTITY" "$app"
  codesign --verify --deep --strict --verbose=2 "$app"
  echo "[sign] Signed $app"
}

notarize_dmg() {
  local dmg="$1"
  local app="${2:-}"
  if [ "${SKIP_NOTARIZE:-0}" = "1" ]; then
    echo "[sign] SKIP_NOTARIZE=1 — skipping Apple notarization"
    return 0
  fi
  echo "[sign] Submitting DMG for notarization (profile=$NOTARY_PROFILE)…"
  xcrun notarytool submit "$dmg" --keychain-profile "$NOTARY_PROFILE" --wait
  echo "[sign] Stapling DMG…"
  xcrun stapler staple "$dmg"
  if [ -n "$app" ] && [ -d "$app" ]; then
    xcrun stapler staple "$app" || true
  fi
  echo "[sign] Notarization complete"
}

case "$MODE" in
  sign)
    [ -d "$APP" ] || { echo "Usage: $0 /path/to/App.app"; exit 1; }
    sign_app "$APP"
    ;;
  notarize-dmg)
    [ -f "$DMG" ] || { echo "Usage: $0 --notarize-dmg /path/to.dmg [/path/to/App.app]"; exit 1; }
    # Re-sign the DMG contents' identity is already on the .app; just sign the DMG wrapper.
    IDENTITY="$(pick_identity)"
    if [ -n "$IDENTITY" ]; then
      codesign --force --sign "$IDENTITY" "$DMG" || true
    fi
    notarize_dmg "$DMG" "$APP"
    ;;
esac
