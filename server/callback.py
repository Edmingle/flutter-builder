"""
Backend callback after build completion.

Sends multipart/form-data to callback_url with retries.
Always attaches the build log. Attaches APK/AAB on SUCCESS when available.

Only the PHP base URL is configurable (env CALLBACK_BASE_URL or
builder.json callback_base_url). The callback path is fixed.
"""

from __future__ import annotations

import logging
import os
import shutil
import time
from pathlib import Path
from typing import List, Optional, Tuple

import requests

log = logging.getLogger("flutter-builder.callback")

# Attempt 1 immediate, then wait 5s, then 15s before attempts 2 and 3.
RETRY_BACKOFF_SECONDS = (0, 5, 15)

# Fixed PHP endpoint — only the host/base is configurable.
CALLBACK_PATH = "nuSource/api/v1/support/mobilebuild/callback"


def resolve_callback_url(base_url: str = "") -> str:
    """
    Build the full callback URL from a configurable base.

    Priority for base:
      1. CALLBACK_BASE_URL env
      2. explicit base_url argument (from builder.json callback_base_url)

    Example: http://localhost/ → http://localhost/nuSource/api/v1/support/mobilebuild/callback
    """
    base = (os.environ.get("CALLBACK_BASE_URL") or base_url or "").strip()
    if not base:
        return ""
    return f"{base.rstrip('/')}/{CALLBACK_PATH}"


def discover_artifact(build_output_dir: Path) -> Optional[Path]:
    """
    Find an APK/AAB/IPA in a single build's output directory only.
    Expects: output/build_<id>/*.{apk,aab,ipa}
    """
    if not build_output_dir.is_dir():
        log.error("Build output directory not found: %s", build_output_dir)
        return None

    candidates: List[Path] = []
    for pattern in ("*.apk", "*.aab", "*.ipa"):
        candidates.extend(p for p in build_output_dir.glob(pattern) if p.is_file())

    if not candidates:
        log.error("No APK/AAB/IPA found in %s", build_output_dir)
        return None

    newest = max(candidates, key=lambda p: p.stat().st_mtime)
    log.info("Uploading artifact: %s (%d bytes)", newest, newest.stat().st_size)
    return newest


def write_build_log(log_path: Path, stdout: str, stderr: str) -> Path:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    parts = []
    if stdout:
        parts.append("===== STDOUT =====\n" + stdout)
    if stderr:
        parts.append("===== STDERR =====\n" + stderr)
    if not parts:
        parts.append("(no build output captured)\n")
    log_path.write_text("\n\n".join(parts), encoding="utf-8", errors="replace")
    return log_path


def append_build_log(log_path: Path, text: str) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8", errors="replace") as fh:
        fh.write(text)
        if not text.endswith("\n"):
            fh.write("\n")


def send_callback(
    *,
    callback_url: str,
    callback_apikey: str,
    build_id: str,
    status: str,
    error_message: str,
    build_duration: int,
    platform: str,
    build_type: str,
    start_time: str,
    end_time: str,
    artifact_path: Optional[Path],
    build_log_path: Optional[Path],
) -> bool:
    """
    POST multipart callback with up to 3 attempts.
    Returns True on any 2xx response. Never raises.
    Always attempts to attach build_log when the file exists.

    Wire contract:
      status: "1" = success, "0" = failed
      error_message: failure text (empty on success)
    """
    if not callback_url:
        log.error(
            "callback_url not configured — cannot notify backend for build_id=%s",
            build_id,
        )
        return False

    is_success = status == "SUCCESS"
    data = {
        "build_id": str(build_id),
        "status": "1" if is_success else "0",
        "error_message": (error_message or "") if not is_success else "",
        "build_duration": str(build_duration),
        "platform": platform,
        "build_type": str(build_type),
        "start_time": start_time,
        "end_time": end_time,
    }

    headers = {"X-API-Key": callback_apikey} if callback_apikey else {}

    for attempt, delay in enumerate(RETRY_BACKOFF_SECONDS, start=1):
        if delay:
            log.info(
                "Retry callback build_id=%s attempt=%s waiting=%ss",
                build_id,
                attempt,
                delay,
            )
            time.sleep(delay)
        else:
            log.info(
                "Sending callback build_id=%s attempt=%s url=%s status=%s",
                build_id,
                attempt,
                callback_url,
                data["status"],
            )

        files: List[Tuple[str, tuple]] = []
        opened = []
        try:
            if is_success and artifact_path and artifact_path.is_file():
                log.info(
                    "Uploading artifact for build_id=%s: %s",
                    build_id,
                    artifact_path.name,
                )
                fh = open(artifact_path, "rb")
                opened.append(fh)
                files.append(
                    ("artifact", (artifact_path.name, fh, "application/octet-stream"))
                )
            elif is_success:
                log.error(
                    "artifact missing for successful build_id=%s — "
                    "sending callback without artifact",
                    build_id,
                )

            if build_log_path and build_log_path.is_file():
                fh = open(build_log_path, "rb")
                opened.append(fh)
                files.append(("build_log", (build_log_path.name, fh, "text/plain")))
            else:
                log.error(
                    "build_log missing for build_id=%s — sending callback without log",
                    build_id,
                )

            response = requests.post(
                callback_url,
                data=data,
                files=files or None,
                headers=headers,
                timeout=120,
            )
        except requests.RequestException as exc:
            log.error(
                "Callback failed build_id=%s attempt=%s error=%s",
                build_id,
                attempt,
                exc,
            )
            continue
        finally:
            for fh in opened:
                try:
                    fh.close()
                except OSError:
                    pass

        if 200 <= response.status_code < 300:
            log.info(
                "Callback successful build_id=%s attempt=%s http=%s",
                build_id,
                attempt,
                response.status_code,
            )
            return True

        log.error(
            "Callback failed build_id=%s attempt=%s http=%s body=%s",
            build_id,
            attempt,
            response.status_code,
            (response.text or "")[:500],
        )

    log.error("Callback exhausted all retries for build_id=%s", build_id)
    return False


def cleanup_build_workspace(workspace: Path) -> None:
    """Remove workspace/build_<id> (flutter-app, assets, assets.zip)."""
    if not workspace.exists():
        log.info("Cleanup skipped — workspace already gone: %s", workspace)
        return
    try:
        log.info("Cleaning workspace: %s", workspace)
        shutil.rmtree(workspace)
        log.info("Workspace deleted: %s", workspace)
    except OSError as exc:
        log.error("Cleanup failed for %s: %s", workspace, exc)


def cleanup_build_output(build_output_dir: Path) -> None:
    """Remove output/build_<id>/ after a successful callback."""
    if not build_output_dir.exists():
        log.info("Output cleanup skipped — already gone: %s", build_output_dir)
        return
    try:
        shutil.rmtree(build_output_dir)
        log.info("Deleted build output directory: %s", build_output_dir)
    except OSError as exc:
        log.error("Failed to delete build output %s: %s", build_output_dir, exc)


def cleanup_build_log(build_log_path: Path) -> None:
    """Remove logs/build_<id>.log after a successful callback."""
    if not build_log_path.exists():
        log.info("Build log cleanup skipped — already gone: %s", build_log_path)
        return
    try:
        build_log_path.unlink()
        log.info("Deleted build log: %s", build_log_path)
    except OSError as exc:
        log.error("Failed to delete build log %s: %s", build_log_path, exc)
