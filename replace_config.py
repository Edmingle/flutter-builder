from pathlib import Path
import re
import os
import json
from typing import Optional

workspace = Path(os.environ.get("WORKSPACE", ""))
if not workspace:
    raise SystemExit(
        "[replace_config] ERROR: WORKSPACE environment variable is not set."
    )

# Prefer explicit APP if provided by build.sh; otherwise WORKSPACE/app
if os.environ.get("APP"):
    root = Path(os.environ["APP"])
else:
    root = workspace / "app"

# Repo-level config/ (preferred) then workspace/config/ (legacy fallback)
config_dir = Path(os.environ.get("CONFIG_DIR", "")) if os.environ.get("CONFIG_DIR") else None


def load_config():
    """
    Resolve build config values with the following priority:
      1. Environment variables (APP_BUNDLE_ID, APP_NAME, PORTAL_NAME,
         WEB_DOMAIN, APP_VERSION)
      2. CONFIG_DIR/build_config.json (repo config/)
      3. workspace/config/build_config.json (legacy fallback)
    """
    json_config = {}
    candidates = []
    if config_dir:
        candidates.append(config_dir / "build_config.json")
    candidates.append(workspace / "config" / "build_config.json")

    config_path = None
    for candidate in candidates:
        if candidate.exists():
            config_path = candidate
            break

    if config_path is not None:
        json_config = json.loads(config_path.read_text())
        print(f"[replace_config] Loaded fallback config from {config_path}")
    else:
        shown = candidates[0] if candidates else "(none)"
        print(
            f"[replace_config] No config JSON found (tried {shown}) — "
            "relying entirely on environment variables"
        )
        config_path = candidates[0] if candidates else Path("build_config.json")

    def resolve(env_key, json_key):
        value = os.environ.get(env_key)
        if value:
            return value
        value = json_config.get(json_key)
        if value:
            return value
        raise SystemExit(
            f"[replace_config] ERROR: '{env_key}' is not set as an environment "
            f"variable, and '{json_key}' is not present in {config_path}. "
            f"Provide one of the two before running this script."
        )

    return {
        "app_bundle_id": resolve("APP_BUNDLE_ID", "app_bundle_id"),
        "app_name": resolve("APP_NAME", "app_name"),
        "portal_name": resolve("PORTAL_NAME", "portal_name"),
        "web_domain": resolve("WEB_DOMAIN", "web_domain"),
        "app_version": resolve("APP_VERSION", "app_version"),
    }


config = load_config()

APP_BUNDLE_ID = config["app_bundle_id"]
APP_NAME = config["app_name"]
PORTAL_NAME = config["portal_name"]
WEB_DOMAIN = config["web_domain"]
APP_VERSION = config["app_version"]

print(f"[replace_config] APP_BUNDLE_ID={APP_BUNDLE_ID}")
print(f"[replace_config] APP_NAME={APP_NAME}")
print(f"[replace_config] PORTAL_NAME={PORTAL_NAME}")
print(f"[replace_config] WEB_DOMAIN={WEB_DOMAIN}")
print(f"[replace_config] APP_VERSION={APP_VERSION}")
print(f"[replace_config] root={root}")


PLACEHOLDER_BUNDLE_ID = "com.edmingle.app"


def replace_bundle_id(txt: str) -> str:
    """
    Swap Play/application id only.

    Do NOT rewrite Java/Kotlin `package`, AGP `namespace`, or AndroidManifest
    `package=` — those define MainActivity's FQCN and must stay stable while
    applicationId becomes APP_BUNDLE_ID (e.g. com.edmingle.learn).
    """
    out = []
    for line in txt.splitlines(keepends=True):
        stripped = line.lstrip()
        if re.match(r"package\s+\S", stripped):
            out.append(line)
            continue
        if re.search(r"\bnamespace\b", stripped) and PLACEHOLDER_BUNDLE_ID in line:
            out.append(line)
            continue
        if re.search(
            r"""\bpackage\s*=\s*['"]""" + re.escape(PLACEHOLDER_BUNDLE_ID) + r"""['"]""",
            line,
        ):
            out.append(line)
            continue
        out.append(line.replace(PLACEHOLDER_BUNDLE_ID, APP_BUNDLE_ID))
    return "".join(out)


# Portal slug is only swapped in this Dart file (not a global search/replace).
PORTAL_NAME_FILE = Path("lib/utils/flavor_utils.dart")


def apply_replacements(txt: str, rel_path: Optional[Path] = None) -> str:
    txt = replace_bundle_id(txt)
    txt = txt.replace("Edmingle Demo", APP_NAME)
    txt = txt.replace("enterpriseplanportal.edmingle.com", WEB_DOMAIN)
    if rel_path is not None and rel_path == PORTAL_NAME_FILE:
        txt = txt.replace("enterpriseplanportal", PORTAL_NAME)
    return txt


TEXT_SUFFIXES = {
    ".kt",
    ".java",
    ".xml",
    ".gradle",
    ".kts",
    ".dart",
    ".properties",
    ".json",
    ".plist",
    ".swift",
    ".pbxproj",
    ".entitlements",
    ".xcconfig",
    ".m",
    ".mm",
    ".yaml",
    ".yml",
}


def should_skip(path):
    # Check path RELATIVE TO `root` so absolute paths that happen to contain
    # a "build" segment (e.g. under a temp dir) are not all skipped.
    try:
        rel_parts = path.relative_to(root).parts
    except ValueError:
        rel_parts = path.parts
    return any(
        part in {"build", ".dart_tool", "Pods"}
        for part in rel_parts
    )


if not root.exists():
    raise SystemExit(f"[replace_config] ERROR: Flutter app not found at {root}")

pubspec = root / "pubspec.yaml"
if not pubspec.exists():
    raise SystemExit(f"[replace_config] ERROR: Missing {pubspec}")

content = pubspec.read_text()

content = re.sub(
    r"^version:.*$",
    f"version: {APP_VERSION}",
    content,
    flags=re.MULTILINE,
)

content = apply_replacements(content)

pubspec.write_text(content)

for folder in ("android", "lib", "ios", "test"):

    directory = root / folder

    if not directory.exists():
        continue

    for file in directory.rglob("*"):

        if not file.is_file():
            continue

        if should_skip(file):
            continue

        if file.suffix.lower() not in TEXT_SUFFIXES:
            continue

        text = file.read_text(errors="ignore")
        rel = file.relative_to(root)

        updated = apply_replacements(text, rel_path=rel)

        if updated != text:
            file.write_text(updated)
            print(f"[replace_config] Updated: {rel}")

print("Replacement completed.")
