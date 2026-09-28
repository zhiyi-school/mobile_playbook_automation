"""
Compatibility wrapper re-exporting the iOS configuration loader and validators.
"""

from __future__ import annotations

from mobile_playbook.platforms.ios.config import ConfigError, load_config, parse_config, validate_config

__all__ = ["ConfigError", "load_config", "parse_config", "validate_config"]
