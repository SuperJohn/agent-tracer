"""Write demo/data/tasks.json. Synthetic incidents only. No secrets."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

AS_OF = datetime(2026, 10, 28, 6, 40, tzinfo=timezone.utc)
AS_OF_TEXT = "2026-10-28T06:40:00Z"
ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "demo" / "data" / "tasks.json"

SYSTEM = (
    "You are warehouse-triage, a DataOps agent. Investigate the synthetic warehouse "
    "incident with the provided tools. Prefer catalog, then freshness, then schema or "
    "lineage, then the arrival sensor or a row sample. Record an incident note when the "
    "checks finish. Never invent rows, owners, or sensor status. If a tool fails, say "
    "what is unconfirmed and do not claim the load is safe to retry."
)


def last_success(age_minutes: int) -> str:
    stamp = AS_OF - timedelta(minutes=age_minutes)
    return stamp.strftime("%Y-%m-%dT%H:%M:%SZ")


def sensor_name(model: str) -> str:
    return f"raw_{model.split('_')[0]}_arrival"


def chat(task_id: str, index: int, phase: str, duration: int, inp: int, out: int, text: str, tool=None, arguments=None):
    step = {
        "phase": phase,
        "duration_ms": duration,
        "input_tokens": inp,
        "output_tokens": out,
        "text": text,
        "response_id": f"chatcmpl_{task_id.replace('-', '_')}_{index:02d}",
    }
    if tool:
        step["tool_calls"] = [
            {
                "id": f"call_{task_id.replace('-', '_')}_{index:02d}",
                "name": tool,
                "arguments": arguments,
            }
        ]
    else:
        step["finish_reason"] = "stop"
    return step


def timing(salt: int, index: int) -> tuple[int, int, int]:
    duration = 160 + ((salt * 37 + index * 23) % 140)
    incoming = 540 + ((salt * 53 + index * 41) % 500)
    outgoing = 36 + ((salt * 19 + index * 11) % 70)
    return duration, incoming, outgoing


def catalog(model: str, owner: str, slo: int) -> dict:
    return {
        "model": model,
        "owner": owner,
        "slo_freshness_minutes": slo,
        "materialization": "table",
        "warehouse": "wh_prod",
    }


def freshness(model: str, age: int, slo: int) -> dict:
    return {
        "model": model,
        "as_of": AS_OF_TEXT,
        "age_minutes": age,
        "slo_minutes": slo,
        "status": "stale" if age > slo else "fresh",
        "last_success": last_success(age),
    }


def schema(model: str, breaking: bool) -> dict:
    stem = model.split("_")[0]
    if breaking:
        changes = [
            {
                "relation": model,
                "change": "rename_column",
                "from": "amount_cents",
                "to": "amount_minor",
            }
        ]
    else:
        changes = [
            {
                "relation": f"stg_{stem}",
                "change": "add_column",
                "column": "discount_bps",
                "nullable": True,
            }
        ]
    return {"model": model, "baseline": "prod", "breaking": breaking, "changes": changes}


def lineage(model: str) -> dict:
    stem = model.split("_")[0]
    sensor = sensor_name(model)
    return {
        "model": model,
        "depth": 2,
        "blocking": {"kind": "sensor", "name": sensor, "owner": "platform-ingest"},
        "nodes": [model, f"stg_{stem}", sensor],
    }


def sensor_ok(model: str, lag: int) -> dict:
    return {
        "sensor": sensor_name(model),
        "status": "healthy",
        "lag_seconds": lag,
        "last_heartbeat": "2026-10-28T06:39:10Z",
    }


def sample(model: str, nulls: int) -> dict:
    return {
        "model": model,
        "rows": 20,
        "max_event_time": "2026-10-28T06:35:00Z",
        "null_key_count": nulls,
    }


def note(task_id: str, action: str) -> dict:
    return {"incident_id": task_id, "recorded": True, "action": action}


def fixture(latency: int, result: dict | None = None, error_type: str | None = None, message: str | None = None):
    body = {"latency_ms": latency}
    if error_type:
        body["error_type"] = error_type
        body["message"] = message or error_type
    else:
        body["result"] = result
    return body


BLURB = {
    "catalog.lookup": "Look up {model} in the catalog before trusting the page.",
    "freshness.check": "Catalog is in. Check freshness for {model} at the incident time.",
    "schema.diff": "Diff {model} against the prod schema before blaming the load.",
    "lineage.upstream": "Walk upstream lineage for {model} and name the blocking node.",
    "sensor.status": "Check the arrival sensor for {model}.",
    "warehouse.sample": "Sample recent rows from {model}.",
    "incident.note": "Record the triage note on {incident}.",
}


def routine(spec: dict) -> dict:
    task_id = spec["id"]
    model = spec["model"]
    calls = spec["calls"]
    replay = []
    fixtures = {}
    for index, call in enumerate(calls):
        duration, incoming, outgoing = timing(spec["salt"], index)
        phase = "plan" if index == 0 else "reason"
        text = BLURB[call["tool"]].format(model=model, incident=task_id)
        arguments = dict(call["arguments"])
        replay.append(chat(task_id, index + 1, phase, duration, incoming, outgoing, text, call["tool"], arguments))
        fixtures[call["tool"]] = call["fixture"]
    duration, incoming, outgoing = timing(spec["salt"], len(calls))
    replay.append(chat(task_id, len(calls) + 1, "finalize", duration, incoming, outgoing, spec["finalize"]))
    return {
        "id": task_id,
        "title": spec["title"],
        "conversation_id": "conv_" + task_id.replace("-", "_"),
        "story": spec["story"],
        "prompt": spec.get("prompt")
        or (
            f"Incident {task_id}. The model {model} was flagged at {AS_OF_TEXT}. "
            "Determine whether it is inside its freshness SLO and whether the load is safe to retry."
        ),
        "replay": replay,
        "fixtures": fixtures,
    }


def hero_failing() -> dict:
    task_id = "inc-1042"
    model = "orders_daily"
    return {
        "id": task_id,
        "title": "orders_daily missed the freshness SLO",
        "conversation_id": "conv_inc_1042",
        "story": (
            "Step 12 of 14 is the only error span. execute_tool sensor.status timed out "
            "(error.type=timeout, 1500 ms) on the synthetic orchestrator. Step 13 latches "
            "case.blocked. Step 14 is a normal chat completion: the model still wrote a partial "
            "answer, so a stdout log looks fine. The root span is ERROR."
        ),
        "prompt": (
            "Incident inc-1042. The mart orders_daily missed its freshness SLO at "
            "2026-10-28T06:40:00Z. Find whether an upstream change or a sensor is blocking "
            "the load, and say if it is safe to retry."
        ),
        "replay": [
            chat(
                task_id, 1, "plan", 220, 612, 48,
                "Look up orders_daily in the catalog before checking freshness.",
                "catalog.lookup", {"model": model},
            ),
            chat(
                task_id, 2, "reason", 260, 748, 62,
                "The mart is owned by revenue-data with a 90 minute SLO. Check freshness at the incident time.",
                "freshness.check", {"model": model, "as_of": AS_OF_TEXT},
            ),
            chat(
                task_id, 3, "reason", 240, 860, 70,
                "Freshness is 372 minutes, past the SLO. Diff the schema against prod before blaming the load.",
                "schema.diff", {"model": model, "baseline": "prod"},
            ),
            chat(
                task_id, 4, "reason", 210, 904, 58,
                "The schema change is additive. Walk lineage two hops and name the blocking sensor.",
                "lineage.upstream", {"model": model, "depth": 2},
            ),
            chat(
                task_id, 5, "reason", 190, 980, 44,
                "Lineage stops on sensor raw_orders_arrival. Check that sensor.",
                "sensor.status", {"sensor": "raw_orders_arrival"},
            ),
            chat(
                task_id, 6, "finalize", 280, 1104, 96,
                "orders_daily is 372 minutes stale (SLO 90). The added discount_bps column is nullable, "
                "not blocking. sensor.status timed out, so raw_orders_arrival is unconfirmed. "
                "Do not retry. Page platform-ingest and cite inc-1042.",
            ),
        ],
        "fixtures": {
            "catalog.lookup": fixture(36, catalog(model, "revenue-data", 90)),
            "freshness.check": fixture(92, freshness(model, 372, 90)),
            "schema.diff": fixture(118, schema(model, breaking=False)),
            "lineage.upstream": fixture(104, lineage(model)),
            "sensor.status": fixture(
                1500,
                error_type="timeout",
                message="synthetic orchestrator timed out after 1500ms",
            ),
        },
    }


def hero_healthy() -> dict:
    task_id = "inc-1041"
    model = "orders_hourly"
    return {
        "id": task_id,
        "title": "orders_hourly is inside the freshness SLO",
        "conversation_id": "conv_inc_1041",
        "story": (
            "Nine steps, all OK. orders_hourly is inside the 90-minute freshness SLO "
            "and sensor raw_orders_arrival answered."
        ),
        "prompt": (
            "Incident inc-1041. Confirm whether orders_hourly is inside its freshness SLO "
            "at 2026-10-28T06:40:00Z and whether anyone should be paged."
        ),
        "replay": [
            chat(
                task_id, 1, "plan", 200, 540, 42,
                "Look up orders_hourly in the catalog.",
                "catalog.lookup", {"model": model},
            ),
            chat(
                task_id, 2, "reason", 180, 690, 40,
                "SLO is 90 minutes. Check freshness at the incident time.",
                "freshness.check", {"model": model, "as_of": AS_OF_TEXT},
            ),
            chat(
                task_id, 3, "reason", 170, 760, 36,
                "Age is 22 minutes. Confirm the arrival sensor is healthy.",
                "sensor.status", {"sensor": "raw_orders_arrival"},
            ),
            chat(
                task_id, 4, "finalize", 210, 820, 48,
                "orders_hourly is inside its 90 minute freshness SLO (age 22 minutes). "
                "Sensor raw_orders_arrival is healthy (lag 50 seconds). No page.",
            ),
        ],
        "fixtures": {
            "catalog.lookup": fixture(28, catalog(model, "revenue-data", 90)),
            "freshness.check": fixture(40, freshness(model, 22, 90)),
            "sensor.status": fixture(42, sensor_ok(model, 50)),
        },
    }


def call(tool: str, arguments: dict, latency: int, result=None, error_type=None, message=None):
    return {
        "tool": tool,
        "arguments": arguments,
        "fixture": fixture(latency, result, error_type, message),
    }


def routines() -> list[dict]:
    rows = [
        {
            "id": "inc-1101",
            "title": "customers_daily freshness check",
            "model": "customers_daily",
            "owner": "growth-data",
            "slo": 90,
            "age": 40,
            "salt": 11,
            "story": "Routine check. customers_daily is fresh. The run records a no-page note.",
            "finalize": "customers_daily is inside its 90 minute SLO (age 40 minutes). No page. Note recorded on inc-1101.",
            "calls": [
                ("catalog.lookup", 22, None),
                ("freshness.check", 55, None),
                ("warehouse.sample", 120, {"nulls": 0}),
                ("incident.note", 18, {"action": "no_page"}),
            ],
        },
        {
            "id": "inc-1102",
            "title": "payments_daily schema and sample",
            "model": "payments_daily",
            "owner": "billing-data",
            "slo": 60,
            "age": 18,
            "salt": 12,
            "story": "payments_daily is fresh. The schema diff is additive and the row sample succeeded.",
            "finalize": "payments_daily is inside its 60 minute SLO (age 18 minutes). Schema diff is not breaking. No page.",
            "calls": [
                ("catalog.lookup", 31, None),
                ("freshness.check", 70, None),
                ("schema.diff", 90, {"breaking": False}),
                ("warehouse.sample", 160, {"nulls": 0}),
            ],
        },
        {
            "id": "inc-1103",
            "title": "refunds_daily warehouse sample failed",
            "model": "refunds_daily",
            "owner": "billing-data",
            "slo": 60,
            "age": 200,
            "salt": 13,
            "story": "warehouse.sample returned HTTP 500. The root span is ERROR with error.type=500.",
            "finalize": "refunds_daily row sample returned HTTP 500 from the synthetic warehouse. Do not retry. Cite inc-1103.",
            "calls": [
                ("catalog.lookup", 44, None),
                ("freshness.check", 66, None),
                ("warehouse.sample", 640, {"error": "500"}),
            ],
        },
        {
            "id": "inc-1104",
            "title": "sessions_hourly sensor check",
            "model": "sessions_hourly",
            "owner": "growth-data",
            "slo": 30,
            "age": 12,
            "salt": 14,
            "story": "sessions_hourly is fresh and the arrival sensor answered.",
            "finalize": "sessions_hourly is inside its 30 minute SLO (age 12 minutes). Arrival sensor is healthy. No page.",
            "calls": [
                ("catalog.lookup", 26, None),
                ("freshness.check", 48, None),
                ("sensor.status", 55, {"lag": 20}),
            ],
        },
        {
            "id": "inc-1105",
            "title": "inventory_snapshot lineage",
            "model": "inventory_snapshot",
            "owner": "revenue-data",
            "slo": 180,
            "age": 50,
            "salt": 15,
            "story": "inventory_snapshot is fresh. Lineage names the arrival sensor, which answered.",
            "finalize": "inventory_snapshot is inside its 180 minute SLO (age 50 minutes). Sensor is healthy. No page.",
            "calls": [
                ("catalog.lookup", 33, None),
                ("freshness.check", 80, None),
                ("lineage.upstream", 88, None),
                ("sensor.status", 61, {"lag": 40}),
            ],
        },
        {
            "id": "inc-1106",
            "title": "clicks_raw sensor timed out",
            "model": "clicks_raw",
            "owner": "growth-data",
            "slo": 45,
            "age": 300,
            "salt": 16,
            "story": "A shorter failing run. sensor.status timed out after lineage named raw_clicks_arrival.",
            "finalize": "clicks_raw is stale and sensor.status timed out, so the block is unconfirmed. Do not retry. Cite inc-1106.",
            "calls": [
                ("catalog.lookup", 41, None),
                ("freshness.check", 74, None),
                ("lineage.upstream", 96, None),
                ("sensor.status", 1400, {"error": "timeout"}),
            ],
        },
        {
            "id": "inc-1107",
            "title": "shipments_daily schema diff",
            "model": "shipments_daily",
            "owner": "revenue-data",
            "slo": 120,
            "age": 30,
            "salt": 17,
            "story": "shipments_daily is fresh. The schema change is additive.",
            "finalize": "shipments_daily is inside its 120 minute SLO (age 30 minutes). Schema diff is not breaking. No page.",
            "calls": [
                ("catalog.lookup", 24, None),
                ("freshness.check", 58, None),
                ("schema.diff", 130, {"breaking": False}),
                ("warehouse.sample", 140, {"nulls": 1}),
            ],
        },
        {
            "id": "inc-1108",
            "title": "invoices_daily breaking schema, tools succeeded",
            "model": "invoices_daily",
            "owner": "billing-data",
            "slo": 90,
            "age": 15,
            "salt": 18,
            "story": (
                "The business answer is do-not-retry, but every tool succeeded. "
                "The trace stays OK. Trace status follows the checks, not the verdict."
            ),
            "finalize": "invoices_daily schema diff is breaking (amount_cents renamed). Do not retry the load. Page billing-data. Cite inc-1108.",
            "calls": [
                ("catalog.lookup", 38, None),
                ("freshness.check", 63, None),
                ("schema.diff", 150, {"breaking": True}),
                ("incident.note", 22, {"action": "page"}),
            ],
        },
        {
            "id": "inc-1109",
            "title": "accounts_daily row sample",
            "model": "accounts_daily",
            "owner": "billing-data",
            "slo": 90,
            "age": 44,
            "salt": 19,
            "story": "accounts_daily is fresh. The sample has 3 null keys and a no-page note.",
            "finalize": "accounts_daily is inside its 90 minute SLO (age 44 minutes). Sample returned 3 null keys. No page.",
            "calls": [
                ("catalog.lookup", 29, None),
                ("freshness.check", 51, None),
                ("warehouse.sample", 180, {"nulls": 3}),
                ("incident.note", 16, {"action": "no_page"}),
            ],
        },
        {
            "id": "inc-1110",
            "title": "subscriptions_daily slow freshness read",
            "model": "subscriptions_daily",
            "owner": "billing-data",
            "slo": 90,
            "age": 25,
            "salt": 20,
            "story": "The model is fresh. freshness.check took 460 ms, which pulls the tool's p95 up.",
            "finalize": "subscriptions_daily is inside its 90 minute SLO (age 25 minutes). Sensor is healthy. No page.",
            "calls": [
                ("catalog.lookup", 52, None),
                ("freshness.check", 460, None),
                ("sensor.status", 48, {"lag": 15}),
            ],
        },
        {
            "id": "inc-1111",
            "title": "tickets_daily schema diff",
            "model": "tickets_daily",
            "owner": "support-data",
            "slo": 60,
            "age": 20,
            "salt": 21,
            "story": "tickets_daily is fresh and the schema diff is additive.",
            "finalize": "tickets_daily is inside its 60 minute SLO (age 20 minutes). Schema diff is not breaking. No page.",
            "calls": [
                ("catalog.lookup", 35, None),
                ("freshness.check", 77, None),
                ("schema.diff", 102, {"breaking": False}),
            ],
        },
        {
            "id": "inc-1112",
            "title": "events_hourly lineage",
            "model": "events_hourly",
            "owner": "growth-data",
            "slo": 30,
            "age": 9,
            "salt": 22,
            "story": "events_hourly is fresh. Lineage names the arrival sensor.",
            "finalize": "events_hourly is inside its 30 minute SLO (age 9 minutes). No page.",
            "calls": [
                ("catalog.lookup", 27, None),
                ("freshness.check", 69, None),
                ("lineage.upstream", 124, None),
            ],
        },
        {
            "id": "inc-1113",
            "title": "pages_daily sample",
            "model": "pages_daily",
            "owner": "growth-data",
            "slo": 120,
            "age": 80,
            "salt": 23,
            "story": "pages_daily is fresh. Schema diff is additive and the sample succeeded.",
            "finalize": "pages_daily is inside its 120 minute SLO (age 80 minutes). Schema diff is not breaking. No page.",
            "calls": [
                ("catalog.lookup", 48, None),
                ("freshness.check", 84, None),
                ("schema.diff", 110, {"breaking": False}),
                ("warehouse.sample", 210, {"nulls": 0}),
            ],
        },
        {
            "id": "inc-1114",
            "title": "devices_daily sensor and note",
            "model": "devices_daily",
            "owner": "revenue-data",
            "slo": 180,
            "age": 100,
            "salt": 24,
            "story": "devices_daily is fresh. The sensor answered and a no-page note was recorded.",
            "finalize": "devices_daily is inside its 180 minute SLO (age 100 minutes). Sensor is healthy. No page.",
            "calls": [
                ("catalog.lookup", 61, None),
                ("freshness.check", 45, None),
                ("sensor.status", 36, {"lag": 80}),
                ("incident.note", 20, {"action": "no_page"}),
            ],
        },
    ]
    built = []
    for row in rows:
        model = row["model"]
        calls = []
        for tool, latency, extra in row["calls"]:
            extra = extra or {}
            if tool == "catalog.lookup":
                calls.append(call(tool, {"model": model}, latency, catalog(model, row["owner"], row["slo"])))
            elif tool == "freshness.check":
                calls.append(
                    call(
                        tool,
                        {"model": model, "as_of": AS_OF_TEXT},
                        latency,
                        freshness(model, row["age"], row["slo"]),
                    )
                )
            elif tool == "schema.diff":
                calls.append(
                    call(
                        tool,
                        {"model": model, "baseline": "prod"},
                        latency,
                        schema(model, breaking=bool(extra.get("breaking"))),
                    )
                )
            elif tool == "lineage.upstream":
                calls.append(call(tool, {"model": model, "depth": 2}, latency, lineage(model)))
            elif tool == "sensor.status":
                if extra.get("error") == "timeout":
                    calls.append(
                        call(
                            tool,
                            {"sensor": sensor_name(model)},
                            latency,
                            error_type="timeout",
                            message="synthetic orchestrator timed out after 1500ms",
                        )
                    )
                else:
                    calls.append(call(tool, {"sensor": sensor_name(model)}, latency, sensor_ok(model, extra["lag"])))
            elif tool == "warehouse.sample":
                if extra.get("error") == "500":
                    calls.append(
                        call(
                            tool,
                            {"model": model, "limit": 20},
                            latency,
                            error_type="500",
                            message=f"synthetic warehouse returned HTTP 500 for statement sample_{model}",
                        )
                    )
                else:
                    calls.append(call(tool, {"model": model, "limit": 20}, latency, sample(model, extra.get("nulls", 0))))
            elif tool == "incident.note":
                action = extra["action"]
                team = "platform-ingest" if action == "page" else row["owner"]
                calls.append(
                    call(
                        tool,
                        {"incident_id": row["id"], "action": action, "team": team},
                        latency,
                        note(row["id"], action),
                    )
                )
            else:
                raise AssertionError(tool)
        built.append(
            routine(
                {
                    "id": row["id"],
                    "title": row["title"],
                    "model": model,
                    "salt": row["salt"],
                    "story": row["story"],
                    "finalize": row["finalize"],
                    "calls": calls,
                }
            )
        )
    return built


def build() -> dict:
    return {
        "schema_version": 1,
        "default_model": "mock-triage-v1",
        "price_note": (
            "Illustrative small-model card for mock-triage-v1 "
            "($0.15 / 1M input tokens, $0.60 / 1M output tokens). Not a vendor bill. "
            "Ollama is local, so its dollar cost stays zero while token counts are still recorded."
        ),
        "price_card": {
            "mock-triage-v1": {
                "input_usd_per_million": 0.15,
                "output_usd_per_million": 0.60,
            }
        },
        "agent": {
            "name": "warehouse-triage",
            "id": "agent_warehouse_triage",
            "version": "1.0.0",
            "description": "Triages synthetic warehouse freshness incidents and decides whether to page.",
            "system_prompt": SYSTEM,
        },
        "featured": {"failing": "inc-1042", "healthy": "inc-1041"},
        "tasks": [hero_failing(), hero_healthy(), *routines()],
    }


def main() -> None:
    document = build()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {OUT} ({len(document['tasks'])} tasks)")


if __name__ == "__main__":
    main()
