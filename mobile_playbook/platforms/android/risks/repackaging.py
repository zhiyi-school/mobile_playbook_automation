from __future__ import annotations

import base64
import re
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path

from mobile_playbook.platforms.android.models import AndroidRiskRunResult
from mobile_playbook.platforms.android.risks.base import AndroidRisk

try:
    from appium.webdriver.common.appiumby import AppiumBy
except ImportError:
    AppiumBy = None


class AndroidRepackagingRisk(AndroidRisk):
    risk_id = "android-feature1-risk2"
    name = "Android Repackaging Test"
    test_case_id = "repackaging"
    test_case_type = "apk_decode_patch_resign_validate"
    requires = ["adb", "apktool", "apksigner", "keytool", "appium"]

    def run(self, app_config, global_config, device_client, report_writer):
        started_at = datetime.now().astimezone()
        report_dir = report_writer.test_report_dir(app_config.id, self.risk_id, self.test_case_id, platform="android")
        cfg = {**global_config.repackaging, **(app_config.risks.get(self.risk_id) or {})}
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
        try:
            app_dir = Path(cfg.get("work_dir", "work/android/repackaging")) / app_config.package_name.replace(".", "_")
            app_dir.mkdir(parents=True, exist_ok=True)
            keystore = Path(cfg.get("keystore_path") or app_dir.parent / "release.keystore")
            keystore_alias = cfg.get("keystore_alias", "mobileplaybook")
            keystore_pass = cfg.get("keystore_pass", "password")

            stages = [
                ("backup_apks", lambda: self._backup_apks(device_client, app_config.package_name, app_dir)),
                ("ensure_keystore", lambda: self._ensure_keystore(keystore, keystore_alias, keystore_pass)),
                ("apktool_decode", lambda: self._run_apktool_decode(app_dir)),
                ("add_debuggable_to_manifest", lambda: self._add_debuggable_to_manifest(app_dir)),
                ("change_apk_name", lambda: self._change_apk_name(app_dir)),
                ("rebuild_apk", lambda: self._rebuild_apk(app_dir)),
                ("sign_apks", lambda: self._sign_apks(app_dir, keystore, keystore_pass)),
                ("install_repackaged_apks", lambda: self._install_apks(device_client, app_dir / "repackaged", app_config.package_name)),
            ]
            failed_stage = None
            for stage, action in stages:
                if not action():
                    failed_stage = stage
                    break
            if failed_stage is None:
                verdict, recording_path = self._validate_with_appium(device_client, app_config.package_name, report_dir, cfg)
                result.metadata["validation_verdict"] = verdict
                if recording_path is not None:
                    result.evidence.append({"kind": "screen_recording", "path": str(recording_path), "label": "Repackaged launch recording"})
                result.final_status = "REPACKAGING_SURVIVED" if verdict.startswith("PASS") else "REPACKAGING_BLOCKED"
                result.verdict = "At Risk" if verdict.startswith("PASS") else "Reduced Risk"
            else:
                result.final_status = "REPACKAGING_FAILED"
                result.errors.append(f"Failed at stage: {failed_stage}")
            if bool(cfg.get("restore_original_after_test", True)):
                self._install_apks(device_client, app_dir / "original", app_config.package_name)
            result.metadata["work_dir"] = str(app_dir)
        except Exception as exc:
            result.final_status = "FAILED"
            result.errors.append(str(exc))
        finally:
            result.timestamp_end = datetime.now().astimezone().isoformat()
            report_writer.write_result(result, report_dir)
        return result

    def _backup_apks(self, device_client, package_name: str, app_dir: Path) -> bool:
        code, output, error = device_client.adb.run(["shell", "pm", "path", package_name])
        if code != 0 or not output:
            return False
        original_dir = app_dir / "original"
        original_dir.mkdir(parents=True, exist_ok=True)
        ok = True
        for line in output.splitlines():
            if not line.startswith("package:"):
                continue
            apk_path = line[len("package:") :].strip()
            destination = original_dir / Path(apk_path).name
            if destination.exists():
                destination.unlink()
            pull_code, _, _ = device_client.adb.run(["pull", apk_path, str(destination)], timeout=120)
            ok = ok and pull_code == 0
        return ok and any(original_dir.glob("*.apk"))

    def _ensure_keystore(self, keystore: Path, alias: str, password: str) -> bool:
        if keystore.exists():
            return True
        keystore.parent.mkdir(parents=True, exist_ok=True)
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
        except (subprocess.CalledProcessError, FileNotFoundError):
            return False
        return True

    def _run_apktool_decode(self, app_dir: Path) -> bool:
        original_dir = app_dir / "original"
        base_apk = original_dir / "base.apk"
        if not base_apk.exists():
            return False
        repackaged_dir = app_dir / "repackaged"
        if repackaged_dir.exists():
            shutil.rmtree(repackaged_dir)
        repackaged_dir.mkdir(parents=True, exist_ok=True)
        decoded_dir = repackaged_dir / "base"
        cmd = ["apktool", "d", "-f", str(base_apk), "-o", str(decoded_dir)]
        result = subprocess.run(cmd, cwd=repackaged_dir, capture_output=True, text=True, stdin=subprocess.DEVNULL)
        return result.returncode == 0 and decoded_dir.exists()

    def _add_debuggable_to_manifest(self, app_dir: Path) -> bool:
        manifest = app_dir / "repackaged" / "base" / "AndroidManifest.xml"
        if not manifest.exists():
            return False
        content = manifest.read_text(encoding="utf-8")
        if "android:debuggable" in content:
            return True
        new_content, count = re.subn(
            r"(<application\b[^>]*)(>)",
            r'\1 android:debuggable="true"\2',
            content,
            count=1,
            flags=re.IGNORECASE,
        )
        if count == 0:
            return False
        manifest.write_text(new_content, encoding="utf-8")
        return True

    def _change_apk_name(self, app_dir: Path) -> bool:
        strings = app_dir / "repackaged" / "base" / "res" / "values" / "strings.xml"
        if not strings.exists():
            return True
        content = strings.read_text(encoding="utf-8")
        match = re.search(r'<string\s+name="app_name">(.*?)</string>', content, flags=re.DOTALL)
        if not match:
            return True
        current_name = match.group(1).strip()
        if "RPK" in current_name:
            return True
        strings.write_text(content[: match.start(1)] + f"RPK {current_name}" + content[match.end(1) :], encoding="utf-8")
        return True

    def _rebuild_apk(self, app_dir: Path) -> bool:
        repackaged_dir = app_dir / "repackaged"
        if not (repackaged_dir / "base").exists():
            return False
        result = subprocess.run(
            ["apktool", "b", "-f", "base", "-o", "repackaged.apk"],
            cwd=repackaged_dir,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
        )
        return result.returncode == 0 and (repackaged_dir / "repackaged.apk").exists()

    def _sign_apks(self, app_dir: Path, keystore: Path, password: str) -> bool:
        original_dir = app_dir / "original"
        repackaged_dir = app_dir / "repackaged"
        split_apks = sorted(p for p in original_dir.iterdir() if p.is_file() and p.suffix == ".apk" and p.name != "base.apk")
        for apk in split_apks:
            if not self._apksigner_sign(keystore, password, apk, repackaged_dir / apk.name):
                return False
        rebuilt = repackaged_dir / "repackaged.apk"
        return rebuilt.exists() and self._apksigner_sign(keystore, password, rebuilt, None)

    def _apksigner_sign(self, keystore: Path, password: str, apk_in: Path, apk_out: Path | None) -> bool:
        cmd = ["apksigner", "sign", "--ks", str(keystore), "--ks-pass", f"pass:{password}"]
        if apk_out is not None:
            cmd.extend(["--out", str(apk_out)])
        cmd.append(str(apk_in))
        result = subprocess.run(cmd, capture_output=True, text=True)
        return result.returncode == 0

    def _install_apks(self, device_client, app_dir: Path, package_name: str) -> bool:
        apks = sorted(p for p in app_dir.iterdir() if p.is_file() and p.suffix == ".apk")
        if not apks:
            return False
        self._uninstall_app(device_client, package_name)
        if len(apks) == 1:
            code, _, _ = device_client.adb.run(["install", "-r", "-g", str(apks[0])], timeout=180)
            return code == 0
        code, _, _ = device_client.adb.run(["install-multiple", "-r", "-g", *[str(apk) for apk in apks]], timeout=180)
        return code == 0

    def _uninstall_app(self, device_client, package_name: str) -> bool:
        code, out, err = device_client.adb.run(["uninstall", package_name], timeout=60)
        output = (out or err).lower()
        return code == 0 or "not installed" in output or "unknown package" in output

    def _validate_with_appium(self, device_client, package_name: str, report_dir: Path, cfg: dict) -> tuple[str, Path | None]:
        if AppiumBy is None:
            return "ERROR: Appium-Python-Client not installed", None
        driver = device_client.make_driver()
        recordings_dir = report_dir / "recordings"
        recordings_dir.mkdir(parents=True, exist_ok=True)
        recording_started = False
        recording_stopped = False
        try:
            recording_started = self._start_recording(driver)
            if recording_started:
                time.sleep(float(cfg.get("record_lead_in", 2)))
            verdict = self._check_app_after_launch(driver, package_name, cfg)
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
                    driver.stop_recording_screen()
                except Exception:
                    pass
            driver.quit()

    def _check_app_after_launch(self, driver, package_name: str, cfg: dict) -> str:
        try:
            driver.activate_app(package_name)
        except Exception as exc:
            return f"ERROR: failed to launch app - {exc}"
        time.sleep(float(cfg.get("launch_wait", 8)))
        try:
            current_package = driver.current_package
        except Exception as exc:
            return f"ERROR: failed to read current package - {exc}"
        if self._is_play_store_block(driver, current_package):
            self._close_app(driver, "com.android.vending")
            return "FAIL: app fail to work after repackaging (redirected to Play Store after launch)"
        if current_package != package_name:
            return "FAIL: app fail to work after repackaging (exited immediately after launch)"
        try:
            buttons = driver.find_elements(AppiumBy.ANDROID_UIAUTOMATOR, "new UiSelector().clickable(true)")
        except Exception as exc:
            return f"ERROR: failed to search for buttons - {exc}"
        if buttons:
            try:
                buttons[0].click()
            except Exception as exc:
                return f"ERROR: failed to click button - {exc}"
            time.sleep(float(cfg.get("post_click_wait", 5)))
            try:
                current_package = driver.current_package
            except Exception as exc:
                return f"ERROR: failed to read current package after button click - {exc}"
            if self._is_play_store_block(driver, current_package):
                self._close_app(driver, "com.android.vending")
                return "FAIL: app fail to work after repackaging (redirected to Play Store after clicking button)"
            if current_package != package_name:
                return "FAIL: app fail to work after repackaging (exited after clicking button)"
        return "PASS: able to work after repackaging"

    def _is_play_store_block(self, driver, current_package: str) -> bool:
        if current_package != "com.android.vending":
            return False
        try:
            return "get this app from play" in driver.page_source.lower()
        except Exception:
            return True

    def _start_recording(self, driver) -> bool:
        try:
            driver.start_recording_screen()
            return True
        except Exception:
            return False

    def _stop_recording(self, driver, package_name: str, recordings_dir: Path) -> Path | None:
        try:
            encoded = driver.stop_recording_screen()
            path = recordings_dir / f"{package_name}.mp4"
            path.write_bytes(base64.b64decode(encoded))
            return path
        except Exception:
            return None

    def _close_app(self, driver, package_name: str) -> None:
        try:
            driver.terminate_app(package_name)
        except Exception:
            pass
