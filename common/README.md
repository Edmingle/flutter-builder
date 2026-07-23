# Build Server common assets (not per-portal, not from assets.zip)

Place MobileRTC here:

```text
common/
└── mobilertc/
    ├── build.gradle
    └── mobilertc.aar
```

`prepare.sh` copies this into each build's `flutter-app/android/mobilertc`.
