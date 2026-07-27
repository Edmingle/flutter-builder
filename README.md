# Flutter Build Server

On-prem **build server** for white-label Flutter Android apps.

> **Stage 1** — layout + configurable paths  
> **Stage 2** — Git (`prepare_repo.sh`)  
> **Stage 3** — Assets (`prepare_assets.sh`) + common `mobilertc`  
> **Stage 4** — HTTP API `POST /build`  
> **Stage 5** — Background queue (one build at a time) + `GET /build/{id}`  
> **Stage 6** — Backend callback + workspace cleanup  

---

## Layout

```text
flutter-builder/
├── build.sh
├── prepare_repo.sh
├── prepare_assets.sh
├── validate.sh
├── prepare.sh
├── replace_config.py
├── lib/common.sh
├── common/
│   └── mobilertc/          ← Build Server–owned (NOT in assets.zip)
├── server/                 ← FastAPI POST /build
├── workspace/
│   └── build_<id>/
│       ├── flutter-app/
│       ├── assets/         ← from assets.zip (portal files only)
│       └── assets.zip
├── output/
├── logs/
└── config/
```

### assets.zip (portal only)

- `logo.png`
- `google-services.json`
- `edmingleKey.jks`
- `key.properties`

`mobilertc/` is **not** in the zip — it lives in `common/mobilertc/`.

---

## Config (`config/builder.json`)

```bash
cp config/builder.example.json config/builder.json
```

```json
{
  "flutter_repo": "https://github.com/company/flutter-app.git",
  "workspace_root": "workspace",
  "common_dir": "common",
  "callback_base_url": "http://localhost/"
}
```

Tokens (`github_token`, `onepub_token`) are sent by cron on each `POST /build` — not stored here.

Also for local CLI only:

```bash
export GITHUB_TOKEN=ghp_xxxxxxxxxxxxxxxxx
```

---

## CLI build

```bash
./build.sh \
    --build-id 101 \
    --branch develop \
    --assets-zip /path/to/assets.zip \
    --app-name "Ideas" \
    --bundle-id "com.edmingle.ideas" \
    --portal-name "ideas" \
    --web-domain "www.edmingle.academy" \
    --app-version "1.0.1" \
    --build-type 1 \
    --onepub-token "YOUR_ONEPUB_TOKEN"
```

(`GITHUB_TOKEN` must be in the environment for CLI.)

---

## HTTP API (Stage 6)

```bash
python3 -m venv server/.venv
source server/.venv/bin/activate
pip install -r server/requirements.txt
uvicorn server.app:app --host 0.0.0.0 --port 8080
```

```bash
# APK only (build_type=1)
curl -X POST http://127.0.0.1:8080/build \
  -F build_id=101 \
  -F institution_id=55 \
  -F branch=develop \
  -F app_name=Ideas \
  -F bundle_id=com.edmingle.ideas \
  -F portal_name=ideas \
  -F web_domain=www.edmingle.academy \
  -F version_number=1.0.1 \
  -F platform=0 \
  -F build_type=1 \
  -F github_token=ghp_xxx \
  -F onepub_token=YOUR_ONEPUB_TOKEN \
  -F assets=@/path/to/assets.zip

# AAB + Play Store upload (build_type=2)
curl -X POST http://127.0.0.1:8080/build \
  -F build_id=102 \
  -F institution_id=55 \
  -F branch=develop \
  -F app_name=Ideas \
  -F bundle_id=com.edmingle.ideas \
  -F portal_name=ideas \
  -F web_domain=www.edmingle.academy \
  -F version_number=1.0.1 \
  -F platform=0 \
  -F build_type=2 \
  -F github_token=ghp_xxx \
  -F onepub_token=YOUR_ONEPUB_TOKEN \
  -F assets=@/path/to/assets.zip \
  -F play-track=internal \
  -F playstore-json=@/path/to/play-service-account.json

curl http://127.0.0.1:8080/build/101
curl http://127.0.0.1:8080/health
```

See `server/README.md` for callback payload details.

---

## Out of scope (later)

Artifact CDN upload beyond callback, Redis/DB, auth, parallel workers, Docker,
metrics (Stage 7 docs/cleanup polish).
