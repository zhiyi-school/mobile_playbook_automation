"""
Android risk check for whether an app's private data directory can be read over USB debugging without root, as the
playbook reads a debuggable app's files with `run-as`, and whether its data could be taken with `adb backup` instead.
"""

from __future__ import annotations

import logging
from datetime import datetime

from mobile_playbook.common.logging_setup import redacted
from mobile_playbook.common.storage_paths import android_work_dir
from mobile_playbook.platforms.android.device.adb import AdbClient
from mobile_playbook.platforms.android.device.fingerprint import installed_base_apk_sha256
from mobile_playbook.platforms.android.device.package_flags import PackageFlags, package_flags
from mobile_playbook.platforms.android.device.private_data import PrivateDataError, copy_private_data_run_as
from mobile_playbook.platforms.android.device.run_as import RunAsUnavailable, run_as_shell
from mobile_playbook.platforms.android.models import AndroidRiskRunResult
from mobile_playbook.platforms.android.private_data.findings import record_scan, write_critical_findings
from mobile_playbook.platforms.android.private_data.launch import launch_and_stop
from mobile_playbook.platforms.android.private_data.scan import scan_archive
from mobile_playbook.platforms.android.risks.base import AndroidRisk

logger = logging.getLogger(__name__)

MARKDOWN_TITLE = "Android USB Debugging Private Data Critical Findings"
# From Android 12 (API 31), adb backup leaves out the data of apps that target API 31 or later.
BACKUP_EXCLUDES_DATA_FROM_SDK = 31


