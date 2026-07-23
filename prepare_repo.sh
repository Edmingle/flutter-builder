#!/usr/bin/env bash

set -euo pipefail

# ---------------------------------------------------------------------------
# Stage 2 — Git repository management
# Clones/updates workspace/build_<build_id>/flutter-app using GITHUB_TOKEN.
# The token is never logged, printed, or written to disk.
# ---------------------------------------------------------------------------

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"

CONFIG_DIR="${CONFIG_DIR:-$SCRIPT_DIR/config}"

usage() {
  cat <<'EOF'
Usage:
  ./prepare_repo.sh --build-id 101 --branch develop

Required:
  --build-id   Build identifier (workspace/build_<id>/)
  --branch     Git branch to checkout (e.g. release, develop, feature/x)

Environment:
  GITHUB_TOKEN   GitHub Personal Access Token (required for private repos)

Config (config/builder.json):
  flutter_repo     Git URL of the Flutter project (required)
  workspace_root   Root for all build workspaces (default: <repo>/workspace)
EOF
  exit 1
}

BUILD_ID=""
BRANCH=""

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
    -h|--help)
      usage
      ;;
    *)
      echo "Unknown argument: $1"
      usage
      ;;
  esac
done

if [[ -z "$BUILD_ID" || -z "$BRANCH" ]]; then
  echo "Missing required --build-id and/or --branch"
  usage
fi

if [[ ! "$BUILD_ID" =~ ^[A-Za-z0-9_-]+$ ]]; then
  log_error "Invalid --build-id '$BUILD_ID' (use letters, numbers, _ or - only)"
  exit 1
fi

if ! command -v git >/dev/null 2>&1; then
  log_error "git not found on PATH"
  exit 1
fi

require_github_token || exit 1
load_builder_config || exit 1

BUILD_WORKSPACE="${WORKSPACE_ROOT}/build_${BUILD_ID}"
APP="${BUILD_WORKSPACE}/flutter-app"
REMOTE_REF="origin/${BRANCH}"

# Safe-to-log HTTPS URL (no credentials)
REPO_HTTPS="$(github_https_url "$FLUTTER_REPO")"
# Authenticated URL — NEVER echo / log this variable
CLONE_URL="$(github_authenticated_url "$REPO_HTTPS")"

export BUILD_ID BRANCH BUILD_WORKSPACE APP

echo "========== Git Repository Management =========="
echo "BUILD_ID         : $BUILD_ID"
echo "BRANCH           : $BRANCH"
echo "FLUTTER_REPO     : $REPO_HTTPS"
echo "AUTH             : GITHUB_TOKEN (redacted)"
echo "WORKSPACE_ROOT   : $WORKSPACE_ROOT"
echo "BUILD_WORKSPACE  : $BUILD_WORKSPACE"
echo "APP              : $APP"
echo "================================================"
echo ""

if [[ ! -d "$BUILD_WORKSPACE" ]]; then
  log_info "Creating workspace: $BUILD_WORKSPACE"
  mkdir -p "$BUILD_WORKSPACE"
else
  log_info "Workspace already exists: $BUILD_WORKSPACE"
fi

run_git() {
  local description="$1"
  shift
  # Log argv without secrets (CLONE_URL never passed as a logged arg here)
  log_info ">>> git $*"
  local output
  set +e
  output="$(git "$@" 2>&1)"
  local rc=$?
  set -e
  if [[ -n "$output" ]]; then
    printf '%s\n' "$output" | redact_secrets
  fi
  if [[ "$rc" -ne 0 ]]; then
    log_error "Git step failed: $description"
    log_error "Command: git $*"
    exit 1
  fi
}

# Clone or update
if [[ ! -d "$APP/.git" ]]; then
  if [[ -e "$APP" ]]; then
    log_error "Path exists but is not a git repository: $APP"
    log_error "Remove it or choose another build id, then retry."
    exit 1
  fi

  log_info "Cloning repository"
  log_info ">>> git clone <GITHUB_TOKEN-authenticated-url> flutter-app  (in $BUILD_WORKSPACE)"
  set +e
  CLONE_OUTPUT="$(git -C "$BUILD_WORKSPACE" clone -- "$CLONE_URL" flutter-app 2>&1)"
  CLONE_RC=$?
  set -e
  if [[ -n "$CLONE_OUTPUT" ]]; then
    printf '%s\n' "$CLONE_OUTPUT" | redact_secrets
  fi
  if [[ "$CLONE_RC" -ne 0 ]]; then
    log_error "Git step failed: clone repository"
    log_error "Command: git clone <redacted> flutter-app"
    exit 1
  fi
else
  log_info "Repository already exists: $APP"
  # Ensure remote uses authenticated HTTPS for fetch (never log the URL)
  log_info "Updating origin remote for authenticated fetch"
  set +e
  REMOTE_OUT="$(git -C "$APP" remote set-url origin -- "$CLONE_URL" 2>&1)"
  REMOTE_RC=$?
  set -e
  if [[ -n "$REMOTE_OUT" ]]; then
    printf '%s\n' "$REMOTE_OUT" | redact_secrets
  fi
  if [[ "$REMOTE_RC" -ne 0 ]]; then
    log_error "Git step failed: set-url origin"
    exit 1
  fi
fi

cd "$APP"

log_info "Fetching latest changes"
run_git "fetch remotes" fetch --all

if ! git rev-parse --verify --quiet "$REMOTE_REF" >/dev/null; then
  log_error "Remote branch not found: $REMOTE_REF"
  log_error "Fetch completed, but origin does not have branch '$BRANCH'."
  exit 1
fi

log_info "Checking out branch: $BRANCH"
run_git "checkout branch" checkout -B "$BRANCH" "$REMOTE_REF"

log_info "Resetting repository to $REMOTE_REF"
run_git "reset hard to origin/$BRANCH" reset --hard "$REMOTE_REF"

log_info "Cleaning repository"
run_git "clean untracked files" clean -fdx

# Scrub credentials from local git config so the token is not persisted on disk
log_info "Scrubbing credentials from local origin URL"
git remote set-url origin -- "$REPO_HTTPS" >/dev/null 2>&1 || true

if [[ ! -f "$APP/pubspec.yaml" ]]; then
  log_error "Checkout succeeded but pubspec.yaml is missing at $APP"
  log_error "Confirm flutter_repo points at a Flutter project root."
  exit 1
fi

echo ""
log_info "✓ Flutter repository ready"
log_info "  APP=$APP"
log_info "  BRANCH=$(git -C "$APP" rev-parse --abbrev-ref HEAD)"
log_info "  COMMIT=$(git -C "$APP" rev-parse --short HEAD)"
echo ""
