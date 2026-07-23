"""
Flutter Build Server — HTTP API (Stage 6)

POST /build         — validate, save assets.zip, queue job → HTTP 202
GET  /build/{id}    — build status
GET  /health        — UP + running_build + queue_size + callback_enabled
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse

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
    assets: UploadFile = File(..., description="assets.zip from backend"),
):
    """
    Validate request, create workspace, save assets.zip, enqueue job.
    Returns HTTP 202 immediately — build runs in the background.

    platform: 0 = Android, 1 = iOS (only 0 supported today)
    build_type: 1 = APK, 2 = AAB
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

    log.info(
        "Incoming build request build_id=%s institution_id=%s branch=%s "
        "platform=%s build_type=%s app_name=%s",
        build_id,
        institution_id,
        branch,
        platform,
        build_type,
        app_name,
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

    cfg = load_builder_config()
    if not (cfg.get("onepub_token") or "").strip():
        raise HTTPException(
            status_code=500,
            detail={
                "success": False,
                "build_id": build_id,
                "error": "onepub_token missing in config/builder.json "
                "(token must not come from the backend request)",
            },
        )

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
        "Accepted build_id=%s status=QUEUED running_build=%s queue_size=%s",
        build_id,
        health.get("running_build"),
        health.get("queue_size"),
    )

    return JSONResponse(
        status_code=202,
        content={
            "success": True,
            "status": "QUEUED",
            "build_id": build_id,
        },
    )
