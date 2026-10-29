import json

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from agenttrace.agent import AgentRunner
from agenttrace.dataset import load_dataset, repo_root
from agenttrace.exporter import JsonFileExporter, otlp_traces_url
from agenttrace.hashutil import sha256_hex
from agenttrace.ids import StableIdGenerator, stable_trace_id
from agenttrace.providers import Completion, ToolCall
from agenttrace.service import build_otlp_exporter, execute
from agenttrace.tools import prepare_call


class ListProvider:
    provider_name = "houghtonlabs.mock"
    uses_scripted_latency = True

    def __init__(self, items, model="mock-triage-v1"):
        self.items = list(items)
        self.model = model

    def complete(self, *, messages, tools, model, turn):
        del messages, tools, model, turn
        if not self.items:
            raise RuntimeError("no more completions")
        return self.items.pop(0)


def _completion(text, tool=None, arguments=None, duration=30, phase="reason", inp=20, out=8):
    calls = []
    finish = "stop"
    if tool:
        finish = "tool_calls"
        calls = [ToolCall(id="call_test", name=tool, arguments=arguments or {})]
    return Completion(
        text=text,
        tool_calls=calls,
        finish_reason=finish,
        input_tokens=inp,
        output_tokens=out,
        response_id="chatcmpl_test",
        model="mock-triage-v1",
        duration_ms=duration,
        phase=phase,
        response_model="mock-triage-v1",
    )


def _run_tiny(provider, task, content_mode="preview", max_turns=12):
    exporter = JsonFileExporter()
    ids = StableIdGenerator()
    ids.prepare(task["id"])
    provider_sdk = TracerProvider(id_generator=ids)
    provider_sdk.add_span_processor(SimpleSpanProcessor(exporter))
    memory = InMemorySpanExporter()
    provider_sdk.add_span_processor(SimpleSpanProcessor(memory))
    tracer = provider_sdk.get_tracer("agenttrace-test")
    agent = {
        "name": "warehouse-triage",
        "id": "agent_warehouse_triage",
        "version": "1.0.0",
        "description": "test",
        "system_prompt": "test",
    }
    runner = AgentRunner(tracer, content_mode, {"mock-triage-v1": {"input_usd_per_million": 0.15, "output_usd_per_million": 0.60}}, agent, max_turns=max_turns)
    runner.run(task, provider, 0)
    provider_sdk.shutdown()
    return exporter.document(), memory.get_finished_spans()


def _children(trace):
    return [span for span in trace["spans"] if span["parent_span_id"]]


