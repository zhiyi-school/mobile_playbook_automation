from __future__ import annotations

from dataclasses import dataclass, field

from mobile_playbook.platforms.android.adb import AdbClient


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
    return code == 0 and any(line.strip() == f"package:{package}" for line in out.splitlines())


def declared_permissions(adb: AdbClient, package: str) -> list[str]:
    code, out, _ = adb.run(["shell", "dumpsys", "package", package])
    if code != 0 or not out:
        return []
    permissions: set[str] = set()
    for raw in out.splitlines():
        line = raw.strip()
        if line.startswith("android.permission."):
            permissions.add(line.split(":", 1)[0].strip())
    return sorted(permissions)


def grant_all(adb: AdbClient, package: str) -> GrantResult:
    result = GrantResult(package=package)
    try:
        if not is_installed(adb, package):
            result.error = "not installed / device unreachable - skipped"
            return result
        permissions = declared_permissions(adb, package)
        if not permissions:
            result.error = "no android.permission.* declared"
            return result
        for permission in permissions:
            if permission in SPECIAL_PERMISSION_OPS:
                code, out, err = adb.run(["shell", "appops", "set", package, SPECIAL_PERMISSION_OPS[permission], "allow"])
                bucket = result.special if _grant_succeeded(code, out, err) else result.skipped
            else:
                code, out, err = adb.run(["shell", "pm", "grant", package, permission])
                bucket = result.granted if _grant_succeeded(code, out, err) else result.skipped
            bucket.append(permission)
    except Exception as exc:
        result.error = f"error while granting: {exc}"
    return result


def grant_many(adb: AdbClient, packages: list[str]) -> list[GrantResult]:
    return [grant_all(adb, package) for package in packages]


def _grant_succeeded(code: int, out: str, err: str) -> bool:
    return code == 0 and not out and not err
