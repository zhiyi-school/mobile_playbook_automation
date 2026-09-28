"""
JSON-ready serialization for report dataclasses.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


# Converts paths, objects with to_dict, sequences and dicts into JSON-ready values.
def serialize(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if hasattr(value, "to_dict"):
        return value.to_dict()
    if isinstance(value, list):
        return [serialize(v) for v in value]
    if isinstance(value, tuple):
        return [serialize(v) for v in value]
    if isinstance(value, dict):
        return {str(k): serialize(v) for k, v in value.items()}
    return value


@dataclass
class SerializableDataclass:
    """Dataclass base that serializes itself to a JSON-ready dict."""

    # Returns the dataclass fields as a JSON-ready dict.
    def to_dict(self) -> dict[str, Any]:
        return serialize(asdict(self))
