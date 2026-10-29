"""Run the dataset, roll up SLOs, write JSON, and serve the static site."""

from __future__ import annotations

import json
import os
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor

from agenttrace import __version__
from agenttrace.agent import AgentRunner
from agenttrace.dataset import load_dataset, repo_root
from agenttrace.exporter import JsonFileExporter, otlp_traces_url
from agenttrace.ids import StableIdGenerator
from agenttrace.providers import llm_settings_from_env, make_provider
from agenttrace.rollup import format_report, rollup

CONVENTIONS = "https://github.com/open-telemetry/semantic-conventions-genai"


def content_mode_from_env() -> str:
    mode = (os.environ.get("HL_TRACE_CONTENT") or "preview").strip().lower()
    if mode not in {"off", "preview", "full"}:
        raise ValueError("HL_TRACE_CONTENT must be off, preview, or full")
    return mode


def otlp_endpoint_from_env() -> str | None:
    raw = (
        os.environ.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT")
        or os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
        or ""
    ).strip()
    return raw or None


def build_otlp_exporter(endpoint: str):
    if ":4317" in endpoint and not endpoint.rstrip("/").endswith("/v1/traces"):
        print(
            "note: this exporter speaks OTLP/HTTP. Use port 4318 "
            "(collector HTTP), not 4317 (gRPC)."
        )
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

    return OTLPSpanExporter(endpoint=otlp_traces_url(endpoint))


def execute(dataset: dict, *, content_mode: str = "preview", otlp_endpoint: str | None = None, llm: dict | None = None):
    settings = llm or llm_settings_from_env(dataset)
    ids = StableIdGenerator()
    exporter = JsonFileExporter()
    resource = Resource.create(
        {
            "service.name": "agenttrace",
            "service.version": __version__,
        }
    )
    provider = TracerProvider(resource=resource, id_generator=ids)
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    if otlp_endpoint:
        provider.add_span_processor(SimpleSpanProcessor(build_otlp_exporter(otlp_endpoint)))
    tracer = provider.get_tracer("agenttrace", __version__)
    runner = AgentRunner(
        tracer=tracer,
        content_mode=content_mode,
        price_card=dataset["price_card"],
        agent=dataset["agent"],
    )
    try:
        for index, task in enumerate(dataset["tasks"]):
            ids.prepare(task["id"])
            task_provider = make_provider(settings, task)
            runner.run(task, task_provider, index)
    finally:
        provider.shutdown()
    document = enrich(exporter.document(), dataset)
    metrics = rollup(document, price_note=dataset.get("price_note", ""))
    return document, metrics


def enrich(document: dict, dataset: dict) -> dict:
    stories = {task["id"]: task.get("story", "") for task in dataset["tasks"]}
    titles = {task["id"]: task["title"] for task in dataset["tasks"]}
    for trace in document["traces"]:
        trace["title"] = titles.get(trace["task_id"], trace.get("title", ""))
        trace["story"] = stories.get(trace["task_id"], "")
    document["featured"] = dict(dataset["featured"])
    document["price_note"] = dataset.get("price_note", "")
    document["conventions"] = CONVENTIONS
    return document


def dump_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def write_pair(directory: Path, document: dict, metrics: dict) -> None:
    dump_json(directory / "traces.json", document)
    dump_json(directory / "metrics.json", metrics)


def run_command() -> int:
    dataset = load_dataset()
    endpoint = otlp_endpoint_from_env()
    document, metrics = execute(
        dataset,
        content_mode=content_mode_from_env(),
        otlp_endpoint=endpoint,
    )
    destination = repo_root() / "runs" / "latest"
    write_pair(destination, document, metrics)
    print(format_report(metrics))
    print(f"\nwrote {destination / 'traces.json'}")
    if endpoint:
        print(f"OTLP/HTTP -> {otlp_traces_url(endpoint)}")
    else:
        print("OTLP off (set OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:4318 to export)")
    return 0


def record_command() -> int:
    """Regenerate the committed demo JSON from the dataset."""
    dataset = load_dataset()
    # Recording is always the deterministic mock, so the site does not depend
    # on whoever last exported OTEL_EXPORTER_OTLP_ENDPOINT or a live model.
    document, metrics = execute(
        dataset,
        content_mode="preview",
        otlp_endpoint=None,
        llm={"llm_provider": "mock", "model": dataset["default_model"], "ollama_host": ""},
    )
    root = repo_root()
    for directory in (root / "demo" / "data", root / "site" / "data", root / "runs" / "latest"):
        write_pair(directory, document, metrics)
    print(format_report(metrics))
    print(f"\nrecorded {root / 'site' / 'data' / 'traces.json'}")
    return 0


def serve_command(port: int | None = None) -> int:
    root = repo_root()
    site = root / "site"
    run_dir = root / "runs" / "latest"
    bind_port = port or int(os.environ.get("HL_PORT") or "8765")

    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(site), **kwargs)

        def do_GET(self):  # noqa: N802
            parsed = urlparse(self.path)
            name = parsed.path.removeprefix("/data/")
            if parsed.path.startswith("/data/") and name in {"traces.json", "metrics.json"}:
                candidate = run_dir / name
                target = candidate if candidate.is_file() else site / "data" / name
                if not target.is_file():
                    self.send_error(404)
                    return
                body = target.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
                return
            super().do_GET()

        def log_message(self, fmt: str, *args) -> None:
            print(f"{self.address_string()} {fmt % args}")

    server = ThreadingHTTPServer(("127.0.0.1", bind_port), Handler)
    print(f"agenttrace site  http://127.0.0.1:{bind_port}")
    if (run_dir / "traces.json").is_file():
        print(f"traces from {run_dir}")
    else:
        print(f"traces from {site / 'data'}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        server.server_close()
    return 0
