"""
Gets Burp's CA certificate, copies it to the device and installs it as a user CA through Settings with Appium, as
playbook feature-03 steps 04 to 06 do.
"""

from __future__ import annotations

import hashlib
import logging
import ssl
import tempfile
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mobile_playbook.platforms.android.device.settings_ui import UIAUTOMATOR, find_text, tap_element, text_matcher
from mobile_playbook.platforms.android.traffic_interception.device_proxy import TrafficInterceptionSetupError

logger = logging.getLogger(__name__)

# Burp serves its CA certificate in DER format at this address to clients of its proxy listener.
BURP_CA_URL = "http://burp/cert"
DEVICE_CA_PATH = "/sdcard/Download/ca.crt"
SETTINGS_PACKAGE = "com.android.settings"
SETTINGS_ACTION = "android.settings.SETTINGS"
TRUSTED_CREDENTIALS_USER_ACTION = "com.android.settings.TRUSTED_CREDENTIALS_USER"
EDIT_TEXT_CLASS = "android.widget.EditText"
POLL_SECONDS = 0.5
# Certificates Burp generates are about 1 KB; anything far outside this range is not one.
MIN_CA_BYTES = 200
MAX_CA_BYTES = 16384


@dataclass(frozen=True)
class CaSettingsLabels:
    """Text Settings and the file picker show on the way to installing a CA certificate; these are One UI's."""

    search_query: str = "CA certificate"
    ca_entry: tuple[str, ...] = ("CA certificate",)
    install_anyway: tuple[str, ...] = ("Install anyway",)
    folder: tuple[str, ...] = ("Download",)
    done: tuple[str, ...] = ("Done",)
    user_tab: tuple[str, ...] = ("User",)


@dataclass
class CaOutcome:
    """How the CA was made trusted, whether this run copied the file to the device, and the screenshots taken."""

    action: str | None = None
    pushed: bool = False
    evidence: list[dict] = field(default_factory=list)
    evidence_errors: list[str] = field(default_factory=list)

    # Describe the outcome for report metadata; evidence paths are listed with the risk's other evidence.
    def to_dict(self) -> dict[str, Any]:
        return {"action": self.action, "pushed": self.pushed, "evidence_errors": list(self.evidence_errors)}


# Download Burp's CA certificate through its proxy listener, as exporting it in DER format from Burp would give it.
def fetch_burp_ca(proxy_url: str, timeout: float = 10) -> bytes:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({"http": proxy_url}))
    with opener.open(BURP_CA_URL, timeout=timeout) as response:
        data = response.read(MAX_CA_BYTES + 1)
    return _checked_der(data, f"{BURP_CA_URL} through {proxy_url}")


# Read a CA certificate exported from Burp, in DER or PEM format, and return it in DER format.
def load_ca(path: Path) -> bytes:
    data = path.read_bytes()
    if data.lstrip().startswith(b"-----BEGIN CERTIFICATE-----"):
        data = ssl.PEM_cert_to_DER_cert(data.decode("ascii"))
    return _checked_der(data, str(path))


# Make the CA trusted as a user CA: when Settings does not list it yet, copy it to the device and install it through
# Settings, then confirm Settings lists it. A different file already at device_path is never overwritten. The outcome
# passed in is filled as setup goes, so a caller still has its screenshots and pushed file when setup fails.
def ensure_user_ca(driver, adb, ca: bytes, display_name: str, evidence_dir: Path, *, device_path: str = DEVICE_CA_PATH,
                   timeout: float = 20, labels: CaSettingsLabels | None = None, outcome: CaOutcome | None = None) -> CaOutcome:
    labels = labels or CaSettingsLabels()
    outcome = outcome if outcome is not None else CaOutcome()
    try:
        if user_ca_listed(driver, adb, display_name, labels, timeout):
            outcome.action = "already_installed"
            return outcome
    except Exception as exc:
        raise _failure(driver, evidence_dir, "CA_INSTALL_UI_FAILED", f"reading the user CA certificates failed: {exc}", outcome) from exc
    outcome.pushed = push_ca(adb, ca, device_path, evidence_dir, outcome)
    try:
        install_user_ca(driver, adb, device_path.rsplit("/", 1)[-1], labels, timeout, evidence_dir, outcome)
        listed = user_ca_listed(driver, adb, display_name, labels, timeout)
    except Exception as exc:
        raise _failure(driver, evidence_dir, "CA_INSTALL_UI_FAILED", f"installing the CA certificate through Settings failed: {exc}", outcome) from exc
    _screenshot(driver, evidence_dir / "ca_user_certificates.png", "ca_user_certificates", "User CA certificates after installing Burp's CA", outcome)
    if not listed:
        raise _failure(driver, evidence_dir, "CA_NOT_TRUSTED", f"{display_name!r} is not listed under User certificates after installing it", outcome)
    outcome.action = "installed"
    return outcome


