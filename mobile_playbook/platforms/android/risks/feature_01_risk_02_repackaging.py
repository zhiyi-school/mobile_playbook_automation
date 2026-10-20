"""
Android risk check for whether an app still works after being decoded, patched, re-signed and reinstalled.
"""

from __future__ import annotations

import base64
import logging
import re
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path

from mobile_playbook.common.storage_paths import android_work_dir, resolve_under_repository

from mobile_playbook.common.logging_setup import REDACTED, redacted
from mobile_playbook.platforms.android.acquisition.store import APP_STORES, DEFAULT_APP_STORE, STORE_APPS, app_store_for
from mobile_playbook.platforms.android.device.apks import pull_installed_apks
from mobile_playbook.platforms.android.device.fingerprint import installed_base_apk_sha256, installer_of
from mobile_playbook.platforms.android.device.install import install_apks
from mobile_playbook.platforms.android.device.permissions import PERMISSION_CONTROLLER_PACKAGES
from mobile_playbook.platforms.android.models import AndroidRiskRunResult
from mobile_playbook.platforms.android.risks.base import AndroidRisk

logger = logging.getLogger(__name__)

try:
    from appium.webdriver.common.appiumby import AppiumBy
except ImportError:
    logger.debug("android repackaging: AppiumBy unavailable; Appium validation disabled", exc_info=True)
    AppiumBy = None

APKEDITOR_DIR = "tools/vendor/apkeditor"
# The prompt's Deny button: by its AOSP ids, else by its English label, which HarmonyOS's own prompts need since
# they use other ids and ignore Back. Prompts matching neither are dismissed with Back.
PERMISSION_DENY_SELECTORS = (
    'new UiSelector().resourceIdMatches(".*:id/permission_deny.*")',
    'new UiSelector().clickable(true).textMatches("(?i)(deny|don.t allow)")',
)


class StageFailure(Exception):
    """Stops the repackaging pipeline at the current stage with a reason for the report."""


