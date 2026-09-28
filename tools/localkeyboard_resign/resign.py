#!/usr/bin/env python3
"""Renew the provisioning profiles of a companion IPA and re-sign it.

Works for any development-signed companion app (LocalKeyboard, ReplayConsentRecorder):
the bundle IDs and App Groups are read from the IPA itself, a placeholder project with
the same identifiers mints fresh profiles, and those profiles are applied to the IPA.

See docs/ios/reports-and-troubleshooting.md.
"""

from __future__ import annotations

import argparse
import json
import plistlib
import shutil
import subprocess
import sys
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent
APP_SOURCE = HERE / "App" / "App.swift"
EXTENSION_SOURCE = HERE / "Extension" / "KeyboardViewController.swift"
APP_GROUPS_KEY = "com.apple.security.application-groups"
PROJECT_NAME = "CompanionResign"
APP_TARGET = "PlaceholderApp"


@dataclass(frozen=True)
class SignedBundle:
    path: Path
    bundle_id: str
    app_groups: list[str]


def run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    result = subprocess.run(cmd, capture_output=True, text=True, **kwargs)
    if result.returncode != 0:
        raise RuntimeError(f"command failed: {' '.join(cmd)}\n{result.stdout}\n{result.stderr}")
    return result


def decode_profile(profile_path: Path) -> dict:
    result = run(["security", "cms", "-D", "-i", str(profile_path)])
    return plistlib.loads(result.stdout.encode())


def signed_app_groups(bundle: Path) -> list[str]:
    signed = subprocess.run(
        ["codesign", "-d", "--entitlements", "-", "--xml", str(bundle)], capture_output=True
    )
    if signed.returncode == 0 and signed.stdout.strip():
        return list(plistlib.loads(signed.stdout).get(APP_GROUPS_KEY) or [])
    profile = bundle / "embedded.mobileprovision"
    if profile.exists():
        return list((decode_profile(profile).get("Entitlements") or {}).get(APP_GROUPS_KEY) or [])
    return []


def read_bundle(bundle: Path) -> SignedBundle:
    info = plistlib.loads((bundle / "Info.plist").read_bytes())
    return SignedBundle(bundle, str(info["CFBundleIdentifier"]), signed_app_groups(bundle))


def _target(bundle: SignedBundle, kind: str, source_dir: str, team_id: str) -> dict:
    target: dict = {
        "type": kind,
        "platform": "iOS",
        "deploymentTarget": "15.0",
        "sources": [source_dir],
        "settings": {
            "base": {
                "PRODUCT_BUNDLE_IDENTIFIER": bundle.bundle_id,
                "DEVELOPMENT_TEAM": team_id,
                "CODE_SIGN_STYLE": "Automatic",
                "CODE_SIGN_IDENTITY": "Apple Development",
            }
        },
    }
    if bundle.app_groups:
        target["entitlements"] = {
            "path": f"{source_dir}/{source_dir}.entitlements",
            "properties": {APP_GROUPS_KEY: bundle.app_groups},
        }
    return target


def write_placeholder_project(project_dir: Path, app: SignedBundle, extensions: list[SignedBundle], team_id: str) -> None:
    """Profiles don't depend on the extension type, so the keyboard placeholder stands in for every extension."""
    (project_dir / "App").mkdir(parents=True)
    shutil.copy(APP_SOURCE, project_dir / "App" / APP_SOURCE.name)
    app_target = _target(app, "application", "App", team_id)
    app_target["settings"]["base"]["GENERATE_INFOPLIST_FILE"] = "YES"
    app_target["dependencies"] = [{"target": f"Extension{index}", "embed": True} for index in range(len(extensions))]
    targets = {APP_TARGET: app_target}
    for index, extension in enumerate(extensions):
        source_dir = f"Extension{index}"
        (project_dir / source_dir).mkdir()
        shutil.copy(EXTENSION_SOURCE, project_dir / source_dir / EXTENSION_SOURCE.name)
        target = _target(extension, "app-extension", source_dir, team_id)
        target["info"] = {
            "path": f"{source_dir}/Info.plist",
            "properties": {
                "NSExtension": {
                    "NSExtensionPointIdentifier": "com.apple.keyboard-service",
                    "NSExtensionPrincipalClass": "$(PRODUCT_MODULE_NAME).KeyboardViewController",
                    "NSExtensionAttributes": {
                        "IsASCIICapable": False,
                        "PrefersRightToLeft": False,
                        "PrimaryLanguage": "en-US",
                        "RequestsOpenAccess": True,
                    },
                }
            },
        }
        targets[source_dir] = target
    spec = {"name": PROJECT_NAME, "options": {"createIntermediateGroups": True}, "targets": targets}
    # XcodeGen reads JSON through its YAML loader.
    (project_dir / "project.yml").write_text(json.dumps(spec, indent=2))


