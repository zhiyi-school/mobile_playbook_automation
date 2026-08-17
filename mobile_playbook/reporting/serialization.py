from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


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
    def to_dict(self) -> dict[str, Any]:
        return serialize(asdict(self))
