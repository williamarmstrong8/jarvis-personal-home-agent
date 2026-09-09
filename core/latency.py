"""Lightweight per-turn latency tracing.

Each completed voice turn is appended to ``data/logs/latency.jsonl``.  The
tracer deliberately uses ``perf_counter`` for durations and wall-clock UTC
only for correlation, so clock adjustments cannot corrupt measurements.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from datetime import datetime, timezone

from .paths import LOGS


_write_lock = threading.Lock()


class LatencyTrace:
    """Collect monotonic timestamps for one wake-to-idle interaction."""

    def __init__(self, source: str = "wake"):
        self.turn_id = uuid.uuid4().hex[:12]
        self.source = source
        self.started_at = time.perf_counter()
        self.started_utc = datetime.now(timezone.utc).isoformat()
        self.marks: dict[str, float] = {}
        self.fields: dict[str, object] = {}
        self._finished = False
        self.mark("trace_started")

    def mark(self, name: str) -> None:
        self.marks[name] = round((time.perf_counter() - self.started_at) * 1000, 1)

    def set(self, name: str, value: object) -> None:
        self.fields[name] = value

    def finish(self, outcome: str = "ok") -> None:
        if self._finished:
            return
        self._finished = True
        self.mark("turn_complete")
        row = {
            "turn_id": self.turn_id,
            "started_utc": self.started_utc,
            "source": self.source,
            "outcome": outcome,
            "marks_ms": self.marks,
            **self.fields,
        }
        try:
            LOGS.mkdir(parents=True, exist_ok=True)
            with _write_lock:
                with (LOGS / "latency.jsonl").open("a", encoding="utf-8") as f:
                    f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
        except OSError:
            # Telemetry must never interrupt the voice path.
            pass


def mark(trace: LatencyTrace | None, name: str) -> None:
    if trace is not None:
        trace.mark(name)
