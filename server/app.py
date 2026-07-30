"""
Flutter Build Server — HTTP API (Stage 6)

POST /build         — validate, save assets.zip, queue job → HTTP 202
GET  /build/{id}    — build status
GET  /build/{id}/logs/stream — live build logs (SSE)
GET  /health        — UP + running_build + queue_size + callback_enabled

build_type=2 (AAB) also uploads to Google Play when playstore-json is provided.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path
from typing import Annotated, Optional

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse

from server.build_manager import BuildJob, BuildManager, BuildStatus, get_manager

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = REPO_ROOT / "config" / "builder.json"
BUILD_SH = REPO_ROOT / "build.sh"
CONFIG_DIR = REPO_ROOT / "config"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("flutter-builder")

app = FastAPI(
    title="Flutter Build Server",
    description="Stage 6 — async queue + backend callback after build",
    version="0.6.0",
)

BUILD_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
VALID_PLAY_TRACKS = {"internal", "alpha", "beta", "production"}

# Platform constants (must match PHP / backend contract)
PLATFORM_ANDROID = "0"
PLATFORM_IOS = "1"
SUPPORTED_PLATFORMS = {PLATFORM_ANDROID}  # iOS not built yet


@app.on_event("startup")
def on_startup() -> None:
    from server import build_manager as bm

    bm.manager = BuildManager(
        repo_root=REPO_ROOT,
        build_sh=BUILD_SH,
        config_dir=CONFIG_DIR,
    )
    bm.manager.start()
    log.info("Build manager ready")


def load_builder_config() -> dict:
    if not CONFIG_PATH.is_file():
        raise HTTPException(
            status_code=500,
            detail={
                "success": False,
                "error": f"Missing config: {CONFIG_PATH}. "
                "Copy config/builder.example.json to config/builder.json.",
            },
        )
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=500,
            detail={"success": False, "error": f"Invalid builder.json: {exc}"},
        ) from exc


def resolve_workspace_root(cfg: dict) -> Path:
    raw = (cfg.get("workspace_root") or "workspace").strip()
    path = Path(raw)
    if not path.is_absolute():
        path = REPO_ROOT / path
    return path.resolve()


def require_field(name: str, value: Optional[str]) -> str:
    if value is None or not str(value).strip():
        raise HTTPException(
            status_code=400,
            detail={
                "success": False,
                "error": f"Missing or empty required field: {name}",
            },
        )
    return str(value).strip()


def _read_log_chunk(log_path: Path, position: int) -> tuple[str, int]:
    try:
        with log_path.open(encoding="utf-8", errors="replace") as log_file:
            log_file.seek(position)
            chunk = log_file.read()
            return chunk, log_file.tell()
    except FileNotFoundError:
        # Callback cleanup can remove the file between exists() and open().
        return "", position


async def _build_log_events(
    manager: BuildManager, build_id: str, log_path: Path
):
    position = 0
    last_keepalive = asyncio.get_running_loop().time()

    while True:
        if log_path.is_file():
            chunk, position = await asyncio.to_thread(
                _read_log_chunk, log_path, position
            )
            if chunk:
                yield f"event: log\ndata: {json.dumps(chunk)}\n\n"

        current_job = manager.get_job(build_id)
        if current_job is None:
            yield 'event: error\ndata: "Build no longer exists"\n\n'
            return

        if current_job.status in (BuildStatus.SUCCESS, BuildStatus.FAILED):
            payload = json.dumps(
                {
                    "build_id": build_id,
                    "status": current_job.status.value,
                    "error": current_job.error,
                }
            )
            yield f"event: done\ndata: {payload}\n\n"
            return

        now = asyncio.get_running_loop().time()
        if now - last_keepalive >= 15:
            yield ": keep-alive\n\n"
            last_keepalive = now

        await asyncio.sleep(0.5)


@app.get("/health")
def health():
    try:
        return get_manager().health()
    except RuntimeError:
        return {
            "status": "UP",
            "running_build": None,
            "queue_size": 0,
            "callback_enabled": False,
        }


@app.get("/build/{build_id}")
def get_build(build_id: str):
    job = get_manager().get_job(build_id)
    if job is None:
        raise HTTPException(
            status_code=404,
            detail={
                "success": False,
                "build_id": build_id,
                "error": f"Unknown build_id: {build_id}",
            },
        )
    body = {
        "build_id": job.build_id,
        "status": job.status.value,
    }
    if job.error and job.status == BuildStatus.FAILED:
        body["error"] = job.error
    return body


@app.get("/build/{build_id}/logs/stream")
async def stream_build_logs(build_id: str):
    """Stream the current build log as Server-Sent Events."""
    manager = get_manager()
    job = manager.get_job(build_id)
    if job is None:
        raise HTTPException(
            status_code=404,
            detail={
                "success": False,
                "build_id": build_id,
                "error": f"Unknown build_id: {build_id}",
            },
        )

    log_path = manager.logs_dir / f"build_{build_id}.log"

    return StreamingResponse(
        _build_log_events(manager, build_id, log_path),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/build", status_code=202)
async def create_build(
    build_id: str = Form(...),
    institution_id: str = Form(...),
    branch: str = Form(...),
    app_name: str = Form(...),
    bundle_id: str = Form(...),
    portal_name: str = Form(...),
    web_domain: str = Form(...),
    version_number: str = Form(...),
    platform: str = Form(PLATFORM_ANDROID),
    build_type: str = Form(...),
    github_token: str = Form(...),
    onepub_token: str = Form(...),
    assets: UploadFile = File(..., description="assets.zip from backend"),
    play_track: Annotated[str, Form(alias="play-track")] = "internal",
    playstore_json: Annotated[
        Optional[UploadFile],
        File(alias="playstore-json"),
    ] = None,
    callback_apikey: str = Form(""),
    artifact_upload_url: str = Form(""),
    build_log_upload_url: str = Form(""),
    artifact_content_type: str = Form("application/octet-stream"),
    build_log_content_type: str = Form("text/plain"),
    s3_storage_class: str = Form("REDUCED_REDUNDANCY"),
    artifact_filename: str = Form(""),
):
    """
    Validate request, create workspace, save assets.zip, enqueue job.
    Returns HTTP 202 immediately — build runs in the background.

    Tokens (github_token, onepub_token) must be sent by cron on every request.
    They are not stored in builder.json.

    When artifact_upload_url / build_log_upload_url are provided, the worker
    PUTs files to S3 and callbacks PHP with JSONString only (no APK/AAB body).

    platform: 0 = Android, 1 = iOS (only 0 supported today)
    build_type: 1 = APK (build only), 2 = AAB + Play Store upload
    For build_type=2, playstore-json (Play Console service-account) is required.
    """
    build_id = require_field("build_id", build_id)
    institution_id = require_field("institution_id", institution_id)
    branch = require_field("branch", branch)
    app_name = require_field("app_name", app_name)
    bundle_id = require_field("bundle_id", bundle_id)
    portal_name = require_field("portal_name", portal_name)
    web_domain = require_field("web_domain", web_domain)
    version_number = require_field("version_number", version_number)
    platform = require_field("platform", platform)
    build_type = require_field("build_type", build_type)
    github_token = require_field("github_token", github_token)
    onepub_token = require_field("onepub_token", onepub_token)
    play_track = (play_track or "internal").strip().lower()
    callback_apikey = (callback_apikey or "").strip()
    artifact_upload_url = (artifact_upload_url or "").strip()
    build_log_upload_url = (build_log_upload_url or "").strip()
    artifact_content_type = (
        artifact_content_type or "application/octet-stream"
    ).strip()
    build_log_content_type = (build_log_content_type or "text/plain").strip()
    s3_storage_class = (s3_storage_class or "").strip()
    artifact_filename = (artifact_filename or "").strip()

    upload_to_play = build_type == "2"
    s3_mode = bool(artifact_upload_url or build_log_upload_url)

    log.info(
        "Incoming build request build_id=%s institution_id=%s branch=%s "
        "platform=%s build_type=%s upload=%s play_track=%s app_name=%s "
        "s3_mode=%s github_token=%s onepub_token=%s",
        build_id,
        institution_id,
        branch,
        platform,
        build_type,
        upload_to_play,
        play_track,
        app_name,
        s3_mode,
        "present",
        "present",
    )

    if not BUILD_ID_RE.match(build_id):
        raise HTTPException(
            status_code=400,
            detail={
                "success": False,
                "build_id": build_id,
                "error": "Invalid build_id (use letters, numbers, _ or - only)",
            },
        )

    if build_type not in ("1", "2"):
        raise HTTPException(
            status_code=400,
            detail={
                "success": False,
                "build_id": build_id,
                "error": "build_type must be 1 (APK) or 2 (AAB)",
            },
        )

    if platform not in (PLATFORM_ANDROID, PLATFORM_IOS):
        raise HTTPException(
            status_code=400,
            detail={
                "success": False,
                "build_id": build_id,
                "error": "platform must be 0 (Android) or 1 (iOS)",
            },
        )

    if platform not in SUPPORTED_PLATFORMS:
        raise HTTPException(
            status_code=400,
            detail={
                "success": False,
                "build_id": build_id,
                "error": "Unsupported platform '1' (iOS) — only platform 0 (Android) is supported",
            },
        )

    if play_track not in VALID_PLAY_TRACKS:
        raise HTTPException(
            status_code=400,
            detail={
                "success": False,
                "build_id": build_id,
                "error": "play-track must be one of: internal, alpha, beta, production",
            },
        )

    filename = (assets.filename or "").lower()
    if not filename.endswith(".zip"):
        raise HTTPException(
            status_code=400,
            detail={
                "success": False,
                "build_id": build_id,
                "error": "assets must be a .zip file (assets.zip)",
            },
        )

    if upload_to_play:
        if playstore_json is None or not (playstore_json.filename or "").strip():
            raise HTTPException(
                status_code=400,
                detail={
                    "success": False,
                    "build_id": build_id,
                    "error": "build_type=2 requires playstore-json "
                    "(Play Console service-account JSON, NOT Firebase google-services.json)",
                },
            )
        pj_name = (playstore_json.filename or "").lower()
        if not pj_name.endswith(".json"):
            raise HTTPException(
                status_code=400,
                detail={
                    "success": False,
                    "build_id": build_id,
                    "error": "playstore-json must be a .json file",
                },
            )

    if s3_mode:
        if not artifact_upload_url or not build_log_upload_url:
            raise HTTPException(
                status_code=400,
                detail={
                    "success": False,
                    "build_id": build_id,
                    "error": "When using S3 uploads, both artifact_upload_url "
                    "and build_log_upload_url are required",
                },
            )

    cfg = load_builder_config()

    if not BUILD_SH.is_file():
        raise HTTPException(
            status_code=500,
            detail={
                "success": False,
                "build_id": build_id,
                "error": f"build.sh not found at {BUILD_SH}",
            },
        )

    workspace_root = resolve_workspace_root(cfg)
    build_workspace = workspace_root / f"build_{build_id}"
    assets_zip_path = build_workspace / "assets.zip"
    playstore_json_path: Optional[str] = None

    try:
        log.info("Workspace creation: %s", build_workspace)
        build_workspace.mkdir(parents=True, exist_ok=True)

        log.info("Saving uploaded assets.zip → %s", assets_zip_path)
        content = await assets.read()
        if not content:
            raise HTTPException(
                status_code=400,
                detail={
                    "success": False,
                    "build_id": build_id,
                    "error": "assets.zip is empty",
                },
            )
        assets_zip_path.write_bytes(content)
        log.info("Received assets.zip (%d bytes)", len(content))

        if upload_to_play and playstore_json is not None:
            dest = build_workspace / "play-service-account.json"
            pj_bytes = await playstore_json.read()
            if not pj_bytes:
                raise HTTPException(
                    status_code=400,
                    detail={
                        "success": False,
                        "build_id": build_id,
                        "error": "playstore-json is empty",
                    },
                )
            dest.write_bytes(pj_bytes)
            playstore_json_path = str(dest)
            log.info("Received playstore-json (%d bytes) → %s", len(pj_bytes), dest)
    except HTTPException:
        raise
    except OSError as exc:
        log.exception("Failed to prepare workspace")
        return JSONResponse(
            status_code=500,
            content={
                "success": False,
                "build_id": build_id,
                "error": f"Failed to prepare workspace: {exc}",
            },
        )

    job = BuildJob(
        build_id=build_id,
        institution_id=institution_id,
        branch=branch,
        app_name=app_name,
        bundle_id=bundle_id,
        portal_name=portal_name,
        web_domain=web_domain,
        version_number=version_number,
        platform=platform,
        build_type=build_type,
        assets_zip_path=str(assets_zip_path),
        workspace=str(build_workspace),
        github_token=github_token,
        onepub_token=onepub_token,
        callback_apikey=callback_apikey,
        artifact_upload_url=artifact_upload_url,
        build_log_upload_url=build_log_upload_url,
        artifact_content_type=artifact_content_type,
        build_log_content_type=build_log_content_type,
        s3_storage_class=s3_storage_class,
        artifact_filename=artifact_filename,
        upload=upload_to_play,
        playstore_json_path=playstore_json_path,
        play_track=play_track,
    )

    try:
        get_manager().enqueue(job)
    except ValueError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "success": False,
                "build_id": build_id,
                "error": str(exc),
            },
        ) from exc

    health = get_manager().health()
    log.info(
        "Accepted build_id=%s status=QUEUED upload=%s running_build=%s queue_size=%s",
        build_id,
        upload_to_play,
        health.get("running_build"),
        health.get("queue_size"),
    )

    return JSONResponse(
        status_code=202,
        content={
            "success": True,
            "status": "QUEUED",
            "build_id": build_id,
            "upload": upload_to_play,
        },
    )
