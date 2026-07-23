#!/usr/bin/env bash

set -euo pipefail

WORKSPACE="${WORKSPACE:?WORKSPACE is not set}"
APP="${APP:-$WORKSPACE/flutter-app}"
ASSETS="${ASSETS:-$WORKSPACE/assets}"
SCRIPT_DIR="${SCRIPT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"

# OnePub is installed via `dart pub global activate onepub`
export PATH="${PATH}:${HOME}/.pub-cache/bin"

# macOS sed requires an extension arg for -i; GNU sed does not.
sed_inplace() {
  if [[ "$(uname -s)" == "Darwin" ]]; then
    sed -i '' "$@"
  else
    sed -i "$@"
  fi
}

set_gradle_property() {
  local key="$1"
  local value="$2"
  local file="$APP/android/gradle.properties"

  mkdir -p "$APP/android"
  touch "$file"

  if grep -q "^${key}=" "$file" 2>/dev/null; then
    sed_inplace "s|^${key}=.*|${key}=${value}|" "$file"
  else
    printf '\n%s=%s\n' "$key" "$value" >> "$file"
  fi
}

verify_mobilertc() {
  local rtc_dir="$APP/android/mobilertc"

  echo "Verifying mobilertc module..."
  echo "Destination: $rtc_dir"

  if [[ ! -d "$rtc_dir" ]]; then
    echo "ERROR: mobilertc directory not found at $rtc_dir"
    exit 1
  fi
  echo "✓ mobilertc directory found"

  if [[ -f "$rtc_dir/build.gradle" ]]; then
    echo "✓ build.gradle found"
  elif [[ -f "$rtc_dir/build.gradle.kts" ]]; then
    echo "✓ build.gradle.kts found"
  else
    echo "ERROR: mobilertc is missing build.gradle or build.gradle.kts"
    exit 1
  fi

  if [[ ! -f "$rtc_dir/mobilertc.aar" ]]; then
    echo "ERROR: mobilertc is missing mobilertc.aar"
    exit 1
  fi
  echo "✓ mobilertc.aar found"
}

echo "========== Preparing Flutter Project =========="

cd "$APP"

echo "Authenticating with OnePub..."
# ONEPUB_TOKEN is expected in the environment (exported by build.sh)
onepub import
onepub pub add hugeicons

mkdir -p "$APP/assets/logos"

echo "Copying assets into Flutter project..."

echo "Adding Portal Logo..."
cp "$ASSETS/logo.png" \
  "$APP/assets/logos/${PORTAL_NAME}-logo.png"

echo "Adding App Icon..."
cp "$ASSETS/logo.png" \
  "$APP/assets/logos/app-icon.png"

echo "Adding edmingleKey.jks..."
cp "$ASSETS/edmingleKey.jks" \
  "$APP/android/edmingleKey.jks"

echo "Adding key.properties..."
cp "$ASSETS/key.properties" \
  "$APP/android/key.properties"
# Point storeFile at the JKS we just copied into android/
sed_inplace "s|^storeFile=.*|storeFile=${APP}/android/edmingleKey.jks|" \
  "$APP/android/key.properties"

if [[ ! -d "$APP/android/mobilertc" ]]; then
  echo "Copying mobilertc from Build Server common..."
  echo "Source: $COMMON_DIR/mobilertc"
  echo "Destination: $APP/android/mobilertc"
  if [[ -z "${COMMON_DIR:-}" || ! -d "$COMMON_DIR/mobilertc" ]]; then
    echo "ERROR: COMMON_DIR/mobilertc not found (COMMON_DIR=${COMMON_DIR:-unset})"
    exit 1
  fi
  cp -R "$COMMON_DIR/mobilertc" "$APP/android/mobilertc"
else
  echo "mobilertc already exists. Skipping copy."
  echo "Destination: $APP/android/mobilertc"
fi

verify_mobilertc

echo "========== Applying Gradle Configuration =========="
echo "Updating org.gradle.daemon"
set_gradle_property "org.gradle.daemon" "false"
echo "Updating org.gradle.jvmargs"
set_gradle_property "org.gradle.jvmargs" "-Xmx3g -XX:MaxMetaspaceSize=768m -XX:+UseSerialGC"
echo "Updating org.gradle.parallel"
set_gradle_property "org.gradle.parallel" "false"
echo "Updating org.gradle.workers.max"
set_gradle_property "org.gradle.workers.max" "1"
echo "Updating kotlin.compiler.execution.strategy"
set_gradle_property "kotlin.compiler.execution.strategy" "in-process"
echo "Updating org.gradle.caching"
set_gradle_property "org.gradle.caching" "true"
echo "Updating org.gradle.configuration-cache"
set_gradle_property "org.gradle.configuration-cache" "false"
echo "Updating org.gradle.vfs.watch"
set_gradle_property "org.gradle.vfs.watch" "false"
echo "✓ Gradle configuration updated."

echo "Replacing google-services.json..."
cp -f "$ASSETS/google-services.json" \
  "$APP/android/app/google-services.json"

echo "Applying build configuration replacements..."
python3 "$SCRIPT_DIR/replace_config.py"

echo "✓ Assets copied into Flutter project"
echo "✓ Preparing completed."
