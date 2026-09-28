"""
Creates UiAutomator2 Appium sessions for Android devices.
"""

from __future__ import annotations

import logging
import time

from mobile_playbook.logging_setup import redacted

logger = logging.getLogger(__name__)

try:
    from appium import webdriver
    from appium.options.android import UiAutomator2Options
except ImportError:
    logger.debug("android appium: Appium-Python-Client import failed", exc_info=True)
    webdriver = None
    UiAutomator2Options = None


# Report whether the Appium Python client imported successfully.
def appium_available() -> bool:
    available = webdriver is not None and UiAutomator2Options is not None
    logger.debug("android appium: client available=%s", available)
    return available


# Start a no-reset UiAutomator2 Appium session, optionally launching a package and activity.
def create_appium_driver(server_url: str, app_package: str | None = None, app_activity: str | None = None):
    if not appium_available():
        raise RuntimeError("Appium-Python-Client is not installed. Run 'pip install Appium-Python-Client'.")

    options = UiAutomator2Options()
    options.platform_name = "Android"
    options.automation_name = "UiAutomator2"
    options.no_reset = True
    options.new_command_timeout = 120
    if app_package:
        options.app_package = app_package
    if app_activity:
        options.app_activity = app_activity
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug("android appium: creating session at %s with capabilities %s", server_url, redacted(options.to_capabilities()))
    started = time.monotonic()
    try:
        driver = webdriver.Remote(server_url, options=options)
    except Exception as exc:
        logger.debug("android appium: session creation at %s failed after %.2fs", server_url, time.monotonic() - started, exc_info=True)
        raise RuntimeError(f"failed to start Appium session at {server_url}: {exc}") from exc
    logger.debug(
        "android appium: session %s created at %s in %.2fs",
        getattr(driver, "session_id", None),
        server_url,
        time.monotonic() - started,
    )
    return driver
