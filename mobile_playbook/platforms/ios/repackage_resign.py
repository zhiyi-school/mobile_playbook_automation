"""
Provisioning-profile selection and codesign-based resigning of repackaged iOS apps.
"""

from __future__ import annotations

import hashlib
import logging
import plistlib
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from mobile_playbook.storage import resolve_under_repository

DEFAULT_PROFILE_DIR = "~/Library/Developer/Xcode/UserData/Provisioning Profiles/"
REPACK_PROVISION_DIR = "tools/repack_provision"

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ResignResult:
    status: str
    errors: list[str] = field(default_factory=list)


# Decode a provisioning profile with security cms and return its plist contents.
def _decode_profile(profile_path: Path) -> dict:
    logger.debug("ios resign: decoding provisioning profile %s", profile_path)
    started = time.monotonic()
    decoded = subprocess.run(
        ["security", "cms", "-D", "-i", str(profile_path)],
        check=True,
        capture_output=True,
    )
    logger.debug("ios resign: security cms decoded %s in %.2fs", profile_path, time.monotonic() - started)
    return plistlib.loads(decoded.stdout)


# Return whether a profile covers the team, bundle ID, device and identity and outlives the margin.
def _profile_compatible(profile: dict, bundle_id: str, team_id: str, udid: str, identity: str, margin_seconds: int = 0) -> bool:
    try:
        entitlements = profile["Entitlements"]
        expiry = profile["ExpirationDate"].replace(tzinfo=timezone.utc)
    except (KeyError, AttributeError) as exc:
        logger.debug("ios resign: profile lacks Entitlements/ExpirationDate: %s", exc, exc_info=True)
        return False
    if team_id not in profile.get("TeamIdentifier", []):
        logger.debug("ios resign: profile incompatible: team %s not in %s", team_id, profile.get("TeamIdentifier", []))
        return False
    if entitlements.get("application-identifier") != f"{team_id}.{bundle_id}":
        logger.debug(
            "ios resign: profile incompatible: application-identifier %s != %s.%s",
            entitlements.get("application-identifier"),
            team_id,
            bundle_id,
        )
        return False
    if udid not in profile.get("ProvisionedDevices", []):
        logger.debug("ios resign: profile incompatible: device %s not provisioned", udid)
        return False
    if not expiry > datetime.now(timezone.utc) + timedelta(seconds=margin_seconds):
        logger.debug("ios resign: profile incompatible: expires %s within margin %ss", expiry, margin_seconds)
        return False
    if not any(
        hashlib.sha1(cert).hexdigest().upper() == identity.upper()
        for cert in profile.get("DeveloperCertificates", [])
    ):
        logger.debug("ios resign: profile incompatible: signing identity %s not among its developer certificates", identity)
        return False
    logger.debug("ios resign: profile compatible with %s/%s on %s (expires %s)", team_id, bundle_id, udid, expiry)
    return True


# Validate the profile against this app/device/signer, embed it, and return its entitlements plist.
def prepare_profile(app_dir: Path, profile_path: Path, bundle_id: str, team_id: str, udid: str, identity: str, work_dir: Path) -> Path:
    profile = _decode_profile(profile_path)
    if not _profile_compatible(profile, bundle_id, team_id, udid, identity):
        raise ValueError("Provisioning profile is incompatible with this app, device, or signer")
    shutil.copy2(profile_path, Path(app_dir) / "embedded.mobileprovision")
    logger.debug("ios resign: embedded %s into %s", profile_path, app_dir)
    output = Path(work_dir) / "entitlements.plist"
    output.write_bytes(plistlib.dumps(profile["Entitlements"]))
    logger.debug("ios resign: wrote entitlements to %s", output)
    return output


# Return the first locally installed profile that matches this app, device and signer, or None.
def discover_provisioning_profile(bundle_id: str, team_id: str, udid: str, identity: str, search_dir: str | None = None, margin_seconds: int = 0) -> Path | None:
    search = Path(search_dir).expanduser() if search_dir else Path(DEFAULT_PROFILE_DIR).expanduser()
    logger.debug("ios resign: searching %s for a profile for %s/%s (margin %ss)", search, team_id, bundle_id, margin_seconds)
    if not search.is_dir():
        logger.debug("ios resign: profile directory %s does not exist", search)
        return None
    for path in sorted(search.glob("*.mobileprovision")):
        try:
            profile = _decode_profile(path)
        except Exception as exc:
            logger.debug("ios resign: could not decode %s: %s", path, exc, exc_info=True)
            continue
        if _profile_compatible(profile, bundle_id, team_id, udid, identity, margin_seconds):
            logger.debug("ios resign: selected profile %s", path)
            return path
    logger.debug("ios resign: no compatible profile in %s", search)
    return None


# Build the RepackProvision project with xcodegen and xcodebuild so Xcode issues a profile.
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
        logger.debug("ios resign: running %s (cwd=%s)", cmd, project_dir)
        started = time.monotonic()
        completed = subprocess.run(cmd, cwd=project_dir, capture_output=True, text=True)
        logger.debug("ios resign: %s exited %s in %.2fs", cmd[0], completed.returncode, time.monotonic() - started)
        if completed.returncode != 0:
            logger.debug("ios resign: %s output head: %s", cmd[0], (completed.stderr.strip() or completed.stdout.strip())[:200])
            raise RuntimeError(f"{cmd[0]} failed: {completed.stderr.strip() or completed.stdout.strip()}")


