"""JSON file exporter. OTLP is added beside it when an endpoint is configured."""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExportResult, SpanExporter


def json_safe(value: object) -> object:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [json_safe(item) for item in value]
    return str(value)


def _duration_ms(start: int, end: int) -> int | float:
    ms = (end - start) / 1_000_000
    if ms == int(ms):
        return int(ms)
    return round(ms, 3)


def span_to_dict(span: ReadableSpan) -> dict:
    parent = span.parent
    parent_id = None
    if parent is not None and parent.span_id:
        parent_id = format(parent.span_id, "016x")
    attributes = {key: json_safe(value) for key, value in sorted((span.attributes or {}).items())}
    events = []
    for event in span.events or ():
        events.append(
            {
                "name": event.name,
                "time_unix_nano": event.timestamp,
                "attributes": {key: json_safe(value) for key, value in sorted((event.attributes or {}).items())},
            }
        )
    status = span.status
    return {
        "trace_id": format(span.context.trace_id, "032x"),
        "span_id": format(span.context.span_id, "016x"),
        "parent_span_id": parent_id,
        "name": span.name,
        "kind": span.kind.name,
        "start_time_unix_nano": span.start_time,
        "end_time_unix_nano": span.end_time,
        "duration_ms": _duration_ms(span.start_time, span.end_time or span.start_time),
        "status": status.status_code.name,
        "status_message": status.description or "",
        "attributes": attributes,
        "events": events,
    }


class JsonFileExporter(SpanExporter):
    """Buffers ended spans and writes one JSON document on flush."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path
        self._spans: list[ReadableSpan] = []

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        self._spans.extend(spans)
        return SpanExportResult.SUCCESS

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        del timeout_millis
        self.write()
        return True

    def shutdown(self) -> None:
        self.write()

    def write(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(self.document(), indent=2, ensure_ascii=False) + "\n"
        self.path.write_text(text, encoding="utf-8")

    def document(self) -> dict:
        grouped: dict[str, list[dict]] = {}
        order: list[str] = []
        for span in self._spans:
            item = span_to_dict(span)
            trace_id = item["trace_id"]
            if trace_id not in grouped:
                grouped[trace_id] = []
                order.append(trace_id)
            grouped[trace_id].append(item)
        traces = []
        for trace_id in order:
            spans = sorted(
                grouped[trace_id],
                key=lambda item: (item["start_time_unix_nano"], -_as_number(item["duration_ms"])),
            )
            root = next(item for item in spans if item["parent_span_id"] is None)
            attrs = root["attributes"]
            children = [item for item in spans if item["parent_span_id"] is not None]
            traces.append(
                {
                    "trace_id": trace_id,
                    "task_id": attrs.get("houghtonlabs.task.id", ""),
                    "title": attrs.get("houghtonlabs.task.title", ""),
                    "outcome": attrs.get("houghtonlabs.agent.outcome", ""),
                    "error_type": attrs.get("error.type", ""),
                    "failing_span": attrs.get("houghtonlabs.agent.failing_span", ""),
                    "steps": len(children),
                    "duration_ms": root["duration_ms"],
                    "spans": spans,
                }
            )
        service_name = "agenttrace"
        service_version = ""
        if self._spans:
            resource = dict(self._spans[0].resource.attributes)
            service_name = str(resource.get("service.name") or service_name)
            service_version = str(resource.get("service.version") or "")
        return {
            "schema_version": 1,
            "service": {"name": service_name, "version": service_version},
            "traces": traces,
        }


def _as_number(value: object) -> float:
    return float(value)  # type: ignore[arg-type]


def otlp_traces_url(raw: str) -> str:
    text = raw.strip().rstrip("/")
    if text.endswith("/v1/traces"):
        return text
    return text + "/v1/traces"
