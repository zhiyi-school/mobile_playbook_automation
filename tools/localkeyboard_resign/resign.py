#!/usr/bin/env python3
"""Renew LocalKeyboard.ipa's provisioning profiles and re-sign it in place.

Free/individual Apple Developer accounts only get 7-day provisioning
profiles, and LocalKeyboard.ipa has no Xcode project in this repo to rebuild
from. This instead builds the placeholder project in this same directory
(project.yml, matching LocalKeyboard's real bundle IDs and entitlements) to
make Xcode mint fresh profiles, pulls those profiles and a matching signing
identity out of the build output, and reapplies them directly to the
existing prebuilt LocalKeyboard.ipa via codesign — no LocalKeyboard source
required. See docs/ios/reports-and-troubleshooting.md for background.

Usage:
    python tools/localkeyboard_resign/resign.py --udid <device.udid> --team-id <device.team_id>
"""

from __future__ import annotations

import argparse
import json
import plistlib
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
APP_BUNDLE_ID = "com.example.LocalKeyboard.4228qcqtj9"
EXTENSION_BUNDLE_ID = "com.example.LocalKeyboard.4228qcqtj9.KeyboardExtension"


def run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    result = subprocess.run(cmd, capture_output=True, text=True, **kwargs)
    if result.returncode != 0:
        raise RuntimeError(f"command failed: {' '.join(cmd)}\n{result.stdout}\n{result.stderr}")
    return result


def decode_profile(profile_path: Path) -> dict:
    result = run(["security", "cms", "-D", "-i", str(profile_path)])
    return plistlib.loads(result.stdout.encode())


def renew_profiles(udid: str, team_id: str) -> None:
    run(["xcodegen", "generate"], cwd=HERE)
    run(
        [
            "xcodebuild",
            "-project", "LocalKeyboardResign.xcodeproj",
            "-scheme", "LocalKeyboard",
            "-destination", f"id={udid}",
            "-allowProvisioningUpdates",
            f"DEVELOPMENT_TEAM={team_id}",
            "build",
        ],
        cwd=HERE,
    )


def build_products_dir() -> Path:
    result = run(
        [
            "xcodebuild",
            "-project", "LocalKeyboardResign.xcodeproj",
            "-scheme", "LocalKeyboard",
            "-showBuildSettings",
            "-json",
        ],
        cwd=HERE,
    )
    settings = json.loads(result.stdout)[0]["buildSettings"]
    return Path(settings["BUILT_PRODUCTS_DIR"])


def find_signing_identity(team_id: str) -> str:
    result = run(["security", "find-identity", "-v", "-p", "codesigning"])
    for line in result.stdout.splitlines():
        line = line.strip()
        if "Apple Development:" not in line:
            continue
        name = line.split('"')[1]
        cert = run(["security", "find-certificate", "-c", name, "-p"]).stdout
        subject = run(["openssl", "x509", "-noout", "-subject"], input=cert).stdout
        if f"OU={team_id}" in subject:
            return name
    raise RuntimeError(f"no 'Apple Development' identity in the keychain has team (OU) {team_id}")

def entitlements_plist_bytes(profile: dict) -> bytes:
    return plistlib.dumps(profile["Entitlements"])


def resign(ipa_path: Path, out_path: Path, udid: str, team_id: str) -> None:
    print(f"Renewing profiles for {APP_BUNDLE_ID} / {EXTENSION_BUNDLE_ID} (team {team_id})...")
    renew_profiles(udid, team_id)
    products = build_products_dir()
    app_profile_path = products / "LocalKeyboard.app" / "embedded.mobileprovision"
    ext_profile_path = products / "LocalKeyboard.app" / "PlugIns" / "KeyboardExtension.appex" / "embedded.mobileprovision"
    app_profile = decode_profile(app_profile_path)
    ext_profile = decode_profile(ext_profile_path)
    print(f"  app profile expires {app_profile['ExpirationDate']}")
    print(f"  extension profile expires {ext_profile['ExpirationDate']}")

    identity = find_signing_identity(team_id)
    print(f"Signing with: {identity}")

    work_dir = Path.cwd() / ".localkeyboard_resign_work"
    if work_dir.exists():
        shutil.rmtree(work_dir)
    work_dir.mkdir(parents=True)
    with zipfile.ZipFile(ipa_path) as zf:
        zf.extractall(work_dir)

    app_dir = next((work_dir / "Payload").glob("*.app"))
    ext_dir = app_dir / "PlugIns" / "KeyboardExtension.appex"
    if not ext_dir.is_dir():
        ext_dir = next(app_dir.glob("PlugIns/*.appex"))

    (ext_dir / "embedded.mobileprovision").write_bytes(ext_profile_path.read_bytes())
    (app_dir / "embedded.mobileprovision").write_bytes(app_profile_path.read_bytes())

    ext_entitlements = work_dir / "extension.entitlements.plist"
    app_entitlements = work_dir / "app.entitlements.plist"
    ext_entitlements.write_bytes(entitlements_plist_bytes(ext_profile))
    app_entitlements.write_bytes(entitlements_plist_bytes(app_profile))

    # Extension first, then the app — codesign validates a signed app's
    # nested PlugIns against the app's own signature, so the inner bundle
    # must already carry a valid signature before the outer one is applied.
    run(["codesign", "--force", "--sign", identity, "--entitlements", str(ext_entitlements), str(ext_dir)])
    run(["codesign", "--force", "--sign", identity, "--entitlements", str(app_entitlements), str(app_dir)])

    run(["codesign", "--verify", "--deep", "--strict", str(app_dir)])
    print("codesign --verify passed.")

    if out_path.exists():
        out_path.unlink()
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for file in work_dir.rglob("*"):
            if file.is_file():
                zf.write(file, file.relative_to(work_dir))
    shutil.rmtree(work_dir)
    print(f"Resigned IPA written to {out_path}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ipa", default="intake/ios/ipas/LocalKeyboard.ipa", type=Path)
    parser.add_argument("--out", default=None, type=Path, help="defaults to overwriting --ipa")
    parser.add_argument("--udid", required=True, help="device.udid from configs/ios.yaml")
    parser.add_argument("--team-id", required=True, help="device.team_id from configs/ios.yaml")
    args = parser.parse_args()

    if not args.ipa.exists():
        print(f"error: {args.ipa} not found", file=sys.stderr)
        return 1
    out_path = args.out or args.ipa
    resign(args.ipa, out_path, args.udid, args.team_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
