#!/usr/bin/env bash

set -euo pipefail

# ---------------------------------------------------------------------------
# Flutter Build Server — single entry point
# Stage 3: repo + assets.zip → validate → prepare → (existing) build
# ---------------------------------------------------------------------------

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"

CONFIG_DIR="${CONFIG_DIR:-$SCRIPT_DIR/config}"
SERVER_DIR="${SERVER_DIR:-$SCRIPT_DIR/server}"
OUTPUT_ROOT="${OUTPUT_ROOT:-$SCRIPT_DIR/output}"
OUTPUT=""
OUTPUT_EXPLICIT=false
LOGS="${LOGS:-$SCRIPT_DIR/logs}"


# OnePub CLI lives in the Dart pub global bin after `dart pub global activate onepub`
export PATH="${PATH}:${HOME}/.pub-cache/bin"

BUILD_START=$(date +%s)
STAGE_START=$BUILD_START

log_duration() {
  local label="$1"
  local now
  now=$(date +%s)
  local elapsed=$((now - STAGE_START))
  printf '⏱  %s: %ds\n' "$label" "$elapsed"
  STAGE_START=$now
}

usage() {
  cat <<'EOF'
Usage:
  ./build.sh \
      --build-id 101 \
      --branch develop \
      --assets-zip /path/to/assets.zip \
      --app-name "Ideas" \
      --bundle-id "com.edmingle.ideas" \
      --portal-name "ideas" \
      --web-domain "www.edmingle.academy" \
      --app-version "1.0.0" \
      --build-type 2

Required flags:
  --build-id         Build id → workspace/build_<id>/
  --branch           Git branch to checkout
  --app-name
  --bundle-id
  --portal-name
  --web-domain
  --app-version

Optional:
  --onepub-token     Overrides config/builder.json onepub_token
  --assets-zip PATH  Multipart assets.zip from backend
                     (default: workspace/build_<id>/assets.zip)
  --output DIR       Artifact output directory
                     (default: <repo>/output/build_<build_id>)
  --build-type       1 = APK, anything else (default 2) = AAB
  --upload           After build, upload to Google Play via Fastlane
  --playstore-json   Path to Play Console service-account JSON
  --play-track       Play track: internal|alpha|beta|production (default: internal)

Repository URL, workspace root, common dir, and OnePub token come from
config/builder.json (mobilertc lives in common/mobilertc — not assets.zip).
EOF
  exit 1
}

BUILD_ID=""
BRANCH=""
APP_NAME=""
APP_BUNDLE_ID=""
PORTAL_NAME=""
WEB_DOMAIN=""
APP_VERSION=""
ONEPUB_TOKEN="${ONEPUB_TOKEN:-}"
BUILD_TYPE="2"
UPLOAD="false"
PLAYSTORE_JSON="${PLAYSTORE_JSON:-}"
PLAY_TRACK="${PLAY_TRACK:-internal}"
ASSETS_ZIP=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --build-id)
      BUILD_ID="${2:-}"
      shift 2
      ;;
    --branch)
      BRANCH="${2:-}"
      shift 2
      ;;
    --app-name)
      APP_NAME="${2:-}"
      shift 2
      ;;
    --bundle-id)
      APP_BUNDLE_ID="${2:-}"
      shift 2
      ;;
    --portal-name)
      PORTAL_NAME="${2:-}"
      shift 2
      ;;
    --web-domain)
      WEB_DOMAIN="${2:-}"
      shift 2
      ;;
    --app-version)
      APP_VERSION="${2:-}"
      shift 2
      ;;
    --onepub-token)
      ONEPUB_TOKEN="${2:-}"
      shift 2
      ;;
    --build-type)
      BUILD_TYPE="${2:-}"
      shift 2
      ;;
    --assets-zip)
      ASSETS_ZIP="${2:-}"
      shift 2
      ;;
    --output)
      OUTPUT="${2:-}"
      OUTPUT_EXPLICIT=true
      shift 2
      ;;
    --upload)
      UPLOAD="true"
      shift 1
      ;;
    --playstore-json)
      PLAYSTORE_JSON="${2:-}"
      shift 2
      ;;
    --play-track)
      PLAY_TRACK="${2:-}"
      shift 2
      ;;
    -h|--help)
      usage
      ;;
    *)
      echo "Unknown argument: $1"
      usage
      ;;
  esac
done

missing_args=0
for pair in \
  "BUILD_ID:$BUILD_ID" \
  "BRANCH:$BRANCH" \
  "APP_NAME:$APP_NAME" \
  "APP_BUNDLE_ID:$APP_BUNDLE_ID" \
  "PORTAL_NAME:$PORTAL_NAME" \
  "WEB_DOMAIN:$WEB_DOMAIN" \
  "APP_VERSION:$APP_VERSION"
