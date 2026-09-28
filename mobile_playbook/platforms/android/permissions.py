from __future__ import annotations

import logging
from dataclasses import dataclass, field

from mobile_playbook.platforms.android.adb import AdbClient

logger = logging.getLogger(__name__)

SPECIAL_PERMISSION_OPS = {
    "android.permission.SYSTEM_ALERT_WINDOW": "SYSTEM_ALERT_WINDOW",
    "android.permission.MANAGE_EXTERNAL_STORAGE": "MANAGE_EXTERNAL_STORAGE",
    "android.permission.PACKAGE_USAGE_STATS": "GET_USAGE_STATS",
    "android.permission.WRITE_SETTINGS": "WRITE_SETTINGS",
    "android.permission.REQUEST_INSTALL_PACKAGES": "REQUEST_INSTALL_PACKAGES",
}


@dataclass
class GrantResult:
    package: str
    granted: list[str] = field(default_factory=list)
    special: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    error: str | None = None

    def to_dict(self) -> dict:
        return {
            "package": self.package,
            "granted": self.granted,
            "special": self.special,
            "skipped": self.skipped,
            "error": self.error,
        }


def is_installed(adb: AdbClient, package: str) -> bool:
    code, out, _ = adb.run(["shell", "pm", "list", "packages", package])
    installed = code == 0 and any(line.strip() == f"package:{package}" for line in out.splitlines())
    logger.debug("android permissions: %s installed=%s (pm list exit %s)", package, installed, code)
    return installed


def declared_permissions(adb: AdbClient, package: str) -> list[str]:
    code, out, _ = adb.run(["shell", "dumpsys", "package", package])
    if code != 0 or not out:
        logger.debug("android permissions: dumpsys package %s gave exit %s with %s chars; no permissions", package, code, len(out or ""))
        return []
    permissions: set[str] = set()
    for raw in out.splitlines():
        line = raw.strip()
        if line.startswith("android.permission."):
            permissions.add(line.split(":", 1)[0].strip())
    logger.debug("android permissions: %s declares %s permissions: %s", package, len(permissions), sorted(permissions))
    return sorted(permissions)


def grant_all(adb: AdbClient, package: str) -> GrantResult:
    result = GrantResult(package=package)
    logger.debug("android permissions: granting all declared permissions for %s", package)
    try:
        if not is_installed(adb, package):
            logger.debug("android permissions: %s not installed or device unreachable; skipping", package)
            result.error = "not installed / device unreachable - skipped"
            return result
        permissions = declared_permissions(adb, package)
        if not permissions:
            logger.debug("android permissions: %s declares no android.permission.*", package)
            result.error = "no android.permission.* declared"
            return result
        for permission in permissions:
            if permission in SPECIAL_PERMISSION_OPS:
                code, out, err = adb.run(["shell", "appops", "set", package, SPECIAL_PERMISSION_OPS[permission], "allow"])
                bucket = result.special if _grant_succeeded(code, out, err) else result.skipped
            else:
                code, out, err = adb.run(["shell", "pm", "grant", package, permission])
                bucket = result.granted if _grant_succeeded(code, out, err) else result.skipped
            logger.debug("android permissions: %s %s -> %s (exit %s, out head=%r, err head=%r)", package, permission, "skipped" if bucket is result.skipped else "granted", code, (out or "")[:200], (err or "")[:200])
            bucket.append(permission)
    except Exception as exc:
        logger.debug("android permissions: granting for %s failed: %s", package, exc, exc_info=True)
        result.error = f"error while granting: {exc}"
    logger.debug("android permissions: %s granted=%s special=%s skipped=%s error=%s", package, len(result.granted), len(result.special), len(result.skipped), result.error)
    return result


def grant_many(adb: AdbClient, packages: list[str]) -> list[GrantResult]:
    return [grant_all(adb, package) for package in packages]


def _grant_succeeded(code: int, out: str, err: str) -> bool:
    return code == 0 and not out and not err