class AndroidUsbDebuggingDataRisk(AndroidRisk):
    risk_id = "android-feature-02-risk-01"
    feature_id = "feature-02"
    name = "Android USB Debugging Private Data Test"
    test_case_id = "private_data_access"
    test_case_type = "adb_run_as_file_access"
    requires = ["adb"]

    # Check whether the app is debuggable; if so, copy its private data with run-as and scan it, otherwise record
    # whether adb backup could still return its data, and write the result.
    def run(self, app_config, global_config, device_client, report_writer):
        started_at = datetime.now().astimezone()
        package = app_config.package_name
        report_dir = report_writer.test_report_dir(app_config.id, self.risk_id, self.test_case_id, platform="android")
        cfg = {**(getattr(global_config, "private_data", None) or {}), **(app_config.risks.get(self.risk_id) or {})}
        result = AndroidRiskRunResult(
            run_timestamp=report_writer.run_timestamp,
            timestamp_start=started_at.isoformat(),
            timestamp_end=None,
            app_id=app_config.id,
            app_name=app_config.name,
            package_name=package,
            risk_id=self.risk_id,
            test_case_id=self.test_case_id,
            test_case_type=self.test_case_type,
        )
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("%s[%s]: setup package=%s report_dir=%s config=%s", self.risk_id, app_config.id, package, report_dir, redacted(cfg))
        adb = device_client.adb
        keep_archive = bool(cfg.get("keep_archive", False))
        # A separate name from android-feature-07-risk-01's archive, which may be copied for the same app in the same run.
        archive = android_work_dir() / "private_data" / package.replace(".", "_") / f"{report_writer.run_timestamp}_run_as.tar"
        try:
            result.metadata["installed_base_apk_sha256"] = installed_base_apk_sha256(adb, package)
            flags = package_flags(adb, package)
            if flags is None:
                raise PrivateDataError(f"dumpsys package reports no flags for {package}; is it installed on the device?")
            result.metadata["package_flags"] = flags.to_dict()
            result.metadata["device_sdk"] = _device_sdk(adb)
            try:
                shell = run_as_shell(adb, package)
            except RunAsUnavailable as exc:
                result.metadata["run_as"] = {"available": False, "detail": str(exc)}
                if flags.debuggable:
                    raise PrivateDataError(f"The app is debuggable, but the device refused run-as: {exc}") from None
                self._record_not_accessible(flags, result.metadata["device_sdk"], app_config, report_dir, result)
                return result
            result.metadata["run_as"] = {"available": True}
            result.metadata["launch"] = launch_and_stop(adb, package, float(cfg.get("launch_wait", 5)))
            copied = copy_private_data_run_as(shell, archive, int(float(cfg.get("max_archive_mb", 200)) * 1024 * 1024))
            result.metadata["directories"] = copied.directories
            result.metadata["archive_bytes"] = copied.size_bytes
            if copied.tar_warnings:
                result.metadata["tar_warnings"] = copied.tar_warnings[:20]
            listing_path = report_dir / "private_data_listing.txt"
            listing_path.write_text(copied.listing)
            result.evidence.append({"kind": "private_data_listing", "path": str(listing_path), "label": "Private data directories listed with run-as"})
            files = scan_archive(archive, bool(cfg.get("reveal_values", False)))
            access_flag = {
                "id": "PRIVATE_DATA_READABLE_VIA_USB_DEBUGGING",
                "severity": "HIGH",
                "title": "The app is debuggable, so its private data directory can be read over USB debugging without root",
                "evidence": [f"run-as {package} succeeded", *copied.directories, f"{len(files)} file(s) read"],
                "recommendation": "Release builds must not set android:debuggable, so that run-as is refused. Encrypt sensitive data at rest with a key held in the Android Keystore.",
            }
            record_scan(files, access_flag, app_config, report_dir, result, MARKDOWN_TITLE)
        except Exception as exc:
            logger.debug("%s[%s]: run failed: %s", self.risk_id, app_config.id, exc, exc_info=True)
            result.final_status = "FAILED"
            result.verdict = "Inconclusive"
            result.errors.append(str(exc))
        finally:
            if archive.exists():
                if keep_archive:
                    result.metadata["archive"] = str(archive)
                else:
                    archive.unlink()
            result.timestamp_end = datetime.now().astimezone().isoformat()
            logger.debug("%s[%s]: writing result final_status=%s verdict=%s errors=%s", self.risk_id, app_config.id, result.final_status, result.verdict, result.errors)
            report_writer.write_result(result, report_dir)
        return result

    # Decide the result for an app run-as refuses: reduced risk, unless adb backup could still return its data, which
    # is not attempted because it needs a tap on the device.
    def _record_not_accessible(self, flags: PackageFlags, device_sdk: int | None, app_config, report_dir, result: AndroidRiskRunResult) -> None:
        backup = backup_returns_data(flags, device_sdk)
        result.metadata["backup_returns_data"] = backup
        flag_list = []
        if backup:
            result.final_status, result.verdict = "PRIVATE_DATA_BACKUP_POSSIBLE", "Inconclusive"
            flag_list.append(
                {
                    "id": "APP_DATA_BACKUP_ALLOWED",
                    "severity": "LOW",
                    "title": "The app allows adb backup, which can return its private data over USB debugging",
                    "evidence": ["ALLOW_BACKUP is set", f"targetSdk {flags.target_sdk if flags.target_sdk is not None else 'unknown'}", f"device API level {device_sdk if device_sdk is not None else 'unknown'}"],
                    "recommendation": 'Set android:allowBackup="false", or leave sensitive files out of backups with android:dataExtractionRules and android:fullBackupContent.',
                }
            )
        else:
            result.final_status, result.verdict = "PRIVATE_DATA_NOT_ACCESSIBLE", "Reduced Risk"
        logger.debug("%s[%s]: run-as refused, backup_returns_data=%s -> %s", self.risk_id, app_config.id, backup, result.final_status)
        write_critical_findings(flag_list, app_config, report_dir, result, MARKDOWN_TITLE)


# Report whether `adb backup` would include the app's data: it must allow backups, and from Android 12 target API 30 or
# lower; an unknown level counts as possible.
def backup_returns_data(flags: PackageFlags, device_sdk: int | None) -> bool:
    if not flags.allow_backup:
        return False
    if device_sdk is None or flags.target_sdk is None:
        return True
    return device_sdk < BACKUP_EXCLUDES_DATA_FROM_SDK or flags.target_sdk < BACKUP_EXCLUDES_DATA_FROM_SDK


# Return the device's API level, or None when getprop does not report one.
def _device_sdk(adb: AdbClient) -> int | None:
    code, out, _ = adb.run(["shell", "getprop", "ro.build.version.sdk"])
    value = (out or "").strip()
    return int(value) if code == 0 and value.isdigit() else None