def test_failing_run_is_14_steps_and_pinpoints_the_timeout():
    dataset = load_dataset(repo_root() / "demo" / "data" / "tasks.json")
    document, metrics = execute(
        dataset,
        content_mode="preview",
        otlp_endpoint=None,
        llm={"llm_provider": "mock", "model": dataset["default_model"], "ollama_host": ""},
    )
    trace = next(item for item in document["traces"] if item["task_id"] == "inc-1042")
    children = _children(trace)
    assert trace["steps"] == 14
    assert len(children) == 14
    assert [span["attributes"]["houghtonlabs.step"] for span in children] == list(range(1, 15))
    errors = [span for span in children if span["status"] == "ERROR"]
    assert len(errors) == 1
    error = errors[0]
    assert error["name"] == "execute_tool sensor.status"
    assert error["attributes"]["houghtonlabs.step"] == 12
    assert error["attributes"]["error.type"] == "timeout"
    assert error["attributes"]["gen_ai.operation.name"] == "execute_tool"
    assert error["attributes"]["gen_ai.tool.name"] == "sensor.status"
    assert error["attributes"]["gen_ai.tool.type"] == "function"
    assert error["attributes"]["gen_ai.tool.call.id"] == "call_inc_1042_05"
    assert error["duration_ms"] == 1500
    assert error["attributes"]["houghtonlabs.tool.args_sha256"] == sha256_hex({"sensor": "raw_orders_arrival"})
    assert "gen_ai.tool.call.arguments" not in error["attributes"]
    blocked = children[12]
    assert blocked["name"] == "state.update case.blocked"
    assert blocked["status"] == "OK"
    assert blocked["attributes"]["houghtonlabs.state.before_present"] is False
    assert blocked["attributes"]["houghtonlabs.state.before_sha256"] != blocked["attributes"]["houghtonlabs.state.after_sha256"]
    final = children[13]
    assert final["name"] == "chat mock-triage-v1"
    assert final["kind"] == "CLIENT"
    assert final["status"] == "OK"
    assert final["attributes"]["houghtonlabs.agent.phase"] == "finalize"
    assert final["attributes"]["gen_ai.operation.name"] == "chat"
    assert final["attributes"]["gen_ai.provider.name"] == "houghtonlabs.mock"
    assert final["attributes"]["gen_ai.response.finish_reasons"] == ["stop"]
    assert final["attributes"]["gen_ai.usage.input_tokens"] == 1104
    assert final["attributes"]["gen_ai.usage.output_tokens"] == 96
    assert final["attributes"]["houghtonlabs.cost.usd"] > 0
    root = trace["spans"][0]
    assert root["name"] == "invoke_agent warehouse-triage"
    assert root["kind"] == "INTERNAL"
    assert root["status"] == "ERROR"
    assert root["attributes"]["error.type"] == "timeout"
    assert root["attributes"]["gen_ai.operation.name"] == "invoke_agent"
    assert root["attributes"]["houghtonlabs.agent.outcome"] == "error"
    assert trace["trace_id"] == format(stable_trace_id("inc-1042"), "032x")
    planner = children[0]
    assert planner["attributes"]["houghtonlabs.agent.phase"] == "plan"
    assert planner["attributes"]["gen_ai.request.model"] == "mock-triage-v1"
    healthy = next(item for item in document["traces"] if item["task_id"] == "inc-1041")
    assert healthy["outcome"] == "ok"
    assert healthy["steps"] == 9
    assert all(span["status"] == "OK" for span in healthy["spans"])
    failing = next(row for row in metrics["tasks"] if row["id"] == "inc-1042")
    assert failing["failing_span"] == "execute_tool sensor.status"
    assert metrics["task_count"] == 16
    assert metrics["task_error_count"] == 3
    sensor = next(tool for tool in metrics["tools"] if tool["name"] == "sensor.status")
    assert sensor["errors"] == 2
    assert sensor["latency_ms"]["p95"] >= 1400


def test_content_off_omits_previews_and_full_includes_arguments():
    dataset = load_dataset(repo_root() / "demo" / "data" / "tasks.json")
    task = next(item for item in dataset["tasks"] if item["id"] == "inc-1041")
    from agenttrace.providers import ScriptedProvider

    off, _ = _run_tiny(ScriptedProvider(task["replay"], "mock-triage-v1"), task, content_mode="off")
    tool = next(span for span in off["traces"][0]["spans"] if span["name"] == "execute_tool catalog.lookup")
    assert "houghtonlabs.tool.args_preview" not in tool["attributes"]
    assert "gen_ai.tool.call.arguments" not in tool["attributes"]
    assert "houghtonlabs.tool.args_sha256" in tool["attributes"]
    chat = next(span for span in off["traces"][0]["spans"] if span["attributes"].get("gen_ai.operation.name") == "chat")
    assert "houghtonlabs.llm.output_preview" not in chat["attributes"]

    full, _ = _run_tiny(ScriptedProvider(task["replay"], "mock-triage-v1"), task, content_mode="full")
    tool = next(span for span in full["traces"][0]["spans"] if span["name"] == "execute_tool catalog.lookup")
    assert json.loads(tool["attributes"]["gen_ai.tool.call.arguments"]) == {"model": "orders_hourly"}
    assert "gen_ai.tool.call.result" in tool["attributes"]
    chat = next(span for span in full["traces"][0]["spans"] if span["attributes"].get("houghtonlabs.agent.phase") == "plan")
    assert "gen_ai.input.messages" in chat["attributes"]
    assert "gen_ai.system_instructions" in chat["attributes"]


