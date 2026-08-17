from __future__ import annotations

try:
    from appium import webdriver
    from appium.options.android import UiAutomator2Options
except ImportError:
    webdriver = None
    UiAutomator2Options = None


def appium_available() -> bool:
    return webdriver is not None and UiAutomator2Options is not None


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
    try:
        return webdriver.Remote(server_url, options=options)
    except Exception as exc:
        raise RuntimeError(f"failed to start Appium session at {server_url}: {exc}") from exc
