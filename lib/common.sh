#!/usr/bin/env bash
# Shared helpers for the Flutter Build Server.
# shellcheck shell=bash

# Resolve a path: absolute stays absolute; relative is relative to $1 (base).
resolve_path() {
  local base="$1"
  local path="$2"
  if [[ -z "$path" ]]; then
    echo ""
    return 0
  fi
  if [[ "$path" = /* ]]; then
    echo "$path"
  else
    echo "$base/$path"
  fi
}

# Read a top-level string key from a JSON file. Prints empty string if missing.
json_get() {
  local file="$1"
  local key="$2"
  python3 - "$file" "$key" <<'PY'
import json, sys
path, key = sys.argv[1], sys.argv[2]
with open(path, encoding="utf-8") as f:
    data = json.load(f)
value = data.get(key, "")
if value is None:
    value = ""
print(value)
PY
}

log_info() {
  printf '%s\n' "$*"
}

log_error() {
  printf 'ERROR: %s\n' "$*" >&2
}

# Load builder.json → FLUTTER_REPO, WORKSPACE_ROOT, COMMON_DIR, ONEPUB_TOKEN (if set).
# Requires SCRIPT_DIR and CONFIG_DIR to be set by the caller.
load_builder_config() {
  local config_file="${CONFIG_DIR}/builder.json"
  local example_file="${CONFIG_DIR}/builder.example.json"

  if [[ ! -f "$config_file" ]]; then
    log_error "Missing Build Server config: $config_file"
    echo "Copy the example and set flutter_repo:" >&2
    echo "  cp \"$example_file\" \"$config_file\"" >&2
    return 1
  fi

  FLUTTER_REPO="$(json_get "$config_file" "flutter_repo")"
  local configured_root
  configured_root="$(json_get "$config_file" "workspace_root")"
  local configured_common
  configured_common="$(json_get "$config_file" "common_dir")"
  local configured_token
  configured_token="$(json_get "$config_file" "onepub_token")"

  if [[ -z "$FLUTTER_REPO" ]]; then
    log_error "flutter_repo is empty in $config_file"
    echo "Set flutter_repo to your Flutter git URL (SSH or HTTPS)." >&2
    return 1
  fi

  if [[ -n "$configured_root" ]]; then
    WORKSPACE_ROOT="$(resolve_path "$SCRIPT_DIR" "$configured_root")"
  else
    WORKSPACE_ROOT="${WORKSPACE_ROOT:-$SCRIPT_DIR/workspace}"
  fi

  if [[ -n "$configured_common" ]]; then
    COMMON_DIR="$(resolve_path "$SCRIPT_DIR" "$configured_common")"
  else
    COMMON_DIR="${COMMON_DIR:-$SCRIPT_DIR/common}"
  fi

  # Prefer CLI/env ONEPUB_TOKEN. builder.json onepub_token is legacy only.
  if [[ -z "${ONEPUB_TOKEN:-}" && -n "$configured_token" ]]; then
    ONEPUB_TOKEN="$configured_token"
    log_info "Using legacy onepub_token from builder.json (prefer request/CLI token)"
  fi

  if [[ -d "$WORKSPACE_ROOT" ]]; then
    WORKSPACE_ROOT="$(cd "$WORKSPACE_ROOT" && pwd)"
  fi
  if [[ -d "$COMMON_DIR" ]]; then
    COMMON_DIR="$(cd "$COMMON_DIR" && pwd)"
  fi

  export FLUTTER_REPO WORKSPACE_ROOT COMMON_DIR ONEPUB_TOKEN
}

# Require GITHUB_TOKEN for private GitHub clones. Never log its value.
require_github_token() {
  if [[ -z "${GITHUB_TOKEN:-}" ]]; then
    log_error "GITHUB_TOKEN environment variable is not set"
    log_error "export GITHUB_TOKEN=ghp_... before starting the Build Server"
    return 1
  fi
  return 0
}

# Convert git@github.com:org/repo.git or https://github.com/org/repo.git
# into a plain https://github.com/org/repo.git URL (no credentials — safe to log).
github_https_url() {
  local repo="$1"
  python3 - "$repo" <<'PY'
import sys, re
repo = sys.argv[1].strip()
m = re.match(r"^git@github\.com:(.+)$", repo)
if m:
    path = m.group(1)
    if not path.endswith(".git"):
        path += ".git"
    print(f"https://github.com/{path}")
    raise SystemExit(0)
m = re.match(r"^ssh://git@github\.com/(.+)$", repo)
if m:
    path = m.group(1)
    if not path.endswith(".git"):
        path += ".git"
    print(f"https://github.com/{path}")
    raise SystemExit(0)
m = re.match(r"^https?://(?:[^@/]+@)?github\.com/(.+)$", repo)
if m:
    path = m.group(1)
    if not path.endswith(".git"):
        path += ".git"
    print(f"https://github.com/{path}")
    raise SystemExit(0)
print(repo)
PY
}

# Build an authenticated HTTPS clone URL using GITHUB_TOKEN.
# Format matches: git clone https://${GITHUB_TOKEN}@github.com/org/repo.git
# Result must NEVER be logged or echoed.
github_authenticated_url() {
  local https_url="$1"
  python3 - "$https_url" <<'PY'
import os, sys, re
from urllib.parse import quote
url = sys.argv[1].strip()
token = os.environ.get("GITHUB_TOKEN", "")
if not token:
    raise SystemExit("GITHUB_TOKEN is empty")
m = re.match(r"^https://github\.com/(.+)$", url)
if not m:
    print(url)
    raise SystemExit(0)
# Token as username (works for fine-grained github_pat_ tokens)
print(f"https://{quote(token, safe='')}@github.com/{m.group(1)}")
PY
}

# Redact GITHUB_TOKEN from stdin → stdout (for safe logging of git errors).
redact_secrets() {
  python3 - <<'PY'
import os, sys
from urllib.parse import quote
text = sys.stdin.read()
token = os.environ.get("GITHUB_TOKEN", "")
if token:
    text = text.replace(token, "***REDACTED***")
    text = text.replace(quote(token, safe=""), "***REDACTED***")
    text = text.replace(f"x-access-token:{token}", "x-access-token:***REDACTED***")
sys.stdout.write(text)
PY
}

# Portal assets from assets.zip (mobilertc is NOT included — lives in common/).
REQUIRED_ASSET_FILES=(
  logo.png
  google-services.json
  edmingleKey.jks
  key.properties
)

# Validate that $1 (assets directory) contains the mandatory portal files.
validate_assets_dir() {
  local assets_dir="$1"
  local missing=0
  local item

  if [[ -z "$assets_dir" || ! -d "$assets_dir" ]]; then
    log_error "Assets directory not found: ${assets_dir:-"(empty)"}"
    return 1
  fi

  for item in "${REQUIRED_ASSET_FILES[@]}"; do
    if [[ ! -f "$assets_dir/$item" ]]; then
      log_error "Missing required asset file: $assets_dir/$item"
      missing=1
    fi
  done

  if [[ "$missing" -ne 0 ]]; then
    return 1
  fi
  return 0
}

# Validate Build Server–owned MobileRTC under common/mobilertc.
validate_common_mobilertc() {
  local common="${COMMON_DIR:-}"
  local rtc_dir

  if [[ -z "$common" ]]; then
    log_error "COMMON_DIR is not set"
    return 1
  fi

  rtc_dir="$common/mobilertc"

  if [[ ! -d "$rtc_dir" ]]; then
    log_error "Build Server common mobilertc not found: $rtc_dir"
    log_error "Place the MobileRTC module at common/mobilertc/ (not inside assets.zip)."
    return 1
  fi

  if [[ -f "$rtc_dir/build.gradle" || -f "$rtc_dir/build.gradle.kts" ]]; then
    :
  else
    log_error "common/mobilertc is missing build.gradle or build.gradle.kts"
    return 1
  fi

  if [[ ! -f "$rtc_dir/mobilertc.aar" ]]; then
    log_error "common/mobilertc is missing mobilertc.aar"
    return 1
  fi

  return 0
}
