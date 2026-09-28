"""
Shortens raw error and status strings for reports.
"""

from __future__ import annotations

MESSAGE_PREFIX = "Message: "


# Reduces a raw error string, such as an Appium exception, to its capped first line without the Message prefix.
def clean_message(text: str, max_length: int = 300) -> str:
    if not text:
        return text
    first_line = text.splitlines()[0].strip()
    if first_line.startswith(MESSAGE_PREFIX):
        first_line = first_line[len(MESSAGE_PREFIX) :].strip()
    if len(first_line) > max_length:
        first_line = first_line[: max_length - 1].rstrip() + "…"
    return first_line
