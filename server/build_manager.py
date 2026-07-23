"""
Background Build Manager (single build at a time).

Executes ./build.sh with GITHUB_TOKEN from the environment, always notifies the
PHP backend via callback (success or failure), then cleans the workspace.
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


def redact_secrets(text: str) -> str:
    """Strip GITHUB_TOKEN from text before logging or writing build logs."""
    if not text:
        return text
    token = os.environ.get("GITHUB_TOKEN", "")
    if token:
        from urllib.parse import quote

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
        stderr = ""

        try:
            # Fail fast if GitHub token missing (still callbacks below).
            if not (os.environ.get("GITHUB_TOKEN") or "").strip():
                failure_reason = (
                    "GITHUB_TOKEN environment variable is not set. "
                    "export GITHUB_TOKEN before starting the Build Server."
                )
                message = failure_reason
                write_build_log(build_log_path, "", failure_reason)
                log.error("build_id=%s aborted: GITHUB_TOKEN missing", job.build_id)
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
                ]

                env = os.environ.copy()
                env["CONFIG_DIR"] = str(self.config_dir)
                # GITHUB_TOKEN already in env — never log it.

                log.info(
                    "Executing pipeline via build.sh for build_id=%s", job.build_id
                )

                result = subprocess.run(
                    cmd,
                    cwd=str(self.repo_root),
                    env=env,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                stdout = redact_secrets(result.stdout or "")
                stderr = redact_secrets(result.stderr or "")
                write_build_log(build_log_path, stdout, stderr)

                if stdout:
                    log.info(
                        "build.sh stdout (build_id=%s):\n%s",
                        job.build_id,
                        stdout[-8000:],
                    )
                if stderr:
                    log.warning(
                        "build.sh stderr (build_id=%s):\n%s",
                        job.build_id,
                        stderr[-4000:],
                    )

                if result.returncode != 0:
                    err_tail = (stderr or stdout or "").strip().splitlines()
                    failure_reason = (
                        err_tail[-1]
                        if err_tail
                        else f"build.sh exited with code {result.returncode}"
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
            tb = redact_secrets(traceback.format_exc())
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

        # Callback must always run (success or failure).
        # Wire: status 1=success / 0=failed; error_message merges message + failure_reason.
        callback_success = False
        error_message = (
            ""
            if status == BuildStatus.SUCCESS
            else (failure_reason or message or "Build failed")
        )
        try:
            callback_success = send_callback(
                callback_url=self.callback_url,
                callback_apikey=self.callback_apikey,
                build_id=job.build_id,
                status=status.value,
                error_message=error_message,
                build_duration=duration,
                platform=job.platform,
                build_type=job.build_type,
                start_time=start_iso,
                end_time=end_iso,
                artifact_path=artifact if status == BuildStatus.SUCCESS else None,
                build_log_path=build_log_path,
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
