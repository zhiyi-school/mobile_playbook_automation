from __future__ import annotations

# Compatibility wrapper. iOS implementation lives in mobile_playbook.platforms.ios.config.
from mobile_playbook.platforms.ios.config import ConfigError, load_config, parse_config, validate_config

__all__ = ["ConfigError", "load_config", "parse_config", "validate_config"]
