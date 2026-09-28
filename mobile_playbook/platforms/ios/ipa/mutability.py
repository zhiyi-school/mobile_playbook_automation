"""
Compatibility re-export of the Mach-O encryption and executable inspection helpers.
"""

from mobile_playbook.platforms.ios.mutations.mutability import detect_macho_encryption, inspect_main_executable

__all__ = ["detect_macho_encryption", "inspect_main_executable"]
