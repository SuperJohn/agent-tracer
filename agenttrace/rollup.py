"""SLO-style rollups from a recorded trace document."""

from __future__ import annotations

from agenttrace.hashutil import mean, percentile


def _round(value: float | None, digits: int = 4) -> float | None:
    if value is None:
        return None
    return round(value, digits)


def _summary(values: list[float], digits: int = 4) -> dict:
    return {
        "p50": _round(percentile(values, 50), digits),
        "p95": _round(percentile(values, 95), digits),
        "mean": _round(mean(values), digits),
    }


def rollup(document: dict, price_note: str = "") -> dict:
    tool_latency: dict[str, list[float]] = {}
    tool_errors: dict[str, int] = {}
    task_rows = []
    tokens = []
    costs = []
    task_errors = 0
    tool_calls = 0
    tool_error_total = 0

    for trace in document["traces"]:
        root = next(span for span in trace["spans"] if span["parent_span_id"] is None)
        root_attrs = root["attributes"]
        input_tokens = int(root_attrs.get("gen_ai.usage.input_tokens") or 0)
        output_tokens = int(root_attrs.get("gen_ai.usage.output_tokens") or 0)
        cost = float(root_attrs.get("houghtonlabs.cost.usd") or 0)
        outcome = trace.get("outcome") or root_attrs.get("houghtonlabs.agent.outcome") or ""
        if outcome == "error":
            task_errors += 1
        tokens.append(input_tokens + output_tokens)
        costs.append(cost)
        for span in trace["spans"]:
            if span["attributes"].get("gen_ai.operation.name") != "execute_tool":
                continue
            name = span["attributes"].get("gen_ai.tool.name") or span["name"]
            tool_latency.setdefault(name, [])
            tool_errors.setdefault(name, 0)
            tool_latency[name].append(float(span["duration_ms"]))
            tool_calls += 1
            if span["status"] == "ERROR":
                tool_errors[name] += 1
                tool_error_total += 1
        task_rows.append(
            {
                "id": trace["task_id"],
                "title": trace.get("title") or "",
                "outcome": outcome,
                "error_type": trace.get("error_type") or "",
                "failing_span": trace.get("failing_span") or "",
                "steps": trace.get("steps") or 0,
                "duration_ms": trace.get("duration_ms") or root["duration_ms"],
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "tokens": input_tokens + output_tokens,
                "cost_usd": round(cost, 10),
            }
        )

    tools = []
    for name in sorted(tool_latency):
        calls = len(tool_latency[name])
        errors = tool_errors.get(name, 0)
        tools.append(
            {
                "name": name,
                "calls": calls,
                "errors": errors,
                "error_rate": round(errors / calls, 6) if calls else 0,
                "latency_ms": _summary(tool_latency[name]),
            }
        )

    task_count = len(task_rows)
    return {
        "schema_version": 1,
        "percentile_method": "linear",
        "price_note": price_note,
        "task_count": task_count,
        "task_error_count": task_errors,
        "task_error_rate": round(task_errors / task_count, 6) if task_count else 0,
        "tool_call_count": tool_calls,
        "tool_error_count": tool_error_total,
        "tool_error_rate": round(tool_error_total / tool_calls, 6) if tool_calls else 0,
        "tokens_per_task": _summary([float(item) for item in tokens], digits=2),
        "cost_usd_per_task": _summary(costs, digits=8),
        "tools": tools,
        "tasks": task_rows,
    }


def format_report(metrics: dict) -> str:
    lines = [
        f"tasks {metrics['task_count']}  "
        f"task error rate {metrics['task_error_rate']:.1%} "
        f"({metrics['task_error_count']}/{metrics['task_count']})",
        f"tokens/task  p50 {metrics['tokens_per_task']['p50']}  "
        f"p95 {metrics['tokens_per_task']['p95']}  "
        f"mean {metrics['tokens_per_task']['mean']}",
        f"cost USD/task (illustrative)  p50 {metrics['cost_usd_per_task']['p50']}  "
        f"p95 {metrics['cost_usd_per_task']['p95']}",
        "",
        f"{'tool':<22} {'calls':>5} {'errors':>6} {'error':>8} {'p50 ms':>8} {'p95 ms':>8}",
    ]
    for tool in metrics["tools"]:
        latency = tool["latency_ms"]
        lines.append(
            f"{tool['name']:<22} {tool['calls']:>5} {tool['errors']:>6} "
            f"{tool['error_rate']:>8.1%} {latency['p50']:>8} {latency['p95']:>8}"
        )
    lines.append("")
    for task in metrics["tasks"]:
        if task["outcome"] != "error":
            continue
        lines.append(
            f"{task['id']} ERROR  {task['steps']} steps  "
            f"failing span: {task['failing_span']} ({task['error_type']})"
        )
    return "\n".join(lines)
