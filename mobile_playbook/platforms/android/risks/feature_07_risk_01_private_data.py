"""
Android risk check for whether an app's private data directory, read as root on a rooted device, holds sensitive data
in plain text, as the playbook reads it with `su -c ls` and `su -c cat`.
"""

from __future__ import annotations

import logging
from datetime import datetime

from mobile_playbook.common.logging_setup import redacted
from mobile_playbook.common.storage_paths import android_work_dir
from mobile_playbook.platforms.android.device.fingerprint import installed_base_apk_sha256
from mobile_playbook.platforms.android.device.private_data import PrivateDataError, copy_private_data
from mobile_playbook.platforms.android.device.root import find_root_shell
from mobile_playbook.platforms.android.models import AndroidRiskRunResult
from mobile_playbook.platforms.android.private_data.findings import record_scan
from mobile_playbook.platforms.android.private_data.launch import launch_and_stop
from mobile_playbook.platforms.android.private_data.scan import scan_archive
from mobile_playbook.platforms.android.risks.base import AndroidRisk

logger = logging.getLogger(__name__)


class AndroidPrivateDataRisk(AndroidRisk):
    risk_id = "android-feature-07-risk-01"
    feature_id = "feature-07"
    name = "Android Private Data Directory Test"
    test_case_id = "private_data_access"
    test_case_type = "adb_root_file_access"
    requires = ["adb", "root"]

    # Launch the app, copy its private data directories off the device as root, scan them and write the result.
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
        archive = android_work_dir() / "private_data" / package.replace(".", "_") / f"{report_writer.run_timestamp}.tar"
        try:
            result.metadata["installed_base_apk_sha256"] = installed_base_apk_sha256(adb, package)
            root = find_root_shell(adb)
            if root is None:
                raise PrivateDataError("The device does not grant root, so the app's private data directory cannot be read.")
            result.metadata["root_method"] = root.method
            result.metadata["launch"] = launch_and_stop(adb, package, float(cfg.get("launch_wait", 5)))
            copied = copy_private_data(root, package, archive, int(float(cfg.get("max_archive_mb", 200)) * 1024 * 1024))
            result.metadata["directories"] = copied.directories
            result.metadata["archive_bytes"] = copied.size_bytes
            if copied.tar_warnings:
                result.metadata["tar_warnings"] = copied.tar_warnings[:20]
            listing_path = report_dir / "private_data_listing.txt"
            listing_path.write_text(copied.listing)
            result.evidence.append({"kind": "private_data_listing", "path": str(listing_path), "label": "Private data directories listed as root"})
            files = scan_archive(archive, bool(cfg.get("reveal_values", False)))
            access_flag = {
                "id": "PRIVATE_DATA_READABLE_WITH_ROOT",
                "severity": "LOW",
                "title": "The app's private data directory can be read with root",
                "evidence": [*copied.directories, f"{len(files)} file(s) read"],
                "recommendation": "Assume files in the private data directory can be read on a rooted device. Encrypt sensitive data at rest with a key held in the Android Keystore.",
            }
            record_scan(files, access_flag, app_config, report_dir, result)
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
