"""
Fake-adb answers for the commands `install_apks` sends while copying APKs to the device before installing them.
"""

from __future__ import annotations

from pathlib import Path


# Answer an `adb push` or the size check after it from the sizes copied so far, or return None for other commands.
def answer_apk_copy(args: list[str], copied: dict[str, int]) -> tuple[int, str, str] | None:
    if args[0] == "push":
        copied[args[2]] = Path(args[1]).stat().st_size
        return 0, "1 file pushed", ""
    if args[:2] == ["shell", "stat"]:
        size = copied.get(args[-1])
        return (0, f"{size}\n", "") if size is not None else (1, "", "No such file or directory")
    return None


# Report whether the command is the package manager install that follows the copy.
def is_pm_install(args: list[str]) -> bool:
    return list(args[:3]) == ["shell", "pm", "install"]
