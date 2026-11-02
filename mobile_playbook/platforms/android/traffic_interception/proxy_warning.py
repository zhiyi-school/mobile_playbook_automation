"""
Recognizes an app's on-screen warning that an HTTP proxy or VPN is active, as playbook feature-03 control 02 shows,
from the visible text in its Appium page source.
"""

from __future__ import annotations

import html
import re

PROXY_WARNING_KEYWORDS = ["proxy", "vpn", "man-in-the-middle", "insecure network", "network is not secure"]
# Only what the screen shows is read, so resource IDs and class names that happen to contain a keyword do not count.
VISIBLE_TEXT_RE = re.compile(r'\b(?:text|content-desc)="([^"]*)"')


# Return the proxy-warning keywords in the page source's visible text, matched case-insensitively.
def proxy_warning_keywords(page_source: str) -> list[str]:
    visible = " ".join(html.unescape(value) for value in VISIBLE_TEXT_RE.findall(page_source or "")).lower()
    return [keyword for keyword in PROXY_WARNING_KEYWORDS if keyword in visible]
