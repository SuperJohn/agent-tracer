"""Stable trace and span ids so a re-run diffs cleanly."""

from __future__ import annotations

import hashlib

from opentelemetry.sdk.trace.id_generator import IdGenerator


def _int_id(key: str, nbytes: int) -> int:
    digest = hashlib.sha256(key.encode("utf-8")).digest()
    value = int.from_bytes(digest[:nbytes], "big")
    return value or 1


def stable_trace_id(task_id: str) -> int:
    return _int_id(f"trace:{task_id}", 16)


def stable_span_id(task_id: str, index: int) -> int:
    return _int_id(f"span:{task_id}:{index}", 8)


class StableIdGenerator(IdGenerator):
    """One trace id per task. Span ids follow the order start_span is called."""

    def __init__(self) -> None:
        self._task_id = "unset"
        self._n = 0

    def prepare(self, task_id: str) -> None:
        self._task_id = task_id
        self._n = 0

    def generate_trace_id(self) -> int:
        return stable_trace_id(self._task_id)

    def generate_span_id(self) -> int:
        span_id = stable_span_id(self._task_id, self._n)
        self._n += 1
        return span_id
