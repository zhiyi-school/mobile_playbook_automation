# mobile-playbook-automation

`mobile-playbook-automation` is a local test framework for running mobile security playbook checks against iOS IPA artifacts, iOS devices, and Android devices.

It uses platform-prefixed risk IDs:

| Risk ID | Platform | Purpose |
| --- | --- | --- |
| `ios-feature1-risk1` | iOS | IPA acquisition and static-analysis exposure |
| `ios-feature-04-risk-01` | iOS | Custom keyboard keystroke collection |
| `android-feature1-risk2` | Android | APK repackaging, resigning, reinstall, and launch validation |
| `android-feature6-risk1` | Android | Screen recording / `FLAG_SECURE` capture blocking |

## Scope

This tool is for local security testing on your own workstation and authorized test devices. It does not decrypt App Store binaries, bypass FairPlay, require jailbreak/root behavior, read protected app containers directly, or redistribute app packages.

If a package cannot be inspected, installed, launched, or modified because of platform restrictions, the framework reports that as a test outcome.

## Requirements

- Python 3.11+
- Appium server
- iOS: Xcode, XCUITest driver, physical iPhone, and optionally MobSF
- Android: Android platform-tools / `adb`, UiAutomator2 Appium support
- Android repackaging: `apktool`, `apksigner`, and `keytool`

Install the package:

```bash
python -m pip install -e .
```

Start Appium in a separate terminal:

```bash
appium
```

## Configuration

This project's own configs write `device`/`runner` inline in each platform's entry-point file (there's only ever one device and one runner profile per project), and split the app roster and per-risk settings — which don't fit comfortably in one file — into their own files under `configs/split/<platform>/`. Set it up from the tracked examples:

```bash
cp configs/ios.example.yaml configs/ios.yaml
cp configs/split/ios/apps.example.yaml configs/split/ios/apps.yaml
for f in ipa_static_analysis keystroke_collection; do cp configs/split/ios/risk_settings.example.yaml "configs/split/ios/$f.yaml"; done

cp configs/android.example.yaml configs/android.yaml
cp configs/split/android/apps.example.yaml configs/split/android/apps.yaml
for f in tools repackaging screen_capture; do cp configs/split/android/risk_settings.example.yaml "configs/split/android/$f.yaml"; done
```

Each `risk_settings.example.yaml` shows every risk's global settings together in one file for easier reading; trim each real copy above down to just its own top-level key (`ipa_static_analysis:`, `keystroke_collection:`, `tools:`, `repackaging:`, `screen_capture:`).

Use `configs/ios.yaml` for iOS apps, IPA paths, signing, Appium, and enabled iOS risks. Put IPAs under `intake/ios/ipas/` or another local path.

Use `configs/android.yaml` for Android package names, ADB/Appium settings, and enabled Android risks.

For local API keys such as `MOBSF_API_KEY`, copy `.env.example` to `.env`. Values exported in your shell take precedence over `.env`.

More detail lives in [docs/ios/configuration.md](docs/ios/configuration.md) and [docs/android/configuration.md](docs/android/configuration.md).

## Common Commands

Validate configs:

```bash
python -m mobile_playbook validate --platform ios --config configs/ios.yaml
python -m mobile_playbook validate --platform android --config configs/android.yaml
```

List risks:

```bash
python -m mobile_playbook list-risks --platform ios
python -m mobile_playbook list-risks --platform android
```

Run iOS risks:

```bash
python -m mobile_playbook run --platform ios --config configs/ios.yaml --risks ios-feature1-risk1 --out reports
python -m mobile_playbook run --platform ios --config configs/ios.yaml --risks ios-feature-04-risk-01 --out reports
```

Run one iOS app:

```bash
python -m mobile_playbook run --platform ios --config configs/ios.yaml --apps sp --risks ios-feature1-risk1 --out reports
```

Run Android risks:

```bash
python -m mobile_playbook run --platform android --config configs/android.yaml --risks android-feature6-risk1 --out reports
python -m mobile_playbook run --platform android --config configs/android.yaml --risks android-feature1-risk2 --out reports
```

Dry run:

```bash
python -m mobile_playbook run --platform ios --config configs/ios.yaml --risks ios-feature1-risk1 --dry-run --out reports
python -m mobile_playbook run --platform android --config configs/android.yaml --risks android-feature6-risk1 --dry-run --out reports
```

Run both platforms in one command:

```bash
python -m mobile_playbook run-all --ios-config configs/ios.yaml --android-config configs/android.yaml --apps parking,lifesg --out reports
```

`run-all` runs the iOS and Android `run` flows concurrently in one process (Appium/adb/network calls are I/O-bound, so a thread per platform is enough). It is additive on top of `run` — nothing about single-platform `run` changes. `--apps`/`--risks` are applied to both configs, and each platform still writes its own `reports/<run_timestamp>/ios/...` or `.../android/...` tree exactly as it would from a standalone `run`, so results are never merged. If both platforms happen to start in the same second, they may share one `<run_timestamp>` folder (their per-app/per-risk reports still land in separate `ios/`/`android/` subfolders either way); the only thing that can then race is which platform's top-level `dashboard_results.json` is written last.

Acquire iOS artifacts only:

```bash
python -m mobile_playbook acquire --config configs/ios.yaml --apps sp --out work/ios/acquired
```

Inspect IPA mutability:

```bash
python -m mobile_playbook inspect-ipa --ipa work/ios/acquired/<run_timestamp>/<app_id>/<timestamp>-original.ipa
```

## Repository Layout

```text
mobile_playbook/       Python package
configs/               iOS and Android YAML configs
intake/ios/ipas/       local IPA drop-zone
intake/android/apks/   local APK drop-zone for future APK-intake flows
work/ios/              generated iOS working files
work/android/          generated Android working files
reports/               timestamped run reports
data/                  future dashboard database location
```

Dashboard code is not implemented yet. Reports already include `dashboard_results.json` as a future dashboard feed.

## Documentation

Docs are split by platform under [docs/](docs/README.md):

- **iOS** ([docs/ios/](docs/ios/README.md)): [Configuration](docs/ios/configuration.md), [Risk Catalog](docs/ios/risks.md), [Manual LocalKeyboard Server](docs/ios/manual-local-keyboard-server.md), [Reports And Troubleshooting](docs/ios/reports-and-troubleshooting.md).
- **Android** ([docs/android/](docs/android/README.md)): [Configuration](docs/android/configuration.md), [Risk Catalog](docs/android/risks.md), [Reports And Troubleshooting](docs/android/reports-and-troubleshooting.md).
- **Architecture** ([docs/architecture.md](docs/architecture.md)): how a run is executed end-to-end and what libraries/tools each part of the framework depends on, across both platforms.