do
  key="${pair%%:*}"
  val="${pair#*:}"
  if [[ -z "$val" ]]; then
    echo "Missing required argument for $key"
    missing_args=1
  fi
done
if [[ "$missing_args" -ne 0 ]]; then
  usage
fi

if [[ ! "$BUILD_ID" =~ ^[A-Za-z0-9_-]+$ ]]; then
  log_error "Invalid --build-id '$BUILD_ID' (use letters, numbers, _ or - only)"
  exit 1
fi

# Load flutter_repo, workspace_root, common_dir, onepub_token from config
load_builder_config || exit 1

if [[ -z "${ONEPUB_TOKEN:-}" ]]; then
  log_error "ONEPUB_TOKEN is not set. Pass --onepub-token or set ONEPUB_TOKEN in the environment (API requests must send onepub_token)."
  exit 1
fi

BUILD_WORKSPACE="${WORKSPACE_ROOT}/build_${BUILD_ID}"
WORKSPACE="$BUILD_WORKSPACE"
APP="$BUILD_WORKSPACE/flutter-app"
ASSETS="$BUILD_WORKSPACE/assets"

# Per-build artifact directory: output/build_<id>/ (unless --output was passed)
if [[ "$OUTPUT_EXPLICIT" != "true" || -z "$OUTPUT" ]]; then
  OUTPUT="$OUTPUT_ROOT/build_${BUILD_ID}"
fi

mkdir -p "$OUTPUT" "$LOGS" "$WORKSPACE_ROOT"
OUTPUT="$(cd "$OUTPUT" && pwd)"
LOGS="$(cd "$LOGS" && pwd)"

if [[ "$UPLOAD" == "true" ]]; then
  if [[ -z "$PLAYSTORE_JSON" ]]; then
    log_error "--upload requires --playstore-json /path/to/play-service-account.json"
    echo "Note: This is the Google Play Console API service-account key," >&2
    echo "      NOT Firebase google-services.json." >&2
    exit 1
  fi
  if [[ ! -f "$PLAYSTORE_JSON" ]]; then
    log_error "Play Store JSON not found: $PLAYSTORE_JSON"
    exit 1
  fi
  PLAYSTORE_JSON="$(cd "$(dirname "$PLAYSTORE_JSON")" && pwd)/$(basename "$PLAYSTORE_JSON")"
fi

export APP_NAME APP_BUNDLE_ID PORTAL_NAME WEB_DOMAIN APP_VERSION ONEPUB_TOKEN BUILD_TYPE
export PLAYSTORE_JSON PLAY_TRACK
export BUILD_ID BRANCH FLUTTER_REPO WORKSPACE_ROOT BUILD_WORKSPACE COMMON_DIR
export WORKSPACE APP ASSETS SCRIPT_DIR CONFIG_DIR SERVER_DIR OUTPUT LOGS

echo "========== Flutter Build Server =========="
echo "SCRIPT_DIR       : $SCRIPT_DIR"
echo "CONFIG_DIR       : $CONFIG_DIR"
echo "SERVER_DIR       : $SERVER_DIR"
echo "COMMON_DIR       : $COMMON_DIR"
echo "FLUTTER_REPO     : $FLUTTER_REPO"
echo "WORKSPACE_ROOT   : $WORKSPACE_ROOT"
echo "BUILD_ID         : $BUILD_ID"
echo "BRANCH           : $BRANCH"
echo "BUILD_WORKSPACE  : $BUILD_WORKSPACE"
echo "APP              : $APP"
echo "ASSETS           : $ASSETS"
echo "ASSETS_ZIP       : ${ASSETS_ZIP:-$BUILD_WORKSPACE/assets.zip}"
echo "OUTPUT           : $OUTPUT"
echo "LOGS             : $LOGS"
echo "BUILD_TYPE       : $BUILD_TYPE ($([ "$BUILD_TYPE" = "1" ] && echo APK || echo AAB))"
echo "UPLOAD           : $UPLOAD"
if [[ "$UPLOAD" == "true" ]]; then
  echo "PLAYSTORE_JSON   : $PLAYSTORE_JSON"
  echo "PLAY_TRACK       : $PLAY_TRACK"
fi
echo "=========================================="
echo ""

# Fail early if common MobileRTC is missing (before clone / extract)
if ! validate_common_mobilertc; then
  exit 1
fi

if ! require_github_token; then
  exit 1
fi

# --- Git: clone / fetch / checkout / reset / clean ---
echo "========== Repository =========="
bash "$SCRIPT_DIR/prepare_repo.sh" --build-id "$BUILD_ID" --branch "$BRANCH"
log_duration "Repository"
echo ""

# --- Assets: receive zip → extract → validate ---
echo "========== Assets =========="
if [[ -n "$ASSETS_ZIP" ]]; then
  bash "$SCRIPT_DIR/prepare_assets.sh" --build-id "$BUILD_ID" --assets-zip "$ASSETS_ZIP"
