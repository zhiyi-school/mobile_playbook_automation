"""
Compatibility wrapper re-exporting the iOS Appium device client.
"""

from __future__ import annotations

from mobile_playbook.platforms.ios.device_client import AppiumDeviceClient

__all__ = ["AppiumDeviceClient"]
