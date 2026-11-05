"""
A fake Appium driver showing one Settings screen at a time, whose elements are tapped through a fake adb `input tap`.
"""

from __future__ import annotations

import re

# Elements register their centre so an adb `input tap` at that point reaches the element the code tapped by its bounds.
TAP_TARGETS: dict[tuple[int, int], "Element"] = {}


class Element:
    _next = 0

    def __init__(self, text="", cls="android.widget.TextView", checked=None, enabled=True, offscreen=False, on_click=None, desc="",
                 package="com.android.settings"):
        self.text = text
        self.package = package
        self.desc = desc
        self.cls = cls
        self.checked = checked
        self.enabled = enabled
        self.offscreen = offscreen
        self.on_click = on_click
        self.clicks = 0
        Element._next += 20
        self.cx, self.cy = 100, Element._next
        TAP_TARGETS[(self.cx, self.cy)] = self

    def get_attribute(self, name):
        if name == "bounds":
            return f"[{self.cx - 5},{self.cy - 5}][{self.cx + 5},{self.cy + 5}]"
        if name in ("class", "text", "package"):
            return {"class": self.cls, "text": self.text, "package": self.package}[name]
        value = {"checked": self.checked, "enabled": self.enabled}[name]
        return None if value is None else str(value).lower()

    def clear(self):
        self.text = ""

    def tapped(self):
        self.clicks += 1
        if self.on_click:
            self.on_click(self)

    # Appium click is unused now; the code taps through adb, but keep it so a stray call is still noticed.
    def click(self):
        self.tapped()


class SettingsDriver:
    """Fake Appium driver showing one Settings screen at a time; UiSelector text regexes are evaluated as Java would."""

    def __init__(self, screens, screen, scrollable=True, screenshots_work=True):
        self.page_source = "<hierarchy/>"
        self.screens = screens
        self.screen = screen
        self.scrollable = scrollable
        self.screenshots_work = screenshots_work
        self.screenshots: list[str] = []
        self.polls = 0

    def find_elements(self, by, value):
        self.polls += 1
        elements = self.screens.get(self.screen, [])
        if by == "class name":
            return [element for element in elements if element.cls == value and not element.offscreen]
        assert by == "-android uiautomator"
        scrolling = value.startswith("new UiScrollable")
        if scrolling and not self.scrollable:
            raise RuntimeError("UiScrollable found no scrollable view")
        if "descriptionMatches" in value:
            pattern = python_pattern(value.replace("descriptionMatches", "textMatches"))
            return [element for element in elements if pattern.fullmatch(element.desc) and not element.offscreen]
        pattern = python_pattern(value)
        return [element for element in elements if pattern.fullmatch(element.text) and (scrolling or not element.offscreen)]

    def get_screenshot_as_file(self, path):
        self.screenshots.append(path.rsplit("/", 1)[1])
        return self.screenshots_work


def python_pattern(selector: str) -> re.Pattern:
    literal = re.search(r'textMatches\("((?:[^"\\]|\\.)*)"\)', selector)[1]
    java = literal.replace('\\"', '"').replace("\\\\", "\\")
    assert java.startswith("(?i)")
    python = re.sub(r"\\Q(.*?)\\E", lambda match: re.escape(match[1]), java[4:])
    return re.compile(python, re.IGNORECASE)
