# Flutter Build Server — HTTP API (Stage 6)

Async single-build queue. After each build, the worker notifies the PHP
backend via multipart callback, then deletes `workspace/build_<id>/`.

## Run

```bash
cd /path/to/Script_for_flutter_builder

cp -n config/builder.example.json config/builder.json
# set flutter_repo, callback_base_url (tokens come from each POST /build)

python3 -m venv server/.venv
source server/.venv/bin/activate
pip install -r server/requirements.txt

uvicorn server.app:app --host 0.0.0.0 --port 8080
```

## Config (`config/builder.json`)

```json
{
  "flutter_repo": "https://github.com/company/flutter-app.git",
  "workspace_root": "workspace",
  "common_dir": "common",
  "callback_base_url": "http://localhost/"
}
```

Only the **base URL** is configurable (`callback_base_url` or env
`CALLBACK_BASE_URL`). The path is fixed:

`{base}/nuSource/api/v1/support/mobilebuild/callback`

Env overrides JSON when both are set.

**Tokens are not stored on the Build Server.** Cron must send on every
`POST /build`:

- `github_token` (required)
- `onepub_token` (required)
- `callback_apikey` (optional — used as `X-API-Key` on the PHP callback)

API key for callbacks is sent as header `X-API-Key` when provided.

## Endpoints

### `POST /build` → HTTP 202

The client (PHP) does **not** send a callback URL. After the build, this
server calls PHP using `callback_base_url` from `builder.json` (or env
`CALLBACK_BASE_URL`):

| Environment | `callback_base_url` | Full callback URL (path always fixed) |
|-------------|---------------------|----------------------------------------|
| Local / test | `http://localhost/` | `http://localhost/nuSource/api/v1/support/mobilebuild/callback` |
| Production | `https://your-php-host/` | `https://your-php-host/nuSource/api/v1/support/mobilebuild/callback` |

Only the base changes; `/nuSource/api/v1/support/mobilebuild/callback` is hardcoded.

```bash
# build_type=2 → AAB + Play Store upload (playstore-json required)
curl -X POST "http://127.0.0.1:8080/build" \
  -F build_id=1001 \
  -F institution_id=42 \
  -F branch=v2/prod \
  -F app_name=Ideas \
  -F bundle_id=com.edmingle.ideas \
  -F portal_name=ideas \
  -F web_domain=www.edmingle.academy \
  -F version_number=1.0.0 \
  -F platform=0 \
  -F build_type=2 \
  -F github_token=ghp_xxx \
  -F onepub_token=YOUR_ONEPUB_TOKEN \
  -F assets=@"/path/to/assets.zip" \
  -F play-track=internal \
  -F playstore-json=@"/path/to/play-service-account.json" \
  -F artifact_upload_url="https://BUCKET.s3.REGION.amazonaws.com/.../app.aab?X-Amz-..." \
  -F build_log_upload_url="https://BUCKET.s3.REGION.amazonaws.com/.../build.log?X-Amz-..." \
  -F artifact_content_type=application/octet-stream \
  -F build_log_content_type=text/plain \
  -F s3_storage_class=REDUCED_REDUNDANCY \
  -F artifact_filename=app-release.aab
```

`platform`: `0` = Android, `1` = iOS (only `0` is supported today).  
`build_type`: `1` = APK (build only), `2` = AAB **and** upload to Play Store.  
`github_token` / `onepub_token`: **required** on every request (not stored on the server).  
`artifact_upload_url` + `build_log_upload_url`: S3 presigned PUT URLs — worker uploads files to S3, then callbacks PHP with **JSONString only** (no APK/AAB in the callback).  
For `build_type=2`, `playstore-json` is required (Play Console service-account — **not** Firebase `google-services.json`).  
`play-track`: `internal` (default) | `alpha` | `beta` | `production`.

Response:

```json
{ "success": true, "status": "QUEUED", "build_id": "1001", "upload": true }
```

### `GET /build/{build_id}`

```json
{ "build_id": "101", "status": "SUCCESS" }
```

### `GET /health`

```json
{
  "status": "UP",
  "running_build": null,
  "queue_size": 0,
  "callback_enabled": true
}
```

## Post-build lifecycle

```text
build.sh → output/build_<id>/
   → write logs/build_<id>.log
   → discover artifact in that build dir only (no recursive scan)
   → POST callback (multipart)  [3 attempts]
   → if callback OK: delete output/build_<id>/ + build log
   → if callback failed: keep output + log for recovery
   → always delete workspace/build_<id>/
   → next queued job
```

Callback sends multipart with:
- `JSONString` — JSON metadata: `build_id`, `status` (`1`/`0`), `error_message`,
  `build_duration`, `platform`, `build_type`, `start_time`, `end_time`,
  optional `version_number`, `artifact_filename`, `s3_upload`
- When S3 URLs were used: **no** `artifact` / `build_log` file parts (already on S3)
- Legacy (no S3 URLs): `artifact` + `build_log` file parts

The callback runs for every build outcome (clone failure, validate failure,
flutter/fastlane failure, exceptions, and success).
