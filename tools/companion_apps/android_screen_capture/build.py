"""
Builds and signs the screen-capture helper APK with the Android SDK's command-line tools, without Gradle.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPOSITORY_ROOT = HERE.parents[2]
# Build intermediates and the signing key stay here; only the APK goes to the companion apps folder.
BUILD_DIR = HERE / "build"
MIN_SDK = 26
TARGET_SDK = 33
KEY_ALIAS = "screencapture"
# The helper's signing key only has to stay the same between builds so reinstalls are upgrades; it guards nothing.
KEY_PASSWORD = "screencapture"


# Return the companion apps folder's APK path, which the screen capture test installs from by default.
def default_output() -> Path:
    sys.path.insert(0, str(REPOSITORY_ROOT))
    from mobile_playbook.platforms.android.device.capture_helper import default_helper_apk

    return default_helper_apk()


# Return the Android SDK root from ANDROID_HOME, ANDROID_SDK_ROOT or the default macOS location.
def sdk_root() -> Path:
    for name in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        if os.environ.get(name):
            return Path(os.environ[name])
    return Path.home() / "Library" / "Android" / "sdk"


# Return the newest installed entry under an SDK folder such as build-tools or platforms.
def newest(folder: Path) -> Path:
    entries = sorted((entry for entry in folder.iterdir() if entry.is_dir()), key=lambda entry: entry.name)
    if not entries:
        raise SystemExit(f"Nothing installed under {folder}; install it with the Android SDK manager.")
    return entries[-1]


# Run a build command, stopping the build with its output when it fails.
def run(command: list[str]) -> None:
    completed = subprocess.run(command, capture_output=True, text=True)
    if completed.returncode != 0:
        raise SystemExit(f"{command[0]} failed:\n{completed.stdout}{completed.stderr}")


# Compile the Java sources, convert them to dex, package them with the manifest, then align and sign the APK.
def build(output: Path) -> Path:
    sdk = sdk_root()
    if not sdk.is_dir():
        raise SystemExit(f"Android SDK not found at {sdk}; set ANDROID_HOME.")
    build_tools = newest(sdk / "build-tools")
    android_jar = newest(sdk / "platforms") / "android.jar"
    work = BUILD_DIR / "intermediates"
    shutil.rmtree(work, ignore_errors=True)
    classes = work / "classes"
    dex = work / "dex"
    classes.mkdir(parents=True)
    dex.mkdir()

    sources = sorted(str(path) for path in (HERE / "src").rglob("*.java"))
    run(["javac", "--release", "11", "-classpath", str(android_jar), "-d", str(classes), *sources])
    run([str(build_tools / "d8"), "--min-api", str(MIN_SDK), "--lib", str(android_jar), "--output", str(dex), *map(str, classes.rglob("*.class"))])

    unsigned = work / "unsigned.apk"
    run([
        str(build_tools / "aapt2"), "link", "-o", str(unsigned), "-I", str(android_jar),
        "--manifest", str(HERE / "AndroidManifest.xml"),
        "--min-sdk-version", str(MIN_SDK), "--target-sdk-version", str(TARGET_SDK),
    ])
    with zipfile.ZipFile(unsigned, "a") as apk:
        apk.write(dex / "classes.dex", "classes.dex")
    aligned = work / "aligned.apk"
    run([str(build_tools / "zipalign"), "-f", "4", str(unsigned), str(aligned)])

    keystore = BUILD_DIR / "helper.keystore"
    if not keystore.exists():
        run([
            "keytool", "-genkeypair", "-keystore", str(keystore), "-alias", KEY_ALIAS, "-keyalg", "RSA", "-keysize", "2048",
            "-validity", "10000", "-storepass", KEY_PASSWORD, "-keypass", KEY_PASSWORD, "-dname", "CN=Playbook Screen Capture",
        ])
    output.parent.mkdir(parents=True, exist_ok=True)
    run([
        str(build_tools / "apksigner"), "sign", "--ks", str(keystore), "--ks-key-alias", KEY_ALIAS,
        "--ks-pass", f"pass:{KEY_PASSWORD}", "--key-pass", f"pass:{KEY_PASSWORD}",
        # No v4 signature: it is only for incremental installs and would leave a .idsig file next to the APK.
        "--v4-signing-enabled", "false", "--out", str(output), str(aligned),
    ])
    shutil.rmtree(work, ignore_errors=True)
    return output


# Parse the optional output path and build the APK there.
def main() -> int:
    parser = argparse.ArgumentParser(description="Build the screen-capture helper APK for android-feature-06-risk-01.")
    parser.add_argument("--output", type=Path, help="where to write the APK (default: artifacts/companion/android/apks/screen-capture-helper.apk)")
    args = parser.parse_args()
    print(f"Built {build((args.output or default_output()).resolve())}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