def test_pluggable_provider_and_max_turns():
    task = {
        "id": "inc-test",
        "title": "tiny",
        "conversation_id": "conv_inc_test",
        "prompt": "Look up orders_daily.",
        "fixtures": {
            "catalog.lookup": {
                "latency_ms": 15,
                "result": {"model": "orders_daily", "owner": "revenue-data", "slo_freshness_minutes": 90},
            }
        },
    }
    provider = ListProvider(
        [
            _completion("Look it up.", "catalog.lookup", {"model": "orders_daily"}, phase="plan"),
            _completion("Found it.", phase="finalize"),
        ]
    )
    document, _ = _run_tiny(provider, task)
    names = [span["name"] for span in _children(document["traces"][0])]
    assert names == [
        "chat mock-triage-v1",
        "execute_tool catalog.lookup",
        "state.update case.model",
        "chat mock-triage-v1",
    ]
    assert document["traces"][0]["outcome"] == "ok"

    looping = ListProvider([_completion("again", "catalog.lookup", {"model": "orders_daily"}) for _ in range(4)])
    document, _ = _run_tiny(looping, task, max_turns=2)
    assert document["traces"][0]["error_type"] == "max_turns"


def test_unknown_tool_is_an_error_span():
    task = {
        "id": "inc-unknown",
        "title": "unknown",
        "conversation_id": "conv_inc_unknown",
        "prompt": "Do a thing.",
        "fixtures": {},
    }
    provider = ListProvider(
        [
            _completion("Try it.", "missing.tool", {"x": 1}),
            _completion("Stopped.", phase="finalize"),
        ]
    )
    document, _ = _run_tiny(provider, task)
    error = next(span for span in document["traces"][0]["spans"] if span["status"] == "ERROR" and span["parent_span_id"])
    assert error["attributes"]["error.type"] == "unknown_tool"
    assert document["traces"][0]["outcome"] == "error"


def test_prepare_call_checks_required_fields():
    task = {"fixtures": {"catalog.lookup": {"latency_ms": 5, "result": {"model": "orders_daily"}}}}
    with pytest.raises(Exception) as caught:
        prepare_call("catalog.lookup", {}, task)
    assert caught.value.error_type == "invalid_argument"


def test_otlp_url_and_exporter_construction(monkeypatch):
    assert otlp_traces_url("http://127.0.0.1:4318") == "http://127.0.0.1:4318/v1/traces"
    assert otlp_traces_url("http://127.0.0.1:4318/v1/traces/") == "http://127.0.0.1:4318/v1/traces"
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    exporter = build_otlp_exporter("http://127.0.0.1:4318")
    assert type(exporter).__name__ == "OTLPSpanExporter"
    assert exporter._endpoint == "http://127.0.0.1:4318/v1/traces"


def test_committed_site_matches_a_fresh_mock_run():
    dataset = load_dataset(repo_root() / "demo" / "data" / "tasks.json")
    document, metrics = execute(
        dataset,
        content_mode="preview",
        otlp_endpoint=None,
        llm={"llm_provider": "mock", "model": dataset["default_model"], "ollama_host": ""},
    )
    site = repo_root() / "site" / "data"
    committed_traces = json.loads((site / "traces.json").read_text(encoding="utf-8"))
    committed_metrics = json.loads((site / "metrics.json").read_text(encoding="utf-8"))
    demo_traces = json.loads((repo_root() / "demo" / "data" / "traces.json").read_text(encoding="utf-8"))
    assert json.loads(json.dumps(document)) == committed_traces
    assert json.loads(json.dumps(metrics)) == committed_metrics
    assert demo_traces == committed_traces
    html = (repo_root() / "site" / "index.html").read_text(encoding="utf-8")
    assert 'src="assets/app.js"' in html
    assert "https://" not in html


def test_no_secrets_in_synthetic_data():
    banned = ("AKIA", "sk-", "api_key", "BEGIN PRIVATE", "@gmail", "@yahoo", "sk_live")
    root = repo_root()
    files = list((root / "demo" / "data").glob("*.json")) + list((root / "site" / "data").glob("*.json"))
    blob = "\n".join(path.read_text(encoding="utf-8") for path in files)
    for token in banned:
        assert token not in blob
