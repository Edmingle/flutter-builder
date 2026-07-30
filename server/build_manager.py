"""
Background Build Manager (single build at a time).

Executes ./build.sh with github_token / onepub_token from each request,
always notifies the PHP backend via callback (success or failure), then
cleans the workspace.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
import time
import traceback
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Deque, Dict, Optional

from server.callback import (
    append_build_log,
    cleanup_build_log,
    cleanup_build_output,
    cleanup_build_workspace,
    discover_artifact,
    put_file_to_presigned_url,
    resolve_callback_url,
    send_callback,
    write_build_log,
)

log = logging.getLogger("flutter-builder.build_manager")


class BuildStatus(str, Enum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def redact_secrets(text: str, *extra_secrets: str) -> str:
    """Strip secrets from text before logging or writing build logs."""
    if not text:
        return text
    from urllib.parse import quote

    secrets = [s for s in extra_secrets if s]
    env_token = os.environ.get("GITHUB_TOKEN", "")
    if env_token:
        secrets.append(env_token)

    for token in secrets:
        text = text.replace(token, "***REDACTED***")
        text = text.replace(quote(token, safe=""), "***REDACTED***")
        text = text.replace(f"x-access-token:{token}", "x-access-token:***REDACTED***")
    return text


@dataclass
class BuildJob:
    build_id: str
    institution_id: str
    branch: str
    app_name: str
    bundle_id: str
    portal_name: str
    web_domain: str
    version_number: str
    platform: str
    build_type: str
    assets_zip_path: str
    workspace: str
    github_token: str = ""
    onepub_token: str = ""
    callback_apikey: str = ""
    artifact_upload_url: str = ""
    build_log_upload_url: str = ""
    artifact_content_type: str = "application/octet-stream"
    build_log_content_type: str = "text/plain"
    s3_storage_class: str = "REDUCED_REDUNDANCY"
    artifact_filename: str = ""
    upload: bool = False
    playstore_json_path: Optional[str] = None
    play_track: str = "internal"
    status: BuildStatus = BuildStatus.QUEUED
    error: Optional[str] = None
    build_duration: Optional[int] = None
    start_time: Optional[str] = None
    end_time: Optional[str] = None


@dataclass
class BuildManager:
    """Single-flight build executor with an in-memory FIFO queue."""

    repo_root: Path
    build_sh: Path
    config_dir: Path
    output_dir: Path = field(init=False)
    logs_dir: Path = field(init=False)
    callback_url: str = ""
    callback_apikey: str = ""
    _queue: Deque[BuildJob] = field(default_factory=deque)
    _jobs: Dict[str, BuildJob] = field(default_factory=dict)
    _running: Optional[BuildJob] = None
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _wake: threading.Event = field(default_factory=threading.Event)
    _worker: Optional[threading.Thread] = None
    _started: bool = False

    def __post_init__(self) -> None:
        self.output_dir = self.repo_root / "output"
        self.logs_dir = self.repo_root / "logs"
        self._load_callback_config()

    def _load_callback_config(self) -> None:
        config_file = self.config_dir / "builder.json"
        data: dict = {}
        if not config_file.is_file():
            log.warning("builder.json missing — using CALLBACK_BASE_URL env only")
        else:
            try:
                data = json.loads(config_file.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                log.error("Invalid builder.json: %s", exc)
                data = {}

        # Base URL only from env (CALLBACK_BASE_URL) or builder.json callback_base_url.
        # Path is fixed inside resolve_callback_url().
        base = (data.get("callback_base_url") or "").strip()
        self.callback_url = resolve_callback_url(base)
        # Prefer per-request apikey; builder.json callback_apikey is legacy/optional.
        self.callback_apikey = (data.get("callback_apikey") or "").strip()
        if self.callback_url:
            log.info("Callback enabled → %s", self.callback_url)
        else:
            log.error(
                "callback_base_url unset (set CALLBACK_BASE_URL env or "
                "callback_base_url in builder.json) — callbacks will fail"
            )

    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            self._worker = threading.Thread(
                target=self._worker_loop,
                name="build-manager-worker",
                daemon=True,
            )
            self._worker.start()
            self._started = True
            log.info("Build manager worker started (single build execution)")

    def enqueue(self, job: BuildJob) -> BuildJob:
        with self._lock:
            existing = self._jobs.get(job.build_id)
            if existing and existing.status in (BuildStatus.QUEUED, BuildStatus.RUNNING):
                raise ValueError(
                    f"build_id {job.build_id} is already {existing.status.value}"
                )

            job.status = BuildStatus.QUEUED
            job.error = None
            job.build_duration = None
            job.start_time = None
            job.end_time = None
            self._jobs[job.build_id] = job
            self._queue.append(job)
            queue_size = len(self._queue)
            running_id = self._running.build_id if self._running else None

        log.info(
            "Job queued build_id=%s queue_size=%s running_build=%s",
            job.build_id,
            queue_size,
            running_id,
        )
        self._wake.set()
        return job

    def get_job(self, build_id: str) -> Optional[BuildJob]:
        with self._lock:
            return self._jobs.get(build_id)

    def health(self) -> dict:
        with self._lock:
            return {
                "status": "UP",
                "running_build": self._running.build_id if self._running else None,
                "queue_size": len(self._queue),
                "callback_enabled": bool(self.callback_url),
            }

    def _worker_loop(self) -> None:
        while True:
            self._wake.wait(timeout=1.0)
            self._wake.clear()
            while True:
                job = self._take_next()
                if job is None:
                    break
                try:
                    self._run_job(job)
                except Exception:
                    # Last-resort: never kill the worker. _run_job already has
                    # its own top-level handler; this covers unexpected bugs.
                    log.exception(
                        "Unhandled error in worker for build_id=%s — continuing queue",
                        getattr(job, "build_id", "?"),
                    )
                    try:
                        self._finish_job(
                            job, BuildStatus.FAILED, error="Unhandled worker exception"
                        )
                    except Exception:
                        log.exception("Failed to mark job FAILED after unhandled error")

    def _take_next(self) -> Optional[BuildJob]:
        with self._lock:
            if self._running is not None:
                return None
            if not self._queue:
                return None
            job = self._queue.popleft()
            job.status = BuildStatus.RUNNING
            self._running = job
            queue_size = len(self._queue)
        log.info(
            "Job started build_id=%s queue_size=%s",
            job.build_id,
            queue_size,
        )
        return job

    def _run_job(self, job: BuildJob) -> None:
        """
        Full lifecycle with guaranteed callback + workspace cleanup.
        """
        build_output_dir = self.output_dir / f"build_{job.build_id}"
        build_log_path = self.logs_dir / f"build_{job.build_id}.log"

        start_iso = _utc_now_iso()
        started_mono = time.monotonic()
        job.start_time = start_iso

        status = BuildStatus.FAILED
        message = "Build failed"
        failure_reason: Optional[str] = None
        artifact: Optional[Path] = None
        stdout = ""

        try:
            # Tokens come from the cron/request — never from builder.json.
            if not (job.github_token or "").strip():
                failure_reason = "github_token missing on build request"
                message = failure_reason
                write_build_log(build_log_path, "", failure_reason)
                log.error("build_id=%s aborted: github_token missing", job.build_id)
            elif not (job.onepub_token or "").strip():
                failure_reason = "onepub_token missing on build request"
                message = failure_reason
                write_build_log(build_log_path, "", failure_reason)
                log.error("build_id=%s aborted: onepub_token missing", job.build_id)
            else:
                cmd = [
                    str(self.build_sh),
                    "--build-id",
                    job.build_id,
                    "--branch",
                    job.branch,
                    "--assets-zip",
                    job.assets_zip_path,
                    "--app-name",
                    job.app_name,
                    "--bundle-id",
                    job.bundle_id,
                    "--portal-name",
                    job.portal_name,
                    "--web-domain",
                    job.web_domain,
                    "--app-version",
                    job.version_number,
                    "--build-type",
                    job.build_type,
                    "--output",
                    str(build_output_dir),
                    "--onepub-token",
                    job.onepub_token,
                ]

                if job.upload and job.playstore_json_path:
                    cmd.extend(
                        [
                            "--upload",
                            "--playstore-json",
                            job.playstore_json_path,
                            "--play-track",
                            job.play_track or "internal",
                        ]
                    )
                    log.info(
                        "Play Store upload enabled for build_id=%s track=%s",
                        job.build_id,
                        job.play_track,
                    )

                env = os.environ.copy()
                env["CONFIG_DIR"] = str(self.config_dir)
                env["GITHUB_TOKEN"] = job.github_token
                env["ONEPUB_TOKEN"] = job.onepub_token

                log.info(
                    "Executing pipeline via build.sh for build_id=%s upload=%s",
                    job.build_id,
                    job.upload,
                )

                # Merge stderr into stdout so build output remains ordered, and
                # append each line immediately for the SSE log endpoint.
                output_lines = []
                build_log_path.parent.mkdir(parents=True, exist_ok=True)
                with build_log_path.open(
                    "w", encoding="utf-8", errors="replace"
                ) as build_log:
                    build_log.write("===== BUILD OUTPUT =====\n")
                    build_log.flush()

                    process = subprocess.Popen(
                        cmd,
                        cwd=str(self.repo_root),
                        env=env,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=True,
                        bufsize=1,
                        errors="replace",
                    )
                    if process.stdout is None:
                        raise RuntimeError("Could not capture build.sh output")

                    for line in process.stdout:
                        safe_line = redact_secrets(
                            line, job.github_token, job.onepub_token
                        )
                        output_lines.append(safe_line)
                        build_log.write(safe_line)
                        build_log.flush()

                    returncode = process.wait()

                stdout = "".join(output_lines)

                if stdout:
                    log.info(
                        "build.sh output (build_id=%s):\n%s",
                        job.build_id,
                        stdout[-8000:],
                    )

                if returncode != 0:
                    err_tail = stdout.strip().splitlines()
                    failure_reason = (
                        err_tail[-1]
                        if err_tail
                        else f"build.sh exited with code {returncode}"
                    )
                    message = failure_reason
                    log.error("Job failed build_id=%s: %s", job.build_id, failure_reason)
                else:
                    status = BuildStatus.SUCCESS
                    message = "Build completed successfully"
                    failure_reason = None
                    artifact = discover_artifact(build_output_dir)
                    if artifact is None:
                        log.error(
                            "Successful build_id=%s but artifact missing under %s",
                            job.build_id,
                            build_output_dir,
                        )
                    log.info("Job completed build_id=%s", job.build_id)

        except Exception as exc:
            status = BuildStatus.FAILED
            failure_reason = f"Unhandled exception: {exc}"
            message = failure_reason
            tb = redact_secrets(
                traceback.format_exc(), job.github_token, job.onepub_token
            )
            log.exception("Unhandled pipeline exception for build_id=%s", job.build_id)
            try:
                if build_log_path.exists():
                    append_build_log(
                        build_log_path,
                        "\n===== UNHANDLED EXCEPTION =====\n" + tb,
                    )
                else:
                    write_build_log(build_log_path, "", tb)
            except OSError:
                log.exception("Could not write exception to build log")

        # Drop secrets from memory after the pipeline finishes.
        job.github_token = ""
        job.onepub_token = ""

        end_iso = _utc_now_iso()
        duration = int(round(time.monotonic() - started_mono))
        job.end_time = end_iso
        job.build_duration = duration
        log.info("Build duration build_id=%s seconds=%s", job.build_id, duration)

        # Persist status before callback.
        with self._lock:
            job.status = status
            job.error = None if status == BuildStatus.SUCCESS else (failure_reason or message)
            self._jobs[job.build_id] = job

        # S3 presigned upload (cron provides URLs) → metadata-only callback.
        # Legacy: no URLs → multipart file callback.
        s3_direct = False
        artifact_filename = (job.artifact_filename or "").strip()
        if artifact is not None and not artifact_filename:
            artifact_filename = artifact.name

        log_url = (job.build_log_upload_url or "").strip()
        artifact_url = (job.artifact_upload_url or "").strip()
        storage_class = (job.s3_storage_class or "").strip()
        used_s3 = bool(log_url and artifact_url)

        if used_s3:
            log_ok = False
            if build_log_path.is_file():
                log_ok = put_file_to_presigned_url(
                    url=log_url,
                    file_path=build_log_path,
                    content_type=job.build_log_content_type or "text/plain",
                    storage_class=storage_class,
                    label=f"build_log build_id={job.build_id}",
                )
            else:
                log.error(
                    "build_log missing locally for build_id=%s — cannot PUT to S3",
                    job.build_id,
                )

            artifact_ok = True
            if status == BuildStatus.SUCCESS:
                if artifact is not None and artifact.is_file():
                    artifact_ok = put_file_to_presigned_url(
                        url=artifact_url,
                        file_path=artifact,
                        content_type=job.artifact_content_type
                        or "application/octet-stream",
                        storage_class=storage_class,
                        label=f"artifact build_id={job.build_id}",
                    )
                else:
                    log.error(
                        "artifact missing locally for successful build_id=%s — "
                        "cannot PUT to S3",
                        job.build_id,
                    )
                    artifact_ok = False

            s3_direct = True  # never send files to PHP when S3 URLs were provided
            if not (log_ok and artifact_ok):
                s3_err = "S3 upload failed"
                if not log_ok and not artifact_ok:
                    s3_err = "S3 upload failed for artifact and build_log"
                elif not log_ok:
                    s3_err = "S3 upload failed for build_log"
                else:
                    s3_err = "S3 upload failed for artifact"
                log.error("build_id=%s %s", job.build_id, s3_err)
                if status == BuildStatus.SUCCESS:
                    status = BuildStatus.FAILED
                    failure_reason = s3_err
                    message = s3_err
                    with self._lock:
                        job.status = status
                        job.error = failure_reason
                        self._jobs[job.build_id] = job

        # Clear URLs from memory (signed; treat as sensitive).
        job.artifact_upload_url = ""
        job.build_log_upload_url = ""

        # Callback must always run (success or failure).
        # Wire: status 1=success / 0=failed; error_message merges message + failure_reason.
        callback_success = False
        error_message = (
            ""
            if status == BuildStatus.SUCCESS
            else (failure_reason or message or "Build failed")
        )
        callback_apikey = (job.callback_apikey or self.callback_apikey or "").strip()
        job.callback_apikey = ""
        try:
            callback_success = send_callback(
                callback_url=self.callback_url,
                callback_apikey=callback_apikey,
                build_id=job.build_id,
                status=status.value,
                error_message=error_message,
                build_duration=duration,
                platform=job.platform,
                build_type=job.build_type,
                start_time=start_iso,
                end_time=end_iso,
                version_number=job.version_number,
                artifact_filename=artifact_filename,
                artifact_path=None
                if s3_direct
                else (artifact if status == BuildStatus.SUCCESS else None),
                build_log_path=None if s3_direct else build_log_path,
                s3_direct=s3_direct,
            )
        except Exception:
            log.exception(
                "Unexpected callback error for build_id=%s — continuing", job.build_id
            )
            callback_success = False

        if callback_success:
            log.info(
                "Callback delivered for build_id=%s — deleting artifacts and build log",
                job.build_id,
            )
            try:
                cleanup_build_output(build_output_dir)
            except Exception:
                log.exception("Failed deleting output dir for build_id=%s", job.build_id)
            try:
                cleanup_build_log(build_log_path)
            except Exception:
                log.exception("Failed deleting build log for build_id=%s", job.build_id)
        else:
            log.warning(
                "Callback delivery failed for build_id=%s — keeping "
                "output/build_%s and %s for manual recovery",
                job.build_id,
                job.build_id,
                build_log_path,
            )

        # Workspace cleanup always after callback attempt; never changes status.
        try:
            cleanup_build_workspace(Path(job.workspace))
        except Exception:
            log.exception(
                "Unexpected cleanup error for build_id=%s — continuing", job.build_id
            )

        self._finish_job(
            job,
            status,
            error=None if status == BuildStatus.SUCCESS else (failure_reason or message),
        )

    def _finish_job(
        self, job: BuildJob, status: BuildStatus, error: Optional[str] = None
    ) -> None:
        with self._lock:
            job.status = status
            job.error = error
            self._jobs[job.build_id] = job
            if self._running is job:
                self._running = None
            queue_size = len(self._queue)
            next_id = self._queue[0].build_id if self._queue else None

        log.info(
            "Job finished build_id=%s status=%s queue_size=%s next=%s",
            job.build_id,
            status.value,
            queue_size,
            next_id,
        )
        self._wake.set()


manager: Optional[BuildManager] = None


def get_manager() -> BuildManager:
    if manager is None:
        raise RuntimeError("BuildManager not initialized")
    return manager
