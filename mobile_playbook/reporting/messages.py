from __future__ import annotations

MESSAGE_PREFIX = "Message: "


def clean_message(text: str, max_length: int = 300) -> str:
    """Reduce a raw error/status string to one short, human-readable line.

    Selenium/Appium exceptions stringify as multi-line blocks such as
    "Message: <text>\\nStacktrace:\\n...java-style trace...". Only the first
    line ever carries the human-meaningful part, so this keeps just that,
    strips the "Message: " prefix Selenium adds, and caps the length. Text
    that is already a clean single-line sentence passes through unchanged.
    The original untouched text always still lives in logs.txt/report.json.
    """
    if not text:
        return text
    first_line = text.splitlines()[0].strip()
    if first_line.startswith(MESSAGE_PREFIX):
        first_line = first_line[len(MESSAGE_PREFIX) :].strip()
    if len(first_line) > max_length:
        first_line = first_line[: max_length - 1].rstrip() + "…"
    return first_line
