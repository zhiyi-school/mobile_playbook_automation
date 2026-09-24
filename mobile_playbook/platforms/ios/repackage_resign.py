from __future__ import annotations

import hashlib
import plistlib
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from mobile_playbook.storage import resolve_under_repository

DEFAULT_PROFILE_DIR = "~/Library/Developer/Xcode/UserData/Provisioning Profiles/"
REPACK_PROVISION_DIR = "tools/repack_provision"


@dataclass(frozen=True)
class ResignResult:
    status: str
    errors: list[str] = field(default_factory=list)


def _decode_profile(profile_path: Path) -> dict:
    decoded = subprocess.run(
        ["security", "cms", "-D", "-i", str(profile_path)],
        check=True,
        capture_output=True,
    )
    return plistlib.loads(decoded.stdout)


def _profile_compatible(profile: dict, bundle_id: str, team_id: str, udid: str, identity: str, margin_seconds: int = 0) -> bool:
    try:
        entitlements = profile["Entitlements"]
        expiry = profile["ExpirationDate"].replace(tzinfo=timezone.utc)
    except (KeyError, AttributeError):
        return False
    return (
        team_id in profile.get("TeamIdentifier", [])
        and entitlements.get("application-identifier") == f"{team_id}.{bundle_id}"
        and udid in profile.get("ProvisionedDevices", [])
        and expiry > datetime.now(timezone.utc) + timedelta(seconds=margin_seconds)
        and any(
            hashlib.sha1(cert).hexdigest().upper() == identity.upper()
            for cert in profile.get("DeveloperCertificates", [])
        )
    )


def prepare_profile(app_dir: Path, profile_path: Path, bundle_id: str, team_id: str, udid: str, identity: str, work_dir: Path) -> Path:
    """Validate the profile against this app/device/signer, embed it, and return its entitlements plist."""
    profile = _decode_profile(profile_path)
    if not _profile_compatible(profile, bundle_id, team_id, udid, identity):
        raise ValueError("Provisioning profile is incompatible with this app, device, or signer")
    shutil.copy2(profile_path, Path(app_dir) / "embedded.mobileprovision")
    output = Path(work_dir) / "entitlements.plist"
    output.write_bytes(plistlib.dumps(profile["Entitlements"]))
    return output


def discover_provisioning_profile(bundle_id: str, team_id: str, udid: str, identity: str, search_dir: str | None = None, margin_seconds: int = 0) -> Path | None:
    """First locally installed profile that matches this app, device, and signer, or None."""
    search = Path(search_dir).expanduser() if search_dir else Path(DEFAULT_PROFILE_DIR).expanduser()
    if not search.is_dir():
        return None
    for path in sorted(search.glob("*.mobileprovision")):
        try:
            profile = _decode_profile(path)
        except Exception:
            continue
        if _profile_compatible(profile, bundle_id, team_id, udid, identity, margin_seconds):
            return path
    return None


def generate_provisioning_profile(bundle_id: str, team_id: str, udid: str) -> None:
    project_dir = resolve_under_repository(REPACK_PROVISION_DIR)
    commands = [
        ["xcodegen", "generate"],
        [
            "xcodebuild",
            "-project", "RepackProvision.xcodeproj",
            "-scheme", "RepackProvision",
            "-destination", f"id={udid}",
            "-allowProvisioningUpdates",
            "-allowProvisioningDeviceRegistration",
            f"DEVELOPMENT_TEAM={team_id}",
            f"PRODUCT_BUNDLE_IDENTIFIER={bundle_id}",
            "build",
        ],
    ]
    for cmd in commands:
        completed = subprocess.run(cmd, cwd=project_dir, capture_output=True, text=True)
        if completed.returncode != 0:
            raise RuntimeError(f"{cmd[0]} failed: {completed.stderr.strip() or completed.stdout.strip()}")


def ensure_provisioning_profile(
    bundle_id: str,
    team_id: str,
    udid: str,
    identity: str,
    margin_seconds: int = 3600,
    generate: bool = True,
) -> Path | None:
    """A valid (not expiring within margin) profile for bundle_id, generating/renewing if needed."""
    existing = discover_provisioning_profile(bundle_id, team_id, udid, identity, margin_seconds=margin_seconds)
    if existing is not None:
        return existing
    if not generate:
        return None
    generate_provisioning_profile(bundle_id, team_id, udid)  # raises on failure
    return discover_provisioning_profile(bundle_id, team_id, udid, identity, margin_seconds=margin_seconds)


def signing_identity_for_team(team_id: str) -> str:
    """SHA-1 of the valid Apple Development identity for this team; names are not unique."""
    listing = subprocess.run(["security", "find-identity", "-v", "-p", "codesigning"], capture_output=True, text=True).stdout
    for line in listing.splitlines():
        parts = line.strip().split('"')
        if len(parts) < 2 or not parts[1].startswith("Apple Development:"):
            continue
        sha1 = parts[0].split()[1]
        pem = subprocess.run(["security", "find-certificate", "-a", "-Z", "-p"], capture_output=True, text=True).stdout
        block = pem.split(f"SHA-1 hash: {sha1}", 1)[-1].split("-----END CERTIFICATE-----", 1)[0] + "-----END CERTIFICATE-----"
        subject = subprocess.run(["openssl", "x509", "-noout", "-subject"], input=block[block.find("-----BEGIN"):], capture_output=True, text=True).stdout
        if f"OU={team_id}" in subject:
            return sha1
    raise RuntimeError(f"no valid Apple Development identity in the keychain has team (OU) {team_id}")


def _codesign(identity: str, target: Path, entitlements: Path | None = None) -> subprocess.CompletedProcess:
    command = ["codesign", "--force", "--sign", identity]
    if entitlements is not None:
        command += ["--entitlements", str(entitlements)]
    command.append(str(target))
    return subprocess.run(command, capture_output=True, text=True)


def _nested_signables(app_dir: Path) -> list[Path]:
    seen: list[Path] = []
    for pattern in ("Frameworks/*.dylib", "Frameworks/*.framework", "PlugIns/*.appex", "**/*.dylib"):
        for path in sorted(app_dir.glob(pattern)):
            if path not in seen and path != app_dir:
                seen.append(path)
    return seen


def resign_app(
    app_dir: Path,
    identity: str,
    team_id: str,
    profile_path: Path,
    bundle_id: str,
    udid: str,
    work_dir: Path,
) -> ResignResult:
    """Embed the validated profile, sign nested code first, then the bundle, then verify the whole tree."""
    app_dir = Path(app_dir)
    try:
        entitlements = prepare_profile(app_dir, Path(profile_path), bundle_id, team_id, udid, identity, work_dir)
    except Exception as exc:
        return ResignResult("RESIGN_FAILED", [str(exc)])
    for nested in _nested_signables(app_dir):
        completed = _codesign(identity, nested)
        if completed.returncode != 0:
            return ResignResult("RESIGN_FAILED", [completed.stderr.strip() or completed.stdout.strip() or f"codesign failed for {nested.name}"])
    completed = _codesign(identity, app_dir, entitlements=entitlements)
    if completed.returncode != 0:
        return ResignResult("RESIGN_FAILED", [completed.stderr.strip() or completed.stdout.strip() or "codesign failed for the app bundle"])
    verify = subprocess.run(["codesign", "--verify", "--deep", "--strict", str(app_dir)], capture_output=True, text=True)
    if verify.returncode != 0:
        return ResignResult("RESIGN_FAILED", [verify.stderr.strip() or verify.stdout.strip() or "codesign verification failed"])
    return ResignResult("RESIGNED")
