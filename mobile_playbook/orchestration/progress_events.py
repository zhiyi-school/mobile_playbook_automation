from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol


@dataclass(frozen=True)
class ProgressEvent:
    event_type: str
    message: str
    payload: dict[str, Any] = field(default_factory=dict)
    timestamp: str = field(default_factory=lambda: datetime.now().astimezone().isoformat())


class ProgressSink(Protocol):
    def publish(self, event: ProgressEvent) -> None:
        ...