else
  bash "$SCRIPT_DIR/prepare_assets.sh" --build-id "$BUILD_ID"
fi
log_duration "Assets"
echo ""

# --- Validate ---
echo "========== Validation =========="
bash "$SCRIPT_DIR/validate.sh"
log_duration "Validate"
echo ""

# --- Prepare (copy assets into Flutter project, gradle, replace_config) ---
echo "========== Preparing =========="
bash "$SCRIPT_DIR/prepare.sh"
log_duration "Prepare"
echo ""

# --- Flutter deps ---
echo "========== Flutter =========="
cd "$APP"

echo ">>> flutter clean"
flutter clean
echo ">>> flutter pub get"
flutter pub get
echo ">>> dart run intl_utils:generate"
dart run intl_utils:generate
echo ">>> dart run flutter_launcher_icons"
dart run flutter_launcher_icons
log_duration "Flutter"
echo ""

# --- Fastlane ---
echo "========== Fastlane =========="
mkdir -p "$APP/android/fastlane"
cp -R "$SCRIPT_DIR/fastlane/"* "$APP/android/fastlane/"

cd "$APP/android"
set +e
if [[ "$UPLOAD" == "true" ]]; then
  echo ">>> fastlane release (build + upload_to_play_store)"
  fastlane release buildType:"$BUILD_TYPE" playstore_json:"$PLAYSTORE_JSON" track:"$PLAY_TRACK"
else
  echo ">>> fastlane build"
  fastlane build buildType:"$BUILD_TYPE"
fi
BUILD_STATUS=$?
set -e
log_duration "Fastlane"
echo ""

# --- Collect artifacts ---
echo "========== Output =========="
mkdir -p "$OUTPUT"

if [[ "$BUILD_TYPE" == "1" ]]; then
  APK_SRC="$APP/build/app/outputs/flutter-apk/app-demo-release.apk"
  if [[ -f "$APK_SRC" ]]; then
    cp -f "$APK_SRC" "$OUTPUT/"
    echo "✓ APK copied: $OUTPUT/$(basename "$APK_SRC")"
  else
    FOUND_APK="$(find "$APP/build/app/outputs" -name '*demo*release*.apk' -type f 2>/dev/null | head -1 || true)"
    if [[ -n "$FOUND_APK" ]]; then
      cp -f "$FOUND_APK" "$OUTPUT/"
      echo "✓ APK copied: $OUTPUT/$(basename "$FOUND_APK")"
    else
      echo "WARNING: APK not found under $APP/build/app/outputs"
    fi
  fi
else
  AAB_SRC="$APP/build/app/outputs/bundle/demoRelease/app-demo-release.aab"
  if [[ -f "$AAB_SRC" ]]; then
    cp -f "$AAB_SRC" "$OUTPUT/"
    echo "✓ AAB copied: $OUTPUT/$(basename "$AAB_SRC")"
  else
    echo "WARNING: AAB not found at $AAB_SRC"
  fi

  BUNDLE_DIR="$APP/build/app/outputs/bundle/demoRelease"
  if [[ -d "$BUNDLE_DIR" ]]; then
    mkdir -p "$OUTPUT/bundle"
    cp -a "$BUNDLE_DIR/." "$OUTPUT/bundle/"
    echo "✓ Bundle dir copied"
  fi
fi

MAPPING="$APP/build/app/outputs/mapping/demoRelease/mapping.txt"
if [[ -f "$MAPPING" ]]; then
  cp -f "$MAPPING" "$OUTPUT/mapping.txt"
  echo "✓ mapping.txt copied"
fi

REPORT="$APP/android/fastlane/report.xml"
if [[ -f "$REPORT" ]]; then
  cp -f "$REPORT" "$OUTPUT/"
  echo "✓ report.xml copied"
fi

if [[ -d "$APP/build/reports" ]]; then
  cp -R "$APP/build/reports" "$OUTPUT/" || true
  echo "✓ build/reports copied"
fi

if [[ "$BUILD_STATUS" -ne 0 ]]; then
  if [[ -d "$APP/build" ]]; then
    find "$APP/build" -name "*.log" -type f 2>/dev/null | head -50 | while read -r logfile; do
      rel="${logfile#$APP/}"
      mkdir -p "$LOGS/$(dirname "$rel")"
      cp -f "$logfile" "$LOGS/$rel" || true
    done
  fi
  echo ""
  echo "Build failed (exit code: ${BUILD_STATUS})."
  BUILD_END=$(date +%s)
  printf '⏱  Total Build Time: %ds\n' "$((BUILD_END - BUILD_START))"
  exit "$BUILD_STATUS"
fi

log_duration "Artifact Copy"

BUILD_END=$(date +%s)
printf '⏱  Total Build Time: %ds\n' "$((BUILD_END - BUILD_START))"
echo ""
echo "Build completed successfully."
exit 0
