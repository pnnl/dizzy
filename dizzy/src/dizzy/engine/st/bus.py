"""In-memory telemetry ring — observability stream for the logs tab.

Deliberately ephemeral: a bounded per-process deque, reset on restart, never
persisted (telemetry is transport/observation, not truth — the event store is
the truth). The engine emits procedure/projection/policy start-end records
into it; host code adds worker and sink records; /api/logs streams it by
cursor to the UI.
"""

from __future__ import annotations

import threading
from collections import deque
from datetime import UTC, datetime
from typing import Any


class TelemetryBus:
    def __init__(self, maxlen: int = 2000):
        self._buf: deque = deque(maxlen=maxlen)
        self._lock = threading.Lock()
        self._seq = 0

    def emit(self, record: dict[str, Any]) -> None:
        with self._lock:
            self._seq += 1
            self._buf.append(
                {
                    "seq": self._seq,
                    "ts": datetime.now(UTC).isoformat(),
                    **record,
                }
            )

    def since(
        self, after: int = 0, limit: int = 500, kind: str | None = None
    ) -> list[dict[str, Any]]:
        with self._lock:
            items = [
                r for r in self._buf if r["seq"] > after and (kind is None or r.get("kind") == kind)
            ]
        return items[:limit]