class AndroidRepackagingRisk(AndroidRisk):
    risk_id = "android-feature-01-risk-02"
    feature_id = "feature-01"
    name = "Android Repackaging Test"
    test_case_id = "repackaging"
    test_case_type = "apk_decode_patch_resign_validate"
    requires = ["adb", "apktool", "apksigner", "keytool", "appium"]
    store = APP_STORES[DEFAULT_APP_STORE]

    # Repackage and reinstall the app, validate it with Appium, optionally restore the original and write the result.
    def run(self, app_config, global_config, device_client, report_writer):
        started_at = datetime.now().astimezone()
        report_dir = report_writer.test_report_dir(app_config.id, self.risk_id, self.test_case_id, platform="android")
        cfg = {**global_config.repackaging, **(app_config.risks.get(self.risk_id) or {})}
        self.store = app_store_for(global_config)
        result = AndroidRiskRunResult(
            run_timestamp=report_writer.run_timestamp,
            timestamp_start=started_at.isoformat(),
            timestamp_end=None,
            app_id=app_config.id,
            app_name=app_config.name,
            package_name=app_config.package_name,
            risk_id=self.risk_id,
            test_case_id=self.test_case_id,
            test_case_type=self.test_case_type,
        )
        app_dir = None
        replaced_app = False
        try:
            try:
                work_dir = resolve_under_repository(cfg["work_dir"]) if cfg.get("work_dir") else android_work_dir() / "repackaging"
                app_dir = work_dir / app_config.package_name.replace(".", "_")
                app_dir.mkdir(parents=True, exist_ok=True)
                keystore = resolve_under_repository(cfg["keystore_path"]) if cfg.get("keystore_path") else app_dir.parent / "release.keystore"
                keystore_alias = cfg.get("keystore_alias", "mobileplaybook")
                keystore_pass = cfg.get("keystore_pass", "password")
                logger.debug("%s[%s]: setup package=%s app_dir=%s report_dir=%s keystore=%s alias=%s", self.risk_id, app_config.id, app_config.package_name, app_dir, report_dir, keystore, keystore_alias)
                if logger.isEnabledFor(logging.DEBUG):
                    logger.debug("%s[%s]: effective config %s", self.risk_id, app_config.id, redacted({key: REDACTED if "pass" in str(key).lower() else value for key, value in cfg.items()}))
                # Identify the tested build, and remember its installer so the restore can record the same one.
                result.metadata["installed_base_apk_sha256"] = installed_base_apk_sha256(device_client.adb, app_config.package_name)
                result.metadata["installer"] = installer_of(device_client.adb, app_config.package_name)

                stages = [
                    ("backup_apks", lambda: self._backup_apks(device_client, app_config.package_name, app_dir)),
                    ("ensure_keystore", lambda: self._ensure_keystore(keystore, keystore_alias, keystore_pass)),
                    ("merge_split_apks", lambda: self._merge_split_apks(app_dir, cfg.get("apkeditor_jar"))),
                    ("apktool_decode", lambda: self._run_apktool_decode(app_dir)),
                    ("add_debuggable_to_manifest", lambda: self._add_debuggable_to_manifest(app_dir)),
                    ("change_apk_name", lambda: self._change_apk_name(app_dir)),
                    ("rebuild_apk", lambda: self._rebuild_apk(app_dir)),
                    ("sign_apks", lambda: self._sign_apks(app_dir, keystore, keystore_pass)),
                    ("install_repackaged_apks", lambda: self._install_repackaged_apk(device_client, app_dir, app_config.package_name)),
                ]
                failed_stage = None
                failure_detail = None
                for stage, action in stages:
                    logger.debug("%s[%s]: stage %s starting", self.risk_id, app_config.id, stage)
                    stage_started = time.monotonic()
                    # The install stage uninstalls the original first, so from here on the device needs restoring.
                    replaced_app = replaced_app or stage == "install_repackaged_apks"
                    try:
                        ok = action()
                    except StageFailure as exc:
                        ok, failure_detail = False, str(exc)
                    if not ok:
                        logger.debug("%s[%s]: stage %s failed after %.2fs (%s)", self.risk_id, app_config.id, stage, time.monotonic() - stage_started, failure_detail)
                        failed_stage = stage
                        break
                    logger.debug("%s[%s]: stage %s ok in %.2fs", self.risk_id, app_config.id, stage, time.monotonic() - stage_started)
                if failed_stage is None:
                    verdict, recording_path = self._validate_with_appium(device_client, app_config.package_name, report_dir, cfg)
                    result.metadata["validation_verdict"] = verdict
                    if recording_path is not None:
                        logger.debug("%s[%s]: evidence repackaged launch recording at %s", self.risk_id, app_config.id, recording_path)
                        result.evidence.append({"kind": "screen_recording", "path": str(recording_path), "label": "Repackaged launch recording"})
                    else:
                        logger.debug("%s[%s]: no launch recording evidence saved", self.risk_id, app_config.id)
                    for screenshot in sorted((report_dir / "screenshots").glob("*.png")):
                        result.evidence.append({"kind": "screenshot", "path": str(screenshot), "label": f"Repackaged app {screenshot.stem.replace('_', ' ')}"})
                    result.metadata["debuggable"] = self._check_debuggable(device_client, app_config.package_name, report_dir)
                    if result.metadata["debuggable"].get("listing"):
                        result.evidence.append({"kind": "run_as_listing", "path": result.metadata["debuggable"]["listing"], "label": "Private files listed with run-as"})
                    if verdict.startswith("PASS"):
                        result.final_status, result.verdict = "REPACKAGING_SURVIVED", "At Risk"
                    elif verdict.startswith("FAIL"):
                        result.final_status, result.verdict = "REPACKAGING_BLOCKED", "Reduced Risk"
                    else:
                        # Only an observed exit or redirect shows a defense; an unjudgable launch stays Inconclusive.
                        result.final_status = "REPACKAGING_FAILED"
                        result.errors.append(f"Validation did not reach a verdict: {verdict}")
                    logger.debug("%s[%s]: validation %r -> final_status=%s verdict=%s", self.risk_id, app_config.id, verdict, result.final_status, result.verdict)
                else:
                    result.final_status = "REPACKAGING_FAILED"
                    result.errors.append(f"Failed at stage: {failed_stage} - {failure_detail}" if failure_detail else f"Failed at stage: {failed_stage}")
                    logger.debug("%s[%s]: repackaging pipeline stopped at %s -> final_status=%s verdict=%s", self.risk_id, app_config.id, failed_stage, result.final_status, result.verdict)
                result.metadata["work_dir"] = str(app_dir)
            except Exception as exc:
                logger.debug("%s[%s]: run failed: %s", self.risk_id, app_config.id, exc, exc_info=True)
                result.final_status = "FAILED"
                result.errors.append(str(exc))
            # Restore outside the pipeline's try, so an error during validation still puts the original back.
            if not replaced_app:
                logger.debug("%s[%s]: the installed app was never replaced; nothing to restore", self.risk_id, app_config.id)
            elif not bool(cfg.get("restore_original_after_test", True)):
                logger.debug("%s[%s]: restore_original_after_test disabled; leaving repackaged build installed", self.risk_id, app_config.id)
            else:
                try:
                    self._restore_original(device_client, app_dir, app_config.package_name, result)
                except Exception as exc:
                    logger.debug("%s[%s]: restore failed: %s", self.risk_id, app_config.id, exc, exc_info=True)
                    result.errors.append(f"Restore failed: {exc}")
                    logger.warning("Could not restore %s after the repackaging test: %s", app_config.package_name, exc)
        finally:
            result.timestamp_end = datetime.now().astimezone().isoformat()
            logger.debug("%s[%s]: writing result final_status=%s verdict=%s errors=%s evidence=%s", self.risk_id, app_config.id, result.final_status, result.verdict, result.errors, [item.get("path") for item in result.evidence])
            report_writer.write_result(result, report_dir)
        return result

    # Pull the installed base and split APKs into the app's original/ folder.
    def _backup_apks(self, device_client, package_name: str, app_dir: Path) -> bool:
        return bool(pull_installed_apks(device_client.adb, package_name, app_dir / "original"))

    # Reuse the signing keystore or generate one with keytool.
    def _ensure_keystore(self, keystore: Path, alias: str, password: str) -> bool:
        if keystore.exists():
            logger.debug("%s: reusing existing keystore %s", self.risk_id, keystore)
            return True
        keystore.parent.mkdir(parents=True, exist_ok=True)
        logger.debug("%s: generating keystore %s via keytool -genkeypair (alias=%s, RSA 2048, 3650 days, passwords %s)", self.risk_id, keystore, alias, REDACTED)
        try:
            subprocess.run(
                [
                    "keytool",
                    "-genkeypair",
                    "-alias",
                    alias,
                    "-keystore",
                    str(keystore),
                    "-keyalg",
                    "RSA",
                    "-keysize",
                    "2048",
                    "-validity",
                    "3650",
                    "-storepass",
                    password,
                    "-keypass",
                    password,
                    "-dname",
                    "CN=Mobile Playbook, OU=Test, O=Local, L=Singapore, ST=Singapore, C=SG",
                ],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except (subprocess.CalledProcessError, FileNotFoundError) as exc:
            logger.debug("%s: keytool failed for %s (%s, exit %s)", self.risk_id, keystore, type(exc).__name__, getattr(exc, "returncode", None))
            return False
        logger.debug("%s: generated keystore %s", self.risk_id, keystore)
        return True

    # Merge the backed-up base and split APKs into merged.apk with APKEditor, so apktool sees resources that live in splits.
    def _merge_split_apks(self, app_dir: Path, configured_jar: str | None) -> bool:
        original_dir = app_dir / "original"
        merged_apk = app_dir / "merged.apk"
        if merged_apk.exists():
            logger.debug("%s: removing stale merged APK %s", self.risk_id, merged_apk)
            merged_apk.unlink()
        splits = self._split_apks(original_dir)
        if not splits:
            logger.debug("%s: no split APKs in %s; decoding base.apk directly", self.risk_id, original_dir)
            return True
        has_splits = f"The app has {len(splits)} split APK{'s' if len(splits) != 1 else ''} that must be merged before decoding"
        jar = self._resolve_apkeditor(configured_jar)
        if jar is None:
            raise StageFailure(
                f"{has_splits}, but APKEditor was not found. "
                f"Put APKEditor.jar under {APKEDITOR_DIR}/ or set repackaging.apkeditor_jar."
            )
        if not shutil.which("java"):
            raise StageFailure(f"{has_splits}, but java (needed to run APKEditor) is not on PATH.")
        cmd = ["java", "-jar", str(jar), "m", "-i", str(original_dir), "-o", str(merged_apk)]
        logger.debug("%s: merging %s split APKs: argv=%s", self.risk_id, len(splits), cmd)
        started = time.monotonic()
        result = subprocess.run(cmd, capture_output=True, text=True, stdin=subprocess.DEVNULL)
        logger.debug("%s: APKEditor merge exited %s in %.2fs (merged exists=%s, stderr head=%r)", self.risk_id, result.returncode, time.monotonic() - started, merged_apk.exists(), (result.stderr or "")[:200])
        if result.returncode != 0 or not merged_apk.exists():
            lines = [line.strip() for line in f"{result.stdout or ''}\n{result.stderr or ''}".splitlines() if line.strip()]
            raise StageFailure(f"APKEditor merge exited {result.returncode}: {lines[-1] if lines else 'no output'}")
        return True

    # Return the backed-up split APKs, excluding base.apk.
    def _split_apks(self, original_dir: Path) -> list[Path]:
        return sorted(p for p in original_dir.glob("*.apk") if p.is_file() and p.name != "base.apk")

    # Return the configured APKEditor jar, else the newest vendored one, or None when neither exists.
    def _resolve_apkeditor(self, configured: str | None) -> Path | None:
        if configured:
            jar = resolve_under_repository(configured)
            logger.debug("%s: configured APKEditor %s exists=%s", self.risk_id, jar, jar.is_file())
            return jar if jar.is_file() else None
        jars = sorted(resolve_under_repository(APKEDITOR_DIR).glob("APKEditor*.jar"))
        logger.debug("%s: vendored APKEditor jars %s", self.risk_id, [jar.name for jar in jars])
        return jars[-1] if jars else None

    # Decode the merged APK, or base.apk when the app has no splits, into a fresh repackaged directory with apktool.
    def _run_apktool_decode(self, app_dir: Path) -> bool:
        merged_apk = app_dir / "merged.apk"
        base_apk = merged_apk if merged_apk.exists() else app_dir / "original" / "base.apk"
        if not base_apk.exists():
            logger.debug("%s: base APK missing at %s; cannot decode", self.risk_id, base_apk)
            return False
        repackaged_dir = app_dir / "repackaged"
        if repackaged_dir.exists():
            logger.debug("%s: clearing previous repackaged dir %s", self.risk_id, repackaged_dir)
            shutil.rmtree(repackaged_dir)
        repackaged_dir.mkdir(parents=True, exist_ok=True)
        decoded_dir = repackaged_dir / "base"
        cmd = ["apktool", "d", "-f", str(base_apk), "-o", str(decoded_dir)]
        logger.debug("%s: running argv=%s (cwd=%s)", self.risk_id, cmd, repackaged_dir)
        started = time.monotonic()
        result = subprocess.run(cmd, cwd=repackaged_dir, capture_output=True, text=True, stdin=subprocess.DEVNULL)
        logger.debug("%s: apktool d exited %s in %.2fs (decoded dir exists=%s, stderr head=%r)", self.risk_id, result.returncode, time.monotonic() - started, decoded_dir.exists(), (result.stderr or "")[:200])
        return result.returncode == 0 and decoded_dir.exists()

    # Mark the decoded manifest's application as debuggable unless it already declares the attribute.
    def _add_debuggable_to_manifest(self, app_dir: Path) -> bool:
        manifest = app_dir / "repackaged" / "base" / "AndroidManifest.xml"
        if not manifest.exists():
            logger.debug("%s: decoded manifest missing at %s", self.risk_id, manifest)
            return False
        content = manifest.read_text(encoding="utf-8")
        if "android:debuggable" in content:
            logger.debug("%s: manifest %s already declares android:debuggable; leaving it", self.risk_id, manifest)
            return True
        new_content, count = re.subn(
            r"(<application\b[^>]*)(>)",
            r'\1 android:debuggable="true"\2',
            content,
            count=1,
            flags=re.IGNORECASE,
        )
        if count == 0:
            logger.debug("%s: no <application> element found in %s", self.risk_id, manifest)
            return False
        manifest.write_text(new_content, encoding="utf-8")
        logger.debug("%s: added android:debuggable=true to %s", self.risk_id, manifest)
        return True

    # Prefix the decoded app_name string with RPK so the repackaged build is recognizable.
    def _change_apk_name(self, app_dir: Path) -> bool:
        strings = app_dir / "repackaged" / "base" / "res" / "values" / "strings.xml"
        if not strings.exists():
            logger.debug("%s: no strings.xml at %s; keeping app name", self.risk_id, strings)
            return True
        content = strings.read_text(encoding="utf-8")
        match = re.search(r'<string\s+name="app_name">(.*?)</string>', content, flags=re.DOTALL)
        if not match:
            logger.debug("%s: no app_name string in %s; keeping app name", self.risk_id, strings)
            return True
        current_name = match.group(1).strip()
        if "RPK" in current_name:
            logger.debug("%s: app_name %r already marked RPK", self.risk_id, current_name[:200])
            return True
        strings.write_text(content[: match.start(1)] + f"RPK {current_name}" + content[match.end(1) :], encoding="utf-8")
        logger.debug("%s: renamed app_name %r -> %r in %s", self.risk_id, current_name[:200], f"RPK {current_name}"[:200], strings)
        return True

    # Rebuild the decoded sources into repackaged.apk with apktool.
    def _rebuild_apk(self, app_dir: Path) -> bool:
        repackaged_dir = app_dir / "repackaged"
        if not (repackaged_dir / "base").exists():
            logger.debug("%s: decoded sources missing at %s; cannot rebuild", self.risk_id, repackaged_dir / "base")
            return False
        logger.debug("%s: running argv=%s (cwd=%s)", self.risk_id, ["apktool", "b", "-f", "base", "-o", "repackaged.apk"], repackaged_dir)
        started = time.monotonic()
        result = subprocess.run(
            ["apktool", "b", "-f", "base", "-o", "repackaged.apk"],
            cwd=repackaged_dir,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
        )
        logger.debug("%s: apktool b exited %s in %.2fs (repackaged.apk exists=%s, stderr head=%r)", self.risk_id, result.returncode, time.monotonic() - started, (repackaged_dir / "repackaged.apk").exists(), (result.stderr or "")[:200])
        return result.returncode == 0 and (repackaged_dir / "repackaged.apk").exists()

    # Sign the rebuilt APK in place with the test keystore.
    def _sign_apks(self, app_dir: Path, keystore: Path, password: str) -> bool:
        rebuilt = app_dir / "repackaged" / "repackaged.apk"
        logger.debug("%s: rebuilt APK %s exists=%s", self.risk_id, rebuilt, rebuilt.exists())
        return rebuilt.exists() and self._apksigner_sign(keystore, password, rebuilt, None)

    # Sign an APK in place or to an output path with apksigner.
    def _apksigner_sign(self, keystore: Path, password: str, apk_in: Path, apk_out: Path | None) -> bool:
        cmd = ["apksigner", "sign", "--ks", str(keystore), "--ks-pass", f"pass:{password}"]
        if apk_out is not None:
            cmd.extend(["--out", str(apk_out)])
        cmd.append(str(apk_in))
        logger.debug("%s: running argv=%s", self.risk_id, [REDACTED if part.startswith("pass:") else part for part in cmd])
        started = time.monotonic()
        result = subprocess.run(cmd, capture_output=True, text=True)
        logger.debug("%s: apksigner sign %s exited %s in %.2fs (stderr head=%r)", self.risk_id, apk_in.name, result.returncode, time.monotonic() - started, (result.stderr or "")[:200])
        return result.returncode == 0

    # Install the signed rebuilt APK, failing the stage with adb's reason.
    def _install_repackaged_apk(self, device_client, app_dir: Path, package_name: str) -> bool:
        installed, reason = self._install_apks(device_client, app_dir / "repackaged", package_name)
        if not installed:
            raise StageFailure(reason)
        return True

    # Uninstall the package, then install every APK in the directory with adb, returning success and adb's failure reason.
    def _install_apks(self, device_client, app_dir: Path, package_name: str, installer: str | None = None) -> tuple[bool, str]:
        apks = sorted(p for p in app_dir.iterdir() if p.is_file() and p.suffix == ".apk")
        logger.debug("%s[%s]: installing %s APKs from %s: %s (installer=%s)", self.risk_id, package_name, len(apks), app_dir, [p.name for p in apks], installer)
        if not apks:
            return False, f"No APKs to install in {app_dir}"
        self._uninstall_app(device_client, package_name)
        return install_apks(device_client.adb, apks, grant_permissions=True, installer=installer)

    # Replace the rebuilt app with the backed-up original APKs, so later tests run against the build that was tested;
    # fall back to the configured install method, which may fetch a different build, and record which one ran.
    def _restore_original(self, device_client, app_dir: Path, package_name: str, result: AndroidRiskRunResult) -> None:
        if not self._uninstall_app(device_client, package_name):
            result.metadata["restore"] = {"method": None}
            result.errors.append("Restore failed: the repackaged app could not be uninstalled.")
            logger.warning("Could not uninstall the repackaged %s; it is still on the device.", package_name)
            return
        installer = result.metadata.get("installer")
        restored, reason = self._install_apks(device_client, app_dir / "original", package_name, installer=installer)
        logger.debug("%s[%s]: backup restore of %s with installer=%s ok=%s (%s)", self.risk_id, package_name, app_dir / "original", installer, restored, reason)
        if not restored and installer:
            # The device may refuse to record another app as the installer; the build matters more than the installer.
            installer_failure = reason
            restored, reason = self._install_apks(device_client, app_dir / "original", package_name)
            logger.debug("%s[%s]: backup restore without installer ok=%s (%s)", self.risk_id, package_name, restored, reason)
            if restored:
                result.metadata["restore"] = {
                    "method": "backup",
                    "installer": None,
                    "note": f"The original build was reinstalled, but the device refused to record {installer} as its installer ({installer_failure}). Later tests ran against an adb install.",
                }
                self._record_restored_build(device_client, package_name, result)
                return
        if restored:
            result.metadata["restore"] = {"method": "backup", "installer": installer}
            self._record_restored_build(device_client, package_name, result)
            return
        backup_failure = reason
        logger.debug("%s[%s]: reinstalling the original from %s", self.risk_id, package_name, self.store.name)
        outcome = self.store.install(device_client.adb, device_client.make_driver, package_name, android_work_dir() / self.store.key / "diagnostics")
        logger.debug("%s[%s]: app store restore status=%s blocker=%s installer=%s", self.risk_id, package_name, outcome.status, outcome.blocker_code, outcome.installer)
        if outcome.status == "installed":
            result.metadata["restore"] = {
                "method": "app_store",
                "store": self.store.key,
                "installer": outcome.installer,
                "backup_failure": backup_failure,
                "note": f"The backed-up APKs could not be reinstalled, so the app was reinstalled from {self.store.name}. It may be a different build from the one tested.",
            }
            logger.warning("Restored %s from %s after the backup could not be reinstalled; it may be a different build.", package_name, self.store.name)
            self._record_restored_build(device_client, package_name, result)
            return
        app_store = {"store": self.store.key, "status": outcome.status, "blocker_code": outcome.blocker_code, "detail": outcome.detail}
        result.metadata["restore"] = {"method": None, "backup_failure": backup_failure, "app_store": app_store}
        result.errors.append(f"Restore failed: {backup_failure}")
        logger.warning("Could not restore %s from the backup or with the configured install method; it is not installed.", package_name)

    # Record the restored build's fingerprint and whether it is the build that was tested.
    def _record_restored_build(self, device_client, package_name: str, result: AndroidRiskRunResult) -> None:
        restore = result.metadata["restore"]
        original = result.metadata.get("installed_base_apk_sha256")
        restored = installed_base_apk_sha256(device_client.adb, package_name)
        restore["base_apk_sha256"] = restored
        restore["matches_original"] = restored == original if restored and original else None
        if restore["matches_original"] is False:
            logger.warning("The restored %s (%s) is a different build from the one tested (%s).", package_name, restored[:12], original[:12])

    # Uninstall the package, treating an absent package as success.
    def _uninstall_app(self, device_client, package_name: str) -> bool:
        code, out, err = device_client.adb.run(["uninstall", package_name], timeout=60)
        output = (out or err).lower()
        uninstalled = code == 0 or "not installed" in output or "unknown package" in output
        logger.debug("%s[%s]: uninstall exit %s ok=%s (output head=%r)", self.risk_id, package_name, code, uninstalled, output[:200])
        return uninstalled

    # Launch the repackaged app under screen recording and return its verdict and recording path.
    def _validate_with_appium(self, device_client, package_name: str, report_dir: Path, cfg: dict) -> tuple[str, Path | None]:
        if AppiumBy is None:
            logger.debug("%s[%s]: AppiumBy unavailable; validation cannot run", self.risk_id, package_name)
            return "ERROR: Appium-Python-Client not installed", None
        logger.debug("%s[%s]: creating Appium driver for validation", self.risk_id, package_name)
        driver = device_client.make_driver()
        recordings_dir = report_dir / "recordings"
        recordings_dir.mkdir(parents=True, exist_ok=True)
        recording_started = False
        recording_stopped = False
        try:
            recording_started = self._start_recording(driver)
            logger.debug("%s[%s]: screen recording started=%s (recordings_dir=%s)", self.risk_id, package_name, recording_started, recordings_dir)
            if recording_started:
                time.sleep(float(cfg.get("record_lead_in", 2)))
            verdict = self._check_app_after_launch(driver, package_name, cfg, report_dir / "screenshots")
            recording_path = None
            if recording_started:
                time.sleep(float(cfg.get("record_tail", 3)))
                recording_path = self._stop_recording(driver, package_name, recordings_dir)
                recording_stopped = True
            self._close_app(driver, package_name)
            return verdict, recording_path
        finally:
            if recording_started and not recording_stopped:
                try:
                    logger.debug("%s[%s]: stopping unfinished recording during cleanup", self.risk_id, package_name)
                    driver.stop_recording_screen()
                except Exception as exc:
                    logger.debug("%s[%s]: stopping recording during cleanup failed: %s", self.risk_id, package_name, exc, exc_info=True)
            logger.debug("%s[%s]: quitting Appium session %s", self.risk_id, package_name, getattr(driver, "session_id", None))
            driver.quit()

    # Return PASS when the app stays in the foreground after launch and a click, else a FAIL, INCONCLUSIVE or ERROR
    # reason, saving a screenshot after each so a tamper warning shown inside the app can be seen in the report.
    def _check_app_after_launch(self, driver, package_name: str, cfg: dict, screenshots_dir: Path | None = None) -> str:
        try:
            logger.debug("%s[%s]: activating repackaged app", self.risk_id, package_name)
            driver.activate_app(package_name)
        except Exception as exc:
            logger.debug("%s[%s]: activate_app failed: %s", self.risk_id, package_name, exc, exc_info=True)
            return f"ERROR: failed to launch app - {exc}"
        time.sleep(float(cfg.get("launch_wait", 8)))
        self._save_screenshot(driver, package_name, screenshots_dir, "after_launch")
        try:
            current_package = driver.current_package
            logger.debug("%s[%s]: foreground package after launch is %s", self.risk_id, package_name, current_package)
            current_package, denied = self._deny_permission_prompts(driver, package_name, current_package, cfg, screenshots_dir, "after_launch")
        except Exception as exc:
            logger.debug("%s[%s]: reading current_package failed: %s", self.risk_id, package_name, exc, exc_info=True)
            return f"ERROR: failed to read current package - {exc}"
        if current_package in PERMISSION_CONTROLLER_PACKAGES:
            return f"INCONCLUSIVE: a system permission prompt ({current_package}) still covered the app after {denied} denials"
        if current_package in STORE_APPS:
            logger.debug("%s[%s]: redirected to %s after launch", self.risk_id, package_name, current_package)
            self._close_app(driver, current_package)
            return f"FAIL: app fail to work after repackaging (redirected to {STORE_APPS[current_package]} after launch)"
        if current_package != package_name:
            logger.debug("%s[%s]: app not in foreground after launch (foreground=%s, prompts denied=%s)", self.risk_id, package_name, current_package, denied)
            if denied:
                # The app may have closed because a permission was denied, so the exit is not attributable to tampering.
                return f"INCONCLUSIVE: app left the foreground after a system permission prompt was denied (foreground: {current_package})"
            return "FAIL: app fail to work after repackaging (exited immediately after launch)"
        try:
            logger.debug("%s[%s]: find_elements by %s %r", self.risk_id, package_name, AppiumBy.ANDROID_UIAUTOMATOR, "new UiSelector().clickable(true)")
            buttons = driver.find_elements(AppiumBy.ANDROID_UIAUTOMATOR, "new UiSelector().clickable(true)")
        except Exception as exc:
            logger.debug("%s[%s]: find_elements failed: %s", self.risk_id, package_name, exc, exc_info=True)
            return f"ERROR: failed to search for buttons - {exc}"
        logger.debug("%s[%s]: found %s clickable elements", self.risk_id, package_name, len(buttons) if isinstance(buttons, list) else type(buttons).__name__)
        if buttons:
            try:
                logger.debug("%s[%s]: clicking first clickable element", self.risk_id, package_name)
                buttons[0].click()
            except Exception as exc:
                logger.debug("%s[%s]: click failed: %s", self.risk_id, package_name, exc, exc_info=True)
                return f"ERROR: failed to click button - {exc}"
            time.sleep(float(cfg.get("post_click_wait", 5)))
            self._save_screenshot(driver, package_name, screenshots_dir, "after_tap")
            try:
                current_package = driver.current_package
                logger.debug("%s[%s]: foreground package after click is %s", self.risk_id, package_name, current_package)
                current_package, denied_after_tap = self._deny_permission_prompts(driver, package_name, current_package, cfg, screenshots_dir, "after_tap")
            except Exception as exc:
                logger.debug("%s[%s]: reading current_package after click failed: %s", self.risk_id, package_name, exc, exc_info=True)
                return f"ERROR: failed to read current package after button click - {exc}"
            denied += denied_after_tap
            if current_package in PERMISSION_CONTROLLER_PACKAGES:
                return f"INCONCLUSIVE: a system permission prompt ({current_package}) still covered the app after {denied_after_tap} denials following the click"
            if current_package in STORE_APPS:
                logger.debug("%s[%s]: redirected to %s after click", self.risk_id, package_name, current_package)
                self._close_app(driver, current_package)
                return f"FAIL: app fail to work after repackaging (redirected to {STORE_APPS[current_package]} after clicking button)"
            if current_package != package_name:
                logger.debug("%s[%s]: app left foreground after click (foreground=%s, prompts denied=%s)", self.risk_id, package_name, current_package, denied_after_tap)
                if denied_after_tap:
                    return f"INCONCLUSIVE: app left the foreground after a system permission prompt was denied following the click (foreground: {current_package})"
                return "FAIL: app fail to work after repackaging (exited after clicking button)"
        else:
            logger.debug("%s[%s]: no clickable elements; judging on launch only", self.risk_id, package_name)
        if denied:
            return f"PASS: able to work after repackaging (denied {denied} system permission prompt{'s' if denied != 1 else ''})"
        return "PASS: able to work after repackaging"

    # Deny system permission prompts covering the app, up to max_permission_prompts, screenshotting what each one
    # uncovered; return the foreground package afterwards and how many prompts were denied.
    def _deny_permission_prompts(self, driver, package_name: str, current_package: str, cfg: dict, screenshots_dir: Path | None, stage: str) -> tuple[str, int]:
        denied = 0
        limit = int(cfg.get("max_permission_prompts", 3))
        while current_package in PERMISSION_CONTROLLER_PACKAGES and denied < limit:
            self._deny_permission_prompt(driver, package_name, current_package)
            denied += 1
            time.sleep(float(cfg.get("permission_prompt_wait", 2)))
            self._save_screenshot(driver, package_name, screenshots_dir, f"{stage}_prompt_{denied}_denied")
            current_package = driver.current_package
            logger.debug("%s[%s]: foreground package after denying prompt %s is %s", self.risk_id, package_name, denied, current_package)
        return current_package, denied

    # Tap the prompt's Deny button, or press Back where none is found, which Android also treats as a denial.
    def _deny_permission_prompt(self, driver, package_name: str, prompt_package: str) -> None:
        for selector in PERMISSION_DENY_SELECTORS:
            try:
                buttons = driver.find_elements(AppiumBy.ANDROID_UIAUTOMATOR, selector)
                if buttons:
                    logger.debug("%s[%s]: tapping Deny on the %s prompt (found by %s)", self.risk_id, package_name, prompt_package, selector)
                    buttons[0].click()
                    return
            except Exception as exc:
                logger.debug("%s[%s]: tapping Deny found by %s on the %s prompt failed: %s", self.risk_id, package_name, selector, prompt_package, exc, exc_info=True)
        logger.debug("%s[%s]: pressing Back to dismiss the %s prompt", self.risk_id, package_name, prompt_package)
        driver.back()

    # Save a screenshot of the device screen as <name>.png, ignoring failures; FLAG_SECURE windows come out black.
    def _save_screenshot(self, driver, package_name: str, screenshots_dir: Path | None, name: str) -> None:
        if screenshots_dir is None:
            return
        try:
            screenshots_dir.mkdir(parents=True, exist_ok=True)
            path = screenshots_dir / f"{name}.png"
            path.write_bytes(driver.get_screenshot_as_png())
            logger.debug("%s[%s]: saved screenshot %s", self.risk_id, package_name, path)
        except Exception as exc:
            logger.debug("%s[%s]: screenshot %s failed: %s", self.risk_id, package_name, name, exc, exc_info=True)

    # List the repackaged app's private files with run-as, which only works for a debuggable app, as the playbook's
    # last step does; save the listing and return whether it worked.
    def _check_debuggable(self, device_client, package_name: str, report_dir: Path) -> dict:
        code, out, err = device_client.adb.run(["shell", "run-as", package_name, "ls", "-R"], timeout=60)
        output = out or ""
        # run-as reports its own refusal ("run-as: package not debuggable: ...") on stderr or stdout depending on the device.
        refusal = next((line.strip() for line in f"{err or ''}\n{output}".splitlines() if line.strip().startswith("run-as:")), None)
        logger.debug("%s[%s]: run-as ls exit %s, %s chars, refusal=%r", self.risk_id, package_name, code, len(output), refusal)
        if code != 0 or refusal:
            return {"run_as": False, "detail": refusal or (err or "").strip() or f"run-as exited {code}"}
        report_dir.mkdir(parents=True, exist_ok=True)
        listing = report_dir / "run_as_ls.txt"
        listing.write_text(output)
        return {"run_as": True, "files_listed": sum(1 for line in output.splitlines() if line.strip() and not line.endswith(":")), "listing": str(listing)}

    # Start Appium screen recording, returning False when unsupported.
    def _start_recording(self, driver) -> bool:
        try:
            driver.start_recording_screen()
            return True
        except Exception as exc:
            logger.debug("%s: start_recording_screen failed: %s", self.risk_id, exc, exc_info=True)
            return False

    # Stop recording and save the MP4, returning its path or None on failure.
    def _stop_recording(self, driver, package_name: str, recordings_dir: Path) -> Path | None:
        try:
            encoded = driver.stop_recording_screen()
            path = recordings_dir / f"{package_name}.mp4"
            data = base64.b64decode(encoded)
            path.write_bytes(data)
            logger.debug("%s[%s]: saved recording %s (%s bytes)", self.risk_id, package_name, path, len(data))
            return path
        except Exception as exc:
            logger.debug("%s[%s]: stopping or saving recording failed: %s", self.risk_id, package_name, exc, exc_info=True)
            return None

    # Terminate the app, ignoring failures.
    def _close_app(self, driver, package_name: str) -> None:
        try:
            logger.debug("%s[%s]: terminating app", self.risk_id, package_name)
            driver.terminate_app(package_name)
        except Exception as exc:
            logger.debug("%s[%s]: terminate_app failed: %s", self.risk_id, package_name, exc, exc_info=True)