# Copy the CA to the device with `adb push`, returning whether it was copied; an identical file already there is left
# in place, and a different one stops the run rather than being replaced.
def push_ca(adb, ca: bytes, device_path: str, evidence_dir: Path, outcome: CaOutcome) -> bool:
    code, out, err = adb.run(["shell", "sha256sum", device_path])
    if code == 0:
        if (out or "").split()[:1] == [hashlib.sha256(ca).hexdigest()]:
            logger.debug("android traffic setup: %s already holds this CA", device_path)
            return False
        raise TrafficInterceptionSetupError(
            "CA_PATH_OCCUPIED",
            f"{device_path} already holds a different file; move it aside or set another CA path",
            outcome.to_dict(),
        )
    if "No such file" not in (err or "") + (out or ""):
        raise TrafficInterceptionSetupError("CA_PUSH_FAILED", f"checking {device_path} failed: {err or out}", outcome.to_dict())
    with tempfile.TemporaryDirectory() as directory:
        local = Path(directory) / "ca.crt"
        local.write_bytes(ca)
        code, out, err = adb.run(["push", str(local), device_path])
    if code != 0:
        raise TrafficInterceptionSetupError("CA_PUSH_FAILED", f"adb push to {device_path} failed: {err or out}", outcome.to_dict())
    logger.debug("android traffic setup: pushed the CA to %s", device_path)
    return True


# Delete the CA file from the device when this run copied it there, leaving a file that was there before.
def remove_pushed_ca(adb, outcome: CaOutcome, device_path: str = DEVICE_CA_PATH) -> None:
    if outcome.pushed:
        adb.run(["shell", "rm", "-f", device_path])


# Open View security certificates on its User tab and return whether it lists a certificate named display_name.
def user_ca_listed(driver, adb, display_name: str, labels: CaSettingsLabels, timeout: float) -> bool:
    _open_settings(adb, ["-a", TRUSTED_CREDENTIALS_USER_ACTION])
    tab = find_text(driver, labels.user_tab, timeout, scroll=False)
    if tab is None:
        raise RuntimeError("the User tab of View security certificates did not open")
    tap_element(adb, tab)
    return find_text(driver, (display_name,), min(timeout, 5)) is not None


# Install the file in the device's Download folder as a CA certificate: search Settings for "CA certificate", open it,
# accept the warning with Install anyway, then choose the file in the picker.
def install_user_ca(driver, adb, file_name: str, labels: CaSettingsLabels, timeout: float, evidence_dir: Path, outcome: CaOutcome) -> None:
    _open_settings(adb, ["-a", SETTINGS_ACTION])
    _search(driver, adb, labels.search_query, timeout)
    deadline = time.monotonic() + timeout
    # The search result opens Install from device storage, whose own CA certificate entry opens the warning.
    while (install := find_text(driver, labels.install_anyway, 0, scroll=False)) is None:
        entry = _labelled(driver, labels.ca_entry)
        if entry is not None:
            tap_element(adb, entry)
        if time.monotonic() >= deadline:
            raise RuntimeError(f"the CA certificate warning with {labels.install_anyway[0]!r} did not appear")
        time.sleep(POLL_SECONDS)
    _screenshot(driver, evidence_dir / "ca_install_warning.png", "ca_install_warning", "Settings warning before installing a CA certificate", outcome)
    tap_element(adb, install)
    _choose_file(driver, adb, file_name, labels, timeout)


