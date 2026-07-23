#!/usr/bin/env bash

set -euo pipefail

# ---------------------------------------------------------------------------
# Stage 3 — Asset extraction
# Receives assets.zip, extracts + validates into:
#   workspace/build_<build_id>/assets/
# Does not download from S3, rename, or normalize filenames.
# ---------------------------------------------------------------------------

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"

CONFIG_DIR="${CONFIG_DIR:-$SCRIPT_DIR/config}"

usage() {
  cat <<'EOF'
Usage:
  ./prepare_assets.sh --build-id 101 [--assets-zip /path/to/assets.zip]

Required:
  --build-id       Build identifier (workspace/build_<id>/)

Optional:
  --assets-zip     Path to assets.zip
                   Default: workspace/build_<id>/assets.zip

Extracts into:
  workspace/build_<id>/assets/

ZIP must contain (portal assets only — NOT mobilertc):
  logo.png
  google-services.json
  edmingleKey.jks
  key.properties

Supports ZIP layouts:
  1) assets.zip → assets/logo.png ...
  2) assets.zip → logo.png ... (flat)
EOF
  exit 1
}

BUILD_ID=""
ASSETS_ZIP_INPUT=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --build-id)
      BUILD_ID="${2:-}"
      shift 2
      ;;
    --assets-zip)
      ASSETS_ZIP_INPUT="${2:-}"
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

if [[ -z "$BUILD_ID" ]]; then
  echo "Missing required --build-id"
  usage
fi

if [[ ! "$BUILD_ID" =~ ^[A-Za-z0-9_-]+$ ]]; then
  log_error "Invalid --build-id '$BUILD_ID' (use letters, numbers, _ or - only)"
  exit 1
fi

if ! command -v unzip >/dev/null 2>&1; then
  log_error "unzip not found on PATH (required to extract assets.zip)"
  exit 1
fi

load_builder_config || exit 1

BUILD_WORKSPACE="${WORKSPACE_ROOT}/build_${BUILD_ID}"
ASSETS="${BUILD_WORKSPACE}/assets"
ASSETS_ZIP="${BUILD_WORKSPACE}/assets.zip"

mkdir -p "$BUILD_WORKSPACE"

echo "========== Asset Extraction =========="
echo "BUILD_ID         : $BUILD_ID"
echo "BUILD_WORKSPACE  : $BUILD_WORKSPACE"
echo "ASSETS           : $ASSETS"
echo "======================================"
echo ""

# Place / verify assets.zip inside the build workspace
if [[ -n "$ASSETS_ZIP_INPUT" ]]; then
  if [[ ! -f "$ASSETS_ZIP_INPUT" ]]; then
    log_error "assets.zip not found: $ASSETS_ZIP_INPUT"
    exit 1
  fi
  ASSETS_ZIP_INPUT="$(cd "$(dirname "$ASSETS_ZIP_INPUT")" && pwd)/$(basename "$ASSETS_ZIP_INPUT")"
  if [[ "$ASSETS_ZIP_INPUT" != "$ASSETS_ZIP" ]]; then
    log_info "Received assets.zip: $ASSETS_ZIP_INPUT"
    cp -f "$ASSETS_ZIP_INPUT" "$ASSETS_ZIP"
    log_info "Copied to: $ASSETS_ZIP"
  else
    log_info "Received assets.zip: $ASSETS_ZIP"
  fi
else
  if [[ ! -f "$ASSETS_ZIP" ]]; then
    log_error "assets.zip not found at $ASSETS_ZIP"
    log_error "Place assets.zip in the build workspace or pass --assets-zip /path/to/assets.zip"
    exit 1
  fi
  log_info "Received assets.zip: $ASSETS_ZIP"
fi

# Extract to a temp dir, then normalize into BUILD_WORKSPACE/assets/
EXTRACT_TMP="$(mktemp -d "${TMPDIR:-/tmp}/flutter-builder-assets.XXXXXX")"
cleanup_tmp() {
  rm -rf "$EXTRACT_TMP"
}
trap cleanup_tmp EXIT

log_info "Extracting assets"
log_info ">>> unzip -q $ASSETS_ZIP -d $EXTRACT_TMP"
if ! unzip -q "$ASSETS_ZIP" -d "$EXTRACT_TMP"; then
  log_error "Failed to extract assets.zip (corrupt or invalid ZIP): $ASSETS_ZIP"
  exit 1
fi

# Detect layout: nested assets/ folder vs flat files at ZIP root
ASSET_SRC=""
if [[ -d "$EXTRACT_TMP/assets" ]]; then
  log_info "Detected ZIP layout: top-level assets/ directory"
  ASSET_SRC="$EXTRACT_TMP/assets"
else
  log_info "Detected ZIP layout: flat files at archive root"
  ASSET_SRC="$EXTRACT_TMP"
fi

if [[ -z "$(ls -A "$ASSET_SRC" 2>/dev/null || true)" ]]; then
  log_error "assets.zip extracted but no files were found"
  exit 1
fi

rm -rf "$ASSETS"
mkdir -p "$ASSETS"
# Preserve structure; do not rename files
cp -a "$ASSET_SRC"/. "$ASSETS"/

log_info "Assets extracted → $ASSETS"

log_info "Validating assets"
if ! validate_assets_dir "$ASSETS"; then
  log_error "Asset validation failed — aborting (will not continue to prepare.sh)"
  exit 1
fi

log_info "Assets validated"
echo ""
log_info "✓ Assets ready at $ASSETS"
echo ""

export BUILD_ID BUILD_WORKSPACE ASSETS ASSETS_ZIP