def renew_profiles(project_dir: Path, udid: str, team_id: str) -> Path:
    run(["xcodegen", "generate"], cwd=project_dir)
    derived = project_dir / "DerivedData"
    run(
        [
            "xcodebuild",
            "-project", f"{PROJECT_NAME}.xcodeproj",
            "-scheme", APP_TARGET,
            "-destination", f"id={udid}",
            "-derivedDataPath", str(derived),
            "-allowProvisioningUpdates",
            f"DEVELOPMENT_TEAM={team_id}",
            "build",
        ],
        cwd=project_dir,
    )
    return derived / "Build" / "Products" / "Debug-iphoneos" / f"{APP_TARGET}.app"


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


def apply_profile(bundle: Path, profile_path: Path, identity: str, entitlements_path: Path) -> None:
    profile = decode_profile(profile_path)
    print(f"  {bundle.name}: profile expires {profile['ExpirationDate']}")
    (bundle / "embedded.mobileprovision").write_bytes(profile_path.read_bytes())
    entitlements_path.write_bytes(plistlib.dumps(profile["Entitlements"]))
    run(["codesign", "--force", "--sign", identity, "--entitlements", str(entitlements_path), str(bundle)])


def resign(ipa_path: Path, out_path: Path, udid: str, team_id: str) -> None:
    with tempfile.TemporaryDirectory(prefix="companion_resign_") as tmp:
        work_dir = Path(tmp)
        extract_dir = work_dir / "ipa"
        with zipfile.ZipFile(ipa_path) as zf:
            zf.extractall(extract_dir)
        app_dir = next((extract_dir / "Payload").glob("*.app"))
        app = read_bundle(app_dir)
        extensions = [read_bundle(path) for path in sorted(app_dir.glob("PlugIns/*.appex"))]

        ids = " / ".join([app.bundle_id] + [extension.bundle_id for extension in extensions])
        print(f"Renewing profiles for {ids} (team {team_id})...")
        project_dir = work_dir / "project"
        write_placeholder_project(project_dir, app, extensions, team_id)
        placeholder_app = renew_profiles(project_dir, udid, team_id)

        identity = find_signing_identity(team_id)
        print(f"Signing with: {identity}")

        # Extensions first, then the app — codesign validates a signed app's
        # nested PlugIns against the app's own signature, so the inner bundles
        # must already carry a valid signature before the outer one is applied.
        for index, extension in enumerate(extensions):
            apply_profile(
                extension.path,
                placeholder_app / "PlugIns" / f"Extension{index}.appex" / "embedded.mobileprovision",
                identity,
                work_dir / f"extension{index}.entitlements.plist",
            )
        apply_profile(app_dir, placeholder_app / "embedded.mobileprovision", identity, work_dir / "app.entitlements.plist")

        run(["codesign", "--verify", "--deep", "--strict", str(app_dir)])
        print("codesign --verify passed.")

        if out_path.exists():
            out_path.unlink()
        with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for file in extract_dir.rglob("*"):
                if file.is_file():
                    zf.write(file, file.relative_to(extract_dir))
    print(f"Resigned IPA written to {out_path}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ipa", default="artifacts/companion/ios/ipas/LocalKeyboard.ipa", type=Path)
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