# Return a profile not expiring within the margin, generating one when allowed and needed.
def ensure_provisioning_profile(
    bundle_id: str,
    team_id: str,
    udid: str,
    identity: str,
    margin_seconds: int = 3600,
    generate: bool = True,
) -> Path | None:
    existing = discover_provisioning_profile(bundle_id, team_id, udid, identity, margin_seconds=margin_seconds)
    if existing is not None:
        logger.debug("ios resign: using existing profile %s", existing)
        return existing
    if not generate:
        logger.debug("ios resign: no profile and generation disabled")
        return None
    logger.debug("ios resign: generating a provisioning profile for %s/%s", team_id, bundle_id)
    generate_provisioning_profile(bundle_id, team_id, udid)
    return discover_provisioning_profile(bundle_id, team_id, udid, identity, margin_seconds=margin_seconds)


# Return the SHA-1 of the team's valid Apple Development identity, since names are not unique.
def signing_identity_for_team(team_id: str) -> str:
    listing = subprocess.run(["security", "find-identity", "-v", "-p", "codesigning"], capture_output=True, text=True).stdout
    logger.debug("ios resign: security find-identity listed %s lines; looking for team %s", len(listing.splitlines()), team_id)
    for line in listing.splitlines():
        parts = line.strip().split('"')
        if len(parts) < 2 or not parts[1].startswith("Apple Development:"):
            continue
        sha1 = parts[0].split()[1]
        logger.debug("ios resign: candidate identity %s (%s)", sha1, parts[1])
        pem = subprocess.run(["security", "find-certificate", "-a", "-Z", "-p"], capture_output=True, text=True).stdout
        block = pem.split(f"SHA-1 hash: {sha1}", 1)[-1].split("-----END CERTIFICATE-----", 1)[0] + "-----END CERTIFICATE-----"
        subject = subprocess.run(["openssl", "x509", "-noout", "-subject"], input=block[block.find("-----BEGIN"):], capture_output=True, text=True).stdout
        logger.debug("ios resign: identity %s subject %s", sha1, subject.strip()[:200])
        if f"OU={team_id}" in subject:
            logger.debug("ios resign: selected identity %s for team %s", sha1, team_id)
            return sha1
    logger.debug("ios resign: no Apple Development identity matched team %s", team_id)
    raise RuntimeError(f"no valid Apple Development identity in the keychain has team (OU) {team_id}")


# Run codesign --force on a target with the identity and optional entitlements.
def _codesign(identity: str, target: Path, entitlements: Path | None = None) -> subprocess.CompletedProcess:
    command = ["codesign", "--force", "--sign", identity]
    if entitlements is not None:
        command += ["--entitlements", str(entitlements)]
    command.append(str(target))
    logger.debug("ios resign: running %s", command)
    started = time.monotonic()
    completed = subprocess.run(command, capture_output=True, text=True)
    logger.debug("ios resign: codesign %s exited %s in %.2fs", target, completed.returncode, time.monotonic() - started)
    return completed


# Return the dylibs, frameworks and app extensions inside the bundle that need signing.
def _nested_signables(app_dir: Path) -> list[Path]:
    seen: list[Path] = []
    for pattern in ("Frameworks/*.dylib", "Frameworks/*.framework", "PlugIns/*.appex", "**/*.dylib"):
        for path in sorted(app_dir.glob(pattern)):
            if path not in seen and path != app_dir:
                seen.append(path)
    logger.debug("ios resign: %s nested signables under %s", len(seen), app_dir)
    return seen


# Embed the validated profile, sign nested code, then the bundle, then verify the whole tree.
def resign_app(
    app_dir: Path,
    identity: str,
    team_id: str,
    profile_path: Path,
    bundle_id: str,
    udid: str,
    work_dir: Path,
) -> ResignResult:
    app_dir = Path(app_dir)
    logger.debug("ios resign: resigning %s as %s (team %s, identity %s, profile %s)", app_dir, bundle_id, team_id, identity, profile_path)
    try:
        entitlements = prepare_profile(app_dir, Path(profile_path), bundle_id, team_id, udid, identity, work_dir)
    except Exception as exc:
        logger.debug("ios resign: preparing profile failed: %s", exc, exc_info=True)
        return ResignResult("RESIGN_FAILED", [str(exc)])
    for nested in _nested_signables(app_dir):
        completed = _codesign(identity, nested)
        if completed.returncode != 0:
            logger.debug("ios resign: codesign failed for %s: %s", nested, (completed.stderr.strip() or completed.stdout.strip())[:200])
            return ResignResult("RESIGN_FAILED", [completed.stderr.strip() or completed.stdout.strip() or f"codesign failed for {nested.name}"])
    completed = _codesign(identity, app_dir, entitlements=entitlements)
    if completed.returncode != 0:
        logger.debug("ios resign: codesign failed for bundle %s: %s", app_dir, (completed.stderr.strip() or completed.stdout.strip())[:200])
        return ResignResult("RESIGN_FAILED", [completed.stderr.strip() or completed.stdout.strip() or "codesign failed for the app bundle"])
    logger.debug("ios resign: verifying %s", app_dir)
    started = time.monotonic()
    verify = subprocess.run(["codesign", "--verify", "--deep", "--strict", str(app_dir)], capture_output=True, text=True)
    logger.debug("ios resign: codesign --verify exited %s in %.2fs", verify.returncode, time.monotonic() - started)
    if verify.returncode != 0:
        logger.debug("ios resign: verification output head: %s", (verify.stderr.strip() or verify.stdout.strip())[:200])
        return ResignResult("RESIGN_FAILED", [verify.stderr.strip() or verify.stdout.strip() or "codesign verification failed"])
    logger.debug("ios resign: %s RESIGNED", app_dir)
    return ResignResult("RESIGNED")
