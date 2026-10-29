"""Planner, tool calls, later LLM hops, and scratchpad writes, as spans."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.trace import SpanKind, Status, StatusCode

from agenttrace.clock import ScriptedClock, SystemClock
from agenttrace.hashutil import estimate_cost_usd, sha256_hex
from agenttrace.providers import Completion, ToolCall
from agenttrace.tools import TOOLS, PreparedCall, ToolFailure, prepare_call, project_state, tool_definitions

STATE_MS = 8
PREVIEW_LIMIT = 320
BASE_NS = int(datetime(2026, 10, 28, 6, 40, tzinfo=timezone.utc).timestamp() * 1_000_000_000)
TASK_GAP_NS = 120 * 1_000_000_000


def _preview(value: object, limit: int = PREVIEW_LIMIT) -> str:
    if isinstance(value, str):
        text = value
    else:
        text = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _status(code: StatusCode, message: str = "") -> Status:
    if message:
        return Status(code, message)
    return Status(code)


class AgentRunner:
    def __init__(self, tracer, content_mode: str, price_card: dict, agent: dict, max_turns: int = 12) -> None:
        self.tracer = tracer
        self.content_mode = content_mode
        self.price_card = price_card
        self.agent = agent
        self.max_turns = max_turns

    def run(self, task: dict, provider, task_index: int) -> None:
        scripted = bool(provider.uses_scripted_latency)
        clock = ScriptedClock(BASE_NS + task_index * TASK_GAP_NS) if scripted else SystemClock()
        memory: dict[str, dict] = {}
        messages = [
            {"role": "system", "content": self.agent["system_prompt"]},
            {"role": "user", "content": task["prompt"]},
        ]
        totals = {"input": 0, "output": 0, "cost": 0.0}
        failure = {"type": "", "span": "", "message": ""}
        blocked = False
        step = {"n": 0}
        root_attrs = {
            "gen_ai.operation.name": "invoke_agent",
            "gen_ai.agent.name": self.agent["name"],
            "gen_ai.agent.id": self.agent["id"],
            "gen_ai.agent.description": self.agent["description"],
            "gen_ai.agent.version": self.agent["version"],
            "gen_ai.conversation.id": task["conversation_id"],
            "gen_ai.output.type": "text",
            "gen_ai.request.model": provider.model,
            "gen_ai.request.temperature": 0.0,
            "gen_ai.request.max_tokens": 512,
            "gen_ai.request.seed": 42,
            "houghtonlabs.task.id": task["id"],
            "houghtonlabs.task.title": task["title"],
        }
        root = self._start(
            f"invoke_agent {self.agent['name']}",
            SpanKind.INTERNAL,
            root_attrs,
            clock,
            scripted,
            new_trace=True,
        )
        root_token = otel_context.attach(trace.set_span_in_context(root))
        try:
            try:
                for turn in range(self.max_turns):
                    completion = self._chat_span(
                        task, provider, messages, clock, scripted, step, totals, turn
                    )
                    messages.append(_assistant_message(completion))
                    if not completion.tool_calls:
                        break
                    for call in completion.tool_calls:
                        failed, prepared = self._tool_span(task, call, clock, scripted, step)
                        if prepared is not None:
                            messages.append(_tool_message(call, prepared.result))
                            if prepared.spec.state_key:
                                self._state_span(
                                    task,
                                    prepared.spec.state_key,
                                    project_state(prepared.result, prepared.spec.state_fields),
                                    memory,
                                    clock,
                                    scripted,
                                    step,
                                )
                        else:
                            messages.append(
                                _tool_message(
                                    call,
                                    {"error_type": failed.error_type, "message": failed.message},
                                )
                            )
                            if not failure["type"]:
                                failure["type"] = failed.error_type
                                failure["span"] = f"execute_tool {call.name}"
                                failure["message"] = failed.message
                            if not blocked:
                                self._state_span(
                                    task,
                                    "case.blocked",
                                    {
                                        "status": "blocked",
                                        "tool": call.name,
                                        "error_type": failed.error_type,
                                    },
                                    memory,
                                    clock,
                                    scripted,
                                    step,
                                )
                                blocked = True
                else:
                    if not failure["type"]:
                        failure["type"] = "max_turns"
                        failure["span"] = f"invoke_agent {self.agent['name']}"
                        failure["message"] = f"stopped after {self.max_turns} LLM turns"
                self._finish_root(root, failure, totals)
            except Exception as exc:
                if not failure["type"]:
                    failure["type"] = type(exc).__name__
                    failure["span"] = f"invoke_agent {self.agent['name']}"
                    failure["message"] = str(exc)
                self._finish_root(root, failure, totals)
                raise
        finally:
            self._end(root, root_token, clock, scripted, duration_ms=None)

    def _finish_root(self, root, failure: dict, totals: dict) -> None:
        if failure["type"]:
            root.set_status(_status(StatusCode.ERROR, f"{failure['span']} {failure['type']}"))
            root.set_attribute("error.type", failure["type"])
            root.set_attribute("houghtonlabs.agent.outcome", "error")
            root.set_attribute("houghtonlabs.agent.failing_span", failure["span"])
        else:
            root.set_status(_status(StatusCode.OK))
            root.set_attribute("houghtonlabs.agent.outcome", "ok")
            root.set_attribute("houghtonlabs.agent.failing_span", "")
        root.set_attribute("gen_ai.usage.input_tokens", totals["input"])
        root.set_attribute("gen_ai.usage.output_tokens", totals["output"])
        root.set_attribute("houghtonlabs.cost.usd", round(totals["cost"], 10))

    def _chat_span(self, task, provider, messages, clock, scripted, step, totals, turn: int) -> Completion:
        step["n"] += 1
        attributes = {
            "gen_ai.operation.name": "chat",
            "gen_ai.provider.name": provider.provider_name,
            "gen_ai.request.model": provider.model,
            "gen_ai.request.temperature": 0.0,
            "gen_ai.request.max_tokens": 512,
            "gen_ai.request.seed": 42,
            "gen_ai.output.type": "text",
            "gen_ai.conversation.id": task["conversation_id"],
            "gen_ai.agent.name": self.agent["name"],
            "houghtonlabs.step": step["n"],
        }
        span = self._start(f"chat {provider.model}", SpanKind.CLIENT, attributes, clock, scripted, new_trace=False)
        token = otel_context.attach(trace.set_span_in_context(span))
        completion: Completion | None = None
        try:
            completion = provider.complete(
                messages=messages,
                tools=tool_definitions(),
                model=provider.model,
                turn=turn,
            )
            phase = completion.phase
            if not provider.uses_scripted_latency:
                if turn == 0 and completion.tool_calls:
                    phase = "plan"
                elif not completion.tool_calls:
                    phase = "finalize"
                else:
                    phase = "reason"
            cost = estimate_cost_usd(
                completion.model,
                completion.input_tokens,
                completion.output_tokens,
                self.price_card,
            )
            totals["input"] += completion.input_tokens
            totals["output"] += completion.output_tokens
            totals["cost"] = round(totals["cost"] + cost, 10)
            span.set_attribute("gen_ai.response.model", completion.response_model or completion.model)
            span.set_attribute("gen_ai.response.finish_reasons", [completion.finish_reason])
            span.set_attribute("gen_ai.usage.input_tokens", completion.input_tokens)
            span.set_attribute("gen_ai.usage.output_tokens", completion.output_tokens)
            span.set_attribute("houghtonlabs.agent.phase", phase)
            span.set_attribute("houghtonlabs.cost.usd", cost)
            if completion.response_id:
                span.set_attribute("gen_ai.response.id", completion.response_id)
            if completion.server_address:
                span.set_attribute("server.address", completion.server_address)
            if completion.server_port:
                span.set_attribute("server.port", completion.server_port)
            if self.content_mode in {"preview", "full"} and completion.text:
                span.set_attribute("houghtonlabs.llm.output_preview", _preview(completion.text))
            if self.content_mode == "full":
                span.set_attribute(
                    "gen_ai.input.messages",
                    json.dumps(_genai_messages(messages), ensure_ascii=False),
                )
                span.set_attribute(
                    "gen_ai.output.messages",
                    json.dumps(
                        [
                            {
                                "role": "assistant",
                                "parts": _output_parts(completion),
                                "finish_reason": completion.finish_reason,
                            }
                        ],
                        ensure_ascii=False,
                    ),
                )
                span.set_attribute(
                    "gen_ai.system_instructions",
                    json.dumps(
                        [{"type": "text", "content": self.agent["system_prompt"]}],
                        ensure_ascii=False,
                    ),
                )
            span.set_status(_status(StatusCode.OK))
            if scripted and completion.duration_ms is not None:
                clock.advance_ms(completion.duration_ms)
            return completion
        except Exception as exc:
            span.set_status(_status(StatusCode.ERROR, str(exc)))
            span.set_attribute("error.type", type(exc).__name__)
            raise
        finally:
            self._end(span, token, clock, scripted, duration_ms=None)

    def _tool_span(self, task, call: ToolCall, clock, scripted, step):
        try:
            prepared = prepare_call(call.name, call.arguments, task)
        except ToolFailure as failure:
            self._emit_tool(task, call, None, failure, clock, scripted, step)
            return failure, None
        self._emit_tool(task, call, prepared, None, clock, scripted, step)
        return None, prepared

    def _emit_tool(self, task, call, prepared: PreparedCall | None, failure: ToolFailure | None, clock, scripted, step) -> None:
        spec = TOOLS.get(call.name)
        attributes = {
            "gen_ai.operation.name": "execute_tool",
            "gen_ai.tool.name": call.name,
            "gen_ai.tool.type": "function",
            "gen_ai.conversation.id": task["conversation_id"],
            "gen_ai.agent.name": self.agent["name"],
            "houghtonlabs.tool.args_sha256": sha256_hex(call.arguments),
            "houghtonlabs.step": step["n"] + 1,
        }
        if call.id:
            attributes["gen_ai.tool.call.id"] = call.id
        if spec is not None:
            attributes["gen_ai.tool.description"] = spec.description
        if self.content_mode in {"preview", "full"}:
            attributes["houghtonlabs.tool.args_preview"] = _preview(call.arguments)
        if self.content_mode == "full":
            attributes["gen_ai.tool.call.arguments"] = json.dumps(call.arguments, sort_keys=True)
        latency = failure.latency_ms if failure else prepared.latency_ms
        if failure:
            attributes["houghtonlabs.tool.error_message"] = failure.message
            self._emit(
                f"execute_tool {call.name}",
                SpanKind.INTERNAL,
                attributes,
                clock,
                scripted,
                latency if scripted else None,
                step,
                status=_status(StatusCode.ERROR, failure.message),
                error_type=failure.error_type,
            )
            return
        if self.content_mode in {"preview", "full"}:
            attributes["houghtonlabs.tool.result_preview"] = _preview(prepared.result)
        if self.content_mode == "full":
            attributes["gen_ai.tool.call.result"] = json.dumps(prepared.result, sort_keys=True)
        self._emit(
            f"execute_tool {call.name}",
            SpanKind.INTERNAL,
            attributes,
            clock,
            scripted,
            latency if scripted else None,
            step,
            status=_status(StatusCode.OK),
        )

    def _state_span(self, task, key: str, value: dict, memory: dict, clock, scripted, step) -> None:
        before = memory.get(key)
        before_present = key in memory
        memory[key] = value
        attributes = {
            "gen_ai.agent.name": self.agent["name"],
            "gen_ai.conversation.id": task["conversation_id"],
            "houghtonlabs.state.key": key,
            "houghtonlabs.state.op": "set",
            "houghtonlabs.state.before_present": before_present,
            "houghtonlabs.state.before_sha256": sha256_hex(before) if before_present else sha256_hex(None),
            "houghtonlabs.state.after_sha256": sha256_hex(value),
            "houghtonlabs.step": step["n"] + 1,
        }
        if self.content_mode in {"preview", "full"}:
            attributes["houghtonlabs.state.preview"] = _preview(value)
        self._emit(
            f"state.update {key}",
            SpanKind.INTERNAL,
            attributes,
            clock,
            scripted,
            STATE_MS if scripted else None,
            step,
            status=_status(StatusCode.OK),
        )

    def _emit(self, name, kind, attributes, clock, scripted, duration_ms, step, status=None, error_type=None) -> None:
        step["n"] += 1
        attributes["houghtonlabs.step"] = step["n"]
        span = self._start(name, kind, attributes, clock, scripted, new_trace=False)
        token = otel_context.attach(trace.set_span_in_context(span))
        try:
            if error_type:
                span.set_attribute("error.type", error_type)
            span.set_status(status or _status(StatusCode.OK))
        finally:
            self._end(span, token, clock, scripted, duration_ms)

    def _start(self, name, kind, attributes, clock, scripted, new_trace: bool):
        kwargs = {"kind": kind, "attributes": attributes}
        if scripted:
            kwargs["start_time"] = clock.now_ns()
        if new_trace:
            kwargs["context"] = otel_context.Context()
        return self.tracer.start_span(name, **kwargs)

    def _end(self, span, token, clock, scripted, duration_ms) -> None:
        try:
            if scripted:
                if duration_ms is not None:
                    clock.advance_ms(duration_ms)
                span.end(end_time=clock.now_ns())
            else:
                span.end()
        finally:
            otel_context.detach(token)


def _assistant_message(completion: Completion) -> dict:
    message = {"role": "assistant", "content": completion.text}
    if completion.tool_calls:
        message["tool_calls"] = [
            {"id": call.id, "name": call.name, "arguments": call.arguments} for call in completion.tool_calls
        ]
    return message


def _tool_message(call: ToolCall, payload: dict) -> dict:
    return {
        "role": "tool",
        "tool_call_id": call.id,
        "name": call.name,
        "content": json.dumps(payload, sort_keys=True, ensure_ascii=False),
    }


def _genai_messages(messages: list[dict]) -> list[dict]:
    converted = []
    for message in messages:
        if message["role"] == "system":
            continue
        if message["role"] == "tool":
            converted.append(
                {
                    "role": "tool",
                    "parts": [
                        {
                            "type": "tool_call_response",
                            "id": message.get("tool_call_id") or "",
                            "response": message.get("content") or "",
                        }
                    ],
                }
            )
            continue
        parts = []
        if message.get("content"):
            parts.append({"type": "text", "content": message["content"]})
        for call in message.get("tool_calls") or []:
            parts.append(
                {
                    "type": "tool_call",
                    "id": call.get("id") or "",
                    "name": call["name"],
                    "arguments": call["arguments"],
                }
            )
        role = "assistant" if message["role"] == "assistant" else "user"
        converted.append({"role": role, "parts": parts})
    return converted


def _output_parts(completion: Completion) -> list[dict]:
    parts = []
    if completion.text:
        parts.append({"type": "text", "content": completion.text})
    for call in completion.tool_calls:
        parts.append(
            {
                "type": "tool_call",
                "id": call.id,
                "name": call.name,
                "arguments": call.arguments,
            }
        )
    return parts
