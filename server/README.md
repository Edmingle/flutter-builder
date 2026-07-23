# Flutter Build Server — HTTP API (Stage 6)

Async single-build queue. After each build, the worker notifies the PHP
backend via multipart callback, then deletes `workspace/build_<id>/`.

## Run

```bash
cd /path/to/Script_for_flutter_builder

cp -n config/builder.example.json config/builder.json
# set flutter_repo, onepub_token, callback_base_url, callback_apikey

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
  "onepub_token": "...",
  "callback_base_url": "http://localhost/",
  "callback_apikey": "YOUR_API_KEY"
}
```

Only the **base URL** is configurable (`callback_base_url` or env
`CALLBACK_BASE_URL`). The path is fixed:

`{base}/nuSource/api/v1/support/mobilebuild/callback`

Env overrides JSON when both are set.

**GitHub auth** uses env var only (never in JSON):

```bash
export GITHUB_TOKEN=ghp_xxxxxxxxxxxxxxxxx
```

API key for callbacks is sent as header `X-API-Key`.

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
  -F build_type=1 \
  -F assets=@"/path/to/assets.zip"
```

`platform`: `0` = Android, `1` = iOS (only `0` is supported today).  
`build_type`: `1` = APK, `2` = AAB.

Response:

```json
{ "success": true, "status": "QUEUED", "build_id": "1001" }
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

Callback fields: `build_id`, `status` (`1` success / `0` failed),
`error_message` (failure text; empty on success), `build_duration`,
`start_time`, `end_time`, `platform` (`0` Android / `1` iOS),
`build_type` (`1` APK / `2` AAB)  
Files: `artifact` (success when available), `build_log` (**always** when available)

The callback runs for every build outcome (clone failure, validate failure,
flutter/fastlane failure, exceptions, and success).
