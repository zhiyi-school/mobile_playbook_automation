"""
Synthetic screenshots and recognised words for the adb debugging prompt.
"""

from __future__ import annotations

import struct

from mobile_playbook.platforms.android.device.debugging_prompt import Word


def png(width: int, height: int) -> bytes:
    return b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + struct.pack(">II", width, height) + b"\x08\x06\x00\x00\x00rest"


def line(number: int, top: int, *texts: str, left: int = 100, gap: int = 20, width: int = 120, height: int = 40) -> list[Word]:
    return [Word(text, left + index * (width + gap), top, width, height, (1, 1, 1, number)) for index, text in enumerate(texts)]


def prompt_words(button_top: int = 1230, allow_left: int = 800) -> list[Word]:
    return [
        *line(1, 900, "Allow", "USB", "debugging?"),
        *line(2, 980, "The", "computer's", "RSA", "key", "fingerprint", "is:"),
        *line(3, 1040, "AB:CD:EF:01:23:45:67:89:AB:CD:EF:01:23:45:67:89"),
        *line(4, 1100, "Always", "allow", "from", "this", "computer"),
        *line(5, button_top, "Cancel", left=500),
        Word("Allow", allow_left, button_top, 100, 40, (1, 1, 1, 5)),
    ]