# Type the query into Settings search, opening the search field first when Settings shows its main page.
def _search(driver, adb, query: str, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while not (fields := driver.find_elements("class name", EDIT_TEXT_CLASS)):
        button = driver.find_elements(UIAUTOMATOR, 'new UiSelector().descriptionMatches("(?i)search")')
        if button:
            tap_element(adb, button[0])
        if time.monotonic() >= deadline:
            raise RuntimeError("the Settings search field did not appear")
        time.sleep(POLL_SECONDS)
    # Settings keeps an earlier search's text when it reopens on its search page.
    fields[0].clear()
    tap_element(adb, fields[0])
    code, out, err = adb.run(["shell", "input", "text", query.replace(" ", "%s")])
    if code != 0:
        raise RuntimeError(f"typing {query!r} into Settings search failed: {err or out}")


# Select the file in the picker, opening the Download folder when the picker starts elsewhere, and confirm the choice
# with Done where the picker asks for it.
def _choose_file(driver, adb, file_name: str, labels: CaSettingsLabels, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while (entry := find_text(driver, (file_name,), 0)) is None:
        folder = find_text(driver, labels.folder, 0, scroll=False)
        if folder is not None:
            tap_element(adb, folder)
        if time.monotonic() >= deadline:
            raise RuntimeError(f"{file_name} was not found in the file picker")
        time.sleep(POLL_SECONDS)
    tap_element(adb, entry)
    done = find_text(driver, labels.done, min(timeout, 5), scroll=False)
    if done is not None:
        tap_element(adb, done)


# Return the first element showing one of the texts that is not a text field, so a search query is not taken for its
# result.
def _labelled(driver, texts: tuple[str, ...]):
    for element in driver.find_elements(UIAUTOMATOR, text_matcher(texts)):
        if element.get_attribute("class") != EDIT_TEXT_CLASS:
            return element
    return None


# Start a Settings page fresh; Settings is stopped first because it otherwise reopens on the page it was left on.
def _open_settings(adb, intent: list[str]) -> None:
    adb.run(["shell", "am", "force-stop", SETTINGS_PACKAGE])
    code, out, err = adb.run(["shell", "am", "start", *intent])
    if code != 0 or "Error" in (out or ""):
        raise RuntimeError(f"am start {' '.join(intent)} failed: {err or out}")


# Return the certificate unchanged when it looks like one DER certificate, otherwise raise naming its source.
def _checked_der(data: bytes, source: str) -> bytes:
    # DER certificates are an ASN.1 SEQUENCE, which starts with 0x30.
    if not (MIN_CA_BYTES <= len(data) <= MAX_CA_BYTES and data[:1] == b"\x30"):
        raise TrafficInterceptionSetupError("CA_UNAVAILABLE", f"{source} did not return a DER certificate ({len(data)} bytes)")
    return data


# Save a screenshot and the page source for a failed step, and return the setup error carrying them.
def _failure(driver, evidence_dir: Path, status: str, message: str, outcome: CaOutcome) -> TrafficInterceptionSetupError:
    _screenshot(driver, evidence_dir / "ca_setup_failure.png", "ca_setup_failure", "Screen when CA setup failed", outcome)
    try:
        (evidence_dir / "ca_setup_failure.xml").write_text(driver.page_source)
    except Exception as exc:
        logger.debug("android traffic setup: saving the page source failed: %s", exc, exc_info=True)
    logger.debug("android traffic setup: %s: %s", status, message)
    return TrafficInterceptionSetupError(status, message, outcome.to_dict())


# Add a screenshot to the evidence when Appium saves it, otherwise record why it is missing.
def _screenshot(driver, path: Path, kind: str, label: str, outcome: CaOutcome) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        saved = driver.get_screenshot_as_file(str(path))
    except Exception as exc:
        logger.debug("android traffic setup: screenshot %s failed: %s", path.name, exc, exc_info=True)
        saved, reason = False, str(exc)
    else:
        reason = "Appium did not save the file"
    if saved:
        outcome.evidence.append({"kind": kind, "path": str(path), "label": label})
    else:
        outcome.evidence_errors.append(f"{path.name}: {reason}")
