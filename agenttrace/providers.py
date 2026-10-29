"""LLM providers. The default replays the dataset. Ollama is optional."""

from __future__ import annotations

import json
import os
import urllib.request
from dataclasses import dataclass, field
from urllib.parse import urlparse


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict


@dataclass
class Completion:
    text: str
    tool_calls: list[ToolCall]
    finish_reason: str
    input_tokens: int
    output_tokens: int
    response_id: str
    model: str
    duration_ms: int | None
    phase: str
    server_address: str | None = None
    server_port: int | None = None
    response_model: str | None = None


@dataclass
class ScriptedProvider:
    """Deterministic replay. Tests and CI never call a model."""

    replay: list[dict]
    model: str
    provider_name: str = "houghtonlabs.mock"
    uses_scripted_latency: bool = True

    def complete(self, *, messages: list[dict], tools: list[dict], model: str, turn: int) -> Completion:
        del messages, tools, model
        if turn >= len(self.replay):
            raise RuntimeError(f"scripted replay exhausted at turn {turn}")
        step = self.replay[turn]
        tool_calls = [
            ToolCall(id=item["id"], name=item["name"], arguments=dict(item["arguments"]))
            for item in (step.get("tool_calls") or [])
        ]
        finish = "tool_calls" if tool_calls else str(step.get("finish_reason") or "stop")
        return Completion(
            text=str(step.get("text") or ""),
            tool_calls=tool_calls,
            finish_reason=finish,
            input_tokens=int(step["input_tokens"]),
            output_tokens=int(step["output_tokens"]),
            response_id=str(step.get("response_id") or f"chatcmpl_turn_{turn}"),
            model=self.model,
            duration_ms=int(step["duration_ms"]),
            phase=str(step.get("phase") or "reason"),
            response_model=self.model,
        )


def ollama_messages(messages: list[dict]) -> list[dict]:
    converted = []
    for message in messages:
        role = message["role"]
        if role == "assistant" and message.get("tool_calls"):
            converted.append(
                {
                    "role": "assistant",
                    "content": message.get("content") or "",
                    "tool_calls": [
                        {
                            "type": "function",
                            "function": {
                                "name": call["name"],
                                "arguments": call["arguments"],
                            },
                        }
                        for call in message["tool_calls"]
                    ],
                }
            )
        elif role == "tool":
            converted.append({"role": "tool", "content": message.get("content") or ""})
        else:
            converted.append({"role": role, "content": message.get("content") or ""})
    return converted


def ollama_tools(tools: list[dict]) -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool["description"],
                "parameters": tool["parameters"],
            },
        }
        for tool in tools
    ]


@dataclass
class OllamaProvider:
    """Optional local provider. Selected with HL_LLM_PROVIDER=ollama."""

    host: str
    model: str
    timeout_s: float = 120
    provider_name: str = "ollama"
    uses_scripted_latency: bool = False
    _opener: object = field(default=None, repr=False)

    def complete(self, *, messages: list[dict], tools: list[dict], model: str, turn: int) -> Completion:
        del turn
        requested = model or self.model
        body = {
            "model": requested,
            "messages": ollama_messages(messages),
            "tools": ollama_tools(tools),
            "stream": False,
            "options": {"temperature": 0, "seed": 42},
        }
        request = urllib.request.Request(
            f"{self.host.rstrip('/')}/api/chat",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        opener = self._opener or urllib.request.urlopen
        with opener(request, timeout=self.timeout_s) as response:
            payload = json.load(response)
        message = payload.get("message") or {}
        tool_calls = []
        for call in message.get("tool_calls") or []:
            function = call.get("function") or call
            name = function.get("name") or ""
            arguments = function.get("arguments") or {}
            if isinstance(arguments, str):
                arguments = json.loads(arguments) if arguments.strip() else {}
            if not isinstance(arguments, dict):
                arguments = {}
            # Empty id means the provider did not supply one. The agent omits
            # gen_ai.tool.call.id in that case, which is what the spec asks for.
            tool_calls.append(ToolCall(id=str(call.get("id") or ""), name=name, arguments=arguments))
        text = message.get("content") or ""
        parsed = urlparse(self.host)
        port = parsed.port
        return Completion(
            text=text,
            tool_calls=tool_calls,
            finish_reason="tool_calls" if tool_calls else "stop",
            input_tokens=int(payload.get("prompt_eval_count") or 0),
            output_tokens=int(payload.get("eval_count") or 0),
            response_id=str(payload.get("id") or ""),
            model=requested,
            duration_ms=None,
            phase="reason",
            server_address=parsed.hostname,
            server_port=port,
            response_model=str(payload.get("model") or requested),
        )


def make_provider(settings: dict, task: dict):
    kind = settings["llm_provider"]
    if kind == "ollama":
        return OllamaProvider(host=settings["ollama_host"], model=settings["model"])
    if kind == "mock":
        return ScriptedProvider(replay=task["replay"], model=settings["model"])
    raise ValueError(f"unknown HL_LLM_PROVIDER {kind!r} (expected mock or ollama)")


def llm_settings_from_env(dataset: dict) -> dict:
    kind = (os.environ.get("HL_LLM_PROVIDER") or "mock").strip().lower()
    default_model = dataset["default_model"]
    if kind == "ollama":
        model = (os.environ.get("HL_OLLAMA_MODEL") or os.environ.get("HL_MODEL") or "").strip()
        if not model:
            raise ValueError("HL_LLM_PROVIDER=ollama requires HL_OLLAMA_MODEL")
        host = (os.environ.get("OLLAMA_HOST") or "http://127.0.0.1:11434").strip()
    else:
        model = default_model
        host = ""
        if kind != "mock":
            raise ValueError(f"unknown HL_LLM_PROVIDER {kind!r} (expected mock or ollama)")
    return {"llm_provider": kind, "model": model, "ollama_host": host}
