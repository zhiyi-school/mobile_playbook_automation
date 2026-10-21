"""
Recognizes an app's on-screen warning that USB debugging or developer options are on, from its Appium page source.
"""

from __future__ import annotations

DEBUGGING_KEYWORDS = ["developer options", "debugging", "usb debugging", "debugging detected"]


# Return the debugging-warning keywords that appear in a page source, matched case-insensitively.
def debugging_keywords(page_source: str) -> list[str]:
    source = page_source.lower()
    return [keyword for keyword in DEBUGGING_KEYWORDS if keyword in source]
