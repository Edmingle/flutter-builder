#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="${SCRIPT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
# shellcheck source=lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"

WORKSPACE="${WORKSPACE:?WORKSPACE is not set}"
APP="${APP:-$WORKSPACE/flutter-app}"
ASSETS="${ASSETS:-$WORKSPACE/assets}"

# OnePub is installed via `dart pub global activate onepub`
export PATH="${PATH}:${HOME}/.pub-cache/bin"

echo "========== Validating Build Workspace =========="

required_vars=(
  APP_NAME
  APP_BUNDLE_ID
  PORTAL_NAME
  WEB_DOMAIN
  APP_VERSION
  ONEPUB_TOKEN
)

for var in "${required_vars[@]}"; do
  if [[ -z "${!var:-}" ]]; then
    echo "Missing environment variable: $var"
    exit 1
  fi
done

if [[ ! -d "$APP" ]]; then
  echo "Flutter project not found: $APP"
  exit 1
fi

if [[ ! -f "$APP/pubspec.yaml" ]]; then
  echo "Missing pubspec.yaml in Flutter project: $APP"
  exit 1
fi

if [[ ! -d "$APP/android" ]]; then
  echo "Missing android/ in Flutter project: $APP"
  exit 1
fi

echo "Validating assets at: $ASSETS"
if ! validate_assets_dir "$ASSETS"; then
  echo "Asset validation failed for workspace/build_<id>/assets/"
  exit 1
fi
echo "✓ Assets validated"

echo "Validating common mobilertc at: ${COMMON_DIR:-unset}/mobilertc"
if ! validate_common_mobilertc; then
  exit 1
fi
echo "✓ Common mobilertc validated"

if ! command -v flutter >/dev/null 2>&1; then
  echo "ERROR: flutter not found on PATH"
  exit 1
fi

if ! command -v dart >/dev/null 2>&1; then
  echo "ERROR: dart not found on PATH"
  exit 1
fi

if ! command -v fastlane >/dev/null 2>&1; then
  echo "ERROR: fastlane not found on PATH"
  exit 1
fi

if ! command -v onepub >/dev/null 2>&1; then
  echo "ERROR: onepub not found on PATH (required for OnePub authentication)"
  echo "Install it once with:"
  echo "  dart pub global activate onepub"
  echo "Then ensure ~/.pub-cache/bin is on your PATH, or re-run ./build.sh (it adds that path automatically)."
  exit 1
fi

if ! command -v python3 >/dev/null 2>&1; then
  echo "ERROR: python3 not found on PATH"
  exit 1
fi

echo "ONEPUB_TOKEN length: ${#ONEPUB_TOKEN}"
echo ""
echo "========== Build Configuration =========="
echo "APP_NAME         : $APP_NAME"
echo "APP_BUNDLE_ID    : $APP_BUNDLE_ID"
echo "PORTAL_NAME      : $PORTAL_NAME"
echo "WEB_DOMAIN       : $WEB_DOMAIN"
echo "APP_VERSION      : $APP_VERSION"
echo "BUILD_ID         : ${BUILD_ID:-}"
echo "BRANCH           : ${BRANCH:-}"
echo "WORKSPACE_ROOT   : ${WORKSPACE_ROOT:-}"
echo "BUILD_WORKSPACE  : ${BUILD_WORKSPACE:-$WORKSPACE}"
echo "WORKSPACE        : $WORKSPACE"
echo "APP              : $APP"
echo "ASSETS           : $ASSETS"
echo "COMMON_DIR       : ${COMMON_DIR:-}"
echo "CONFIG_DIR       : ${CONFIG_DIR:-}"
echo "========================================="
echo ""

echo "✓ Validation completed."
