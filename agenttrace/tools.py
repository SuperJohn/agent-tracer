"""In-process tools backed by synthetic fixtures on each task."""

from __future__ import annotations

import json
from dataclasses import dataclass, field


class ToolFailure(Exception):
    def __init__(self, error_type: str, message: str, latency_ms: int = 1) -> None:
        super().__init__(message)
        self.error_type = error_type
        self.message = message
        self.latency_ms = latency_ms


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    required: tuple[str, ...]
    properties: dict
    state_key: str | None = None
    state_fields: tuple[str, ...] = ()

    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": self.properties,
            "required": list(self.required),
            "additionalProperties": False,
        }

    def definition(self) -> dict:
        return {
            "type": "function",
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters(),
        }


def _str_prop(description: str) -> dict:
    return {"type": "string", "description": description}


def _int_prop(description: str) -> dict:
    return {"type": "integer", "description": description}


TOOLS: dict[str, ToolSpec] = {
    spec.name: spec
    for spec in (
        ToolSpec(
            name="catalog.lookup",
            description="Look up a warehouse model in the synthetic catalog.",
            required=("model",),
            properties={"model": _str_prop("Model name, for example orders_daily.")},
            state_key="case.model",
            state_fields=("model", "owner", "slo_freshness_minutes"),
        ),
        ToolSpec(
            name="freshness.check",
            description="Read the model's freshness against its SLO at an as-of time.",
            required=("model", "as_of"),
            properties={
                "model": _str_prop("Model name."),
                "as_of": _str_prop("ISO-8601 timestamp the incident was opened at."),
            },
            state_key="case.freshness",
            state_fields=("model", "status", "age_minutes", "slo_minutes"),
        ),
        ToolSpec(
            name="schema.diff",
            description="Diff a model against a named schema baseline.",
            required=("model", "baseline"),
            properties={
                "model": _str_prop("Model name."),
                "baseline": _str_prop("Baseline name, usually prod."),
            },
        ),
        ToolSpec(
            name="lineage.upstream",
            description="Walk upstream lineage and name the blocking node.",
            required=("model",),
            properties={
                "model": _str_prop("Model name."),
                "depth": _int_prop("How many hops to walk. Defaults inside the fixture."),
            },
        ),
        ToolSpec(
            name="sensor.status",
            description="Read an arrival sensor from the synthetic orchestrator.",
            required=("sensor",),
            properties={"sensor": _str_prop("Sensor name, for example raw_orders_arrival.")},
        ),
        ToolSpec(
            name="warehouse.sample",
            description="Sample recent rows from a model in the synthetic warehouse.",
            required=("model",),
            properties={
                "model": _str_prop("Model name."),
                "limit": _int_prop("Maximum rows to sample."),
            },
            state_key="case.sample",
            state_fields=("model", "rows", "null_key_count"),
        ),
        ToolSpec(
            name="incident.note",
            description="Record the triage decision on the synthetic incident.",
            required=("incident_id", "action"),
            properties={
                "incident_id": _str_prop("Incident id, for example inc-1042."),
                "action": _str_prop("no_page or page."),
                "team": _str_prop("Team the note is addressed to."),
            },
            state_key="case.note",
            state_fields=("incident_id", "action"),
        ),
    )
}


@dataclass
class PreparedCall:
    spec: ToolSpec
    arguments: dict
    result: dict
    latency_ms: int
    error: ToolFailure | None = field(default=None)


def coerce_arguments(arguments: object) -> dict:
    if arguments is None:
        return {}
    if isinstance(arguments, str):
        text = arguments.strip()
        if not text:
            return {}
        try:
            arguments = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ToolFailure("invalid_argument", "arguments are not valid JSON") from exc
    if not isinstance(arguments, dict):
        raise ToolFailure("invalid_argument", "arguments must be an object")
    return arguments


def prepare_call(name: str, arguments: object, task: dict) -> PreparedCall:
    """Resolve a tool call against the task fixture, or raise ToolFailure."""
    try:
        coerced = coerce_arguments(arguments)
    except ToolFailure:
        raise
    spec = TOOLS.get(name)
    if spec is None:
        raise ToolFailure("unknown_tool", f"unknown tool {name}")
    fixture = (task.get("fixtures") or {}).get(name) or {}
    latency = int(fixture.get("latency_ms") or 1)
    missing = [key for key in spec.required if key not in coerced]
    if missing:
        raise ToolFailure("invalid_argument", "missing " + ",".join(missing), latency)
    if fixture.get("error_type"):
        message = fixture.get("message") or fixture["error_type"]
        raise ToolFailure(str(fixture["error_type"]), str(message), latency)
    if "result" not in fixture:
        raise ToolFailure("not_found", f"no synthetic fixture for {name}", latency)
    return PreparedCall(spec=spec, arguments=coerced, result=fixture["result"], latency_ms=latency)


def tool_definitions() -> list[dict]:
    return [spec.definition() for spec in TOOLS.values()]


def project_state(result: dict, fields: tuple[str, ...]) -> dict:
    return {key: result[key] for key in fields if key in result}
