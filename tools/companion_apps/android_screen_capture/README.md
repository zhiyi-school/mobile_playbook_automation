# Android screen-capture helper

A minimal Android app that records the screen the way the playbook's malicious app does for `android-feature-06-risk-01`: it asks for consent through the system's screen-recording prompt, records through `MediaProjection` from a foreground service, and saves frames on request. A window with `FLAG_SECURE` comes out black in its recording, so the test can tell whether the app under test let its screen be captured.

## Build

```sh
python tools/companion_apps/android_screen_capture/build.py
```

The script uses the Android SDK's command-line tools (`aapt2`, `d8`, `zipalign`, `apksigner`, from the newest installed build-tools and platform) and a JDK (`javac`, `keytool`); Gradle is not needed. It finds the SDK through `ANDROID_HOME`, `ANDROID_SDK_ROOT`, or `~/Library/Android/sdk`. The APK is written next to the other companion apps, to `artifacts/companion/android/apks/screen-capture-helper.apk` (under `ARTIFACTS_DIR` when that is set); `--output` writes it elsewhere. It is signed with a key the script generates in `build/helper.keystore` on first use, which is git-ignored. Keep that keystore, so later builds install over the old one as upgrades.

The APK only needs building once per machine, or after the helper's source changes. The test installs it itself when the device lacks the current version, and it can be installed by hand with `adb install -r artifacts/companion/android/apks/screen-capture-helper.apk`. `screen_capture.helper_apk` in the Android config points the test at a different APK.

## How the test drives it

All commands go over adb; the package is `com.mobileplaybook.screencapture`.

| Step | Command | Result |
| --- | --- | --- |
| Start | `am start -S -n com.mobileplaybook.screencapture/.CaptureActivity` | Clears earlier output and shows the consent prompt; Appium chooses the entire screen when the prompt asks, then taps `android:id/button1` |
| Save a frame | `am broadcast -a com.mobileplaybook.screencapture.SNAP --es label <label> -p com.mobileplaybook.screencapture` | Writes `<label>.png` and `<label>.json` |
| Stop | `am broadcast -a com.mobileplaybook.screencapture.STOP -p com.mobileplaybook.screencapture` | Ends the recording and the service |

Output is written to `/sdcard/Android/data/com.mobileplaybook.screencapture/files/capture/`:

- `status.json`: `state` is `awaiting_consent`, `recording`, `denied`, `error` or `stopped`, with `detail` giving the reason.
- `<label>.json`: the frame's `width` and `height` (half the screen resolution), `dark_ratio` (share of near-black pixels in the middle 80% of the frame, leaving out the status and navigation bars), `distinct_colors` (capped at 4096), and `png`; or `error` when no frame could be saved.

## Changing it

Bump `android:versionName` in `AndroidManifest.xml` and `HELPER_VERSION` in `mobile_playbook/platforms/android/device/capture_helper.py` together, so devices with the old helper get the new build.
