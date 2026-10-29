"""Clocks for span start and end times.

The mock provider replays a duration from the dataset and advances
ScriptedClock, so a laptop and CI draw the same waterfall. Ollama uses
SystemClock and the span covers the real HTTP call.
"""

from __future__ import annotations

import time
from dataclasses import dataclass


@dataclass
class ScriptedClock:
    """Monotonic nanosecond clock that only moves when the runner advances it."""

    _ns: int
    scripted: bool = True

    def now_ns(self) -> int:
        return self._ns

    def advance_ms(self, duration_ms: int) -> None:
        self._ns += int(duration_ms) * 1_000_000


@dataclass
class SystemClock:
    scripted: bool = False

    def now_ns(self) -> int:
        return time.time_ns()

    def advance_ms(self, duration_ms: int) -> None:
        return None
