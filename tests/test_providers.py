import io
import json

from agenttrace.providers import OllamaProvider, ollama_messages, ollama_tools
from agenttrace.tools import tool_definitions


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def test_ollama_request_is_deterministic_and_parses_usage(monkeypatch):
    captured = {}

    def opener(request, timeout):
        captured["url"] = request.full_url
        captured["timeout"] = timeout
        captured["body"] = json.loads(request.data.decode())
        payload = {
            "model": "qwen3.5:9b",
            "message": {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"type": "function", "function": {"name": "catalog.lookup", "arguments": {"model": "orders_daily"}}}
                ],
            },
            "prompt_eval_count": 40,
            "eval_count": 12,
        }
        return _Response(json.dumps(payload).encode())

    provider = OllamaProvider(host="http://127.0.0.1:11434", model="qwen3.5:9b", _opener=opener)
    completion = provider.complete(
        messages=[
            {"role": "system", "content": "Be brief."},
            {"role": "user", "content": "Check orders_daily."},
            {
                "role": "assistant",
                "content": "Looking it up.",
                "tool_calls": [{"id": "call_1", "name": "catalog.lookup", "arguments": {"model": "orders_daily"}}],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "{\"model\":\"orders_daily\"}"},
        ],
        tools=tool_definitions(),
        model="qwen3.5:9b",
        turn=1,
    )
    assert captured["url"] == "http://127.0.0.1:11434/api/chat"
    assert captured["body"]["stream"] is False
    assert captured["body"]["options"] == {"temperature": 0, "seed": 42}
    assert captured["body"]["tools"][0]["function"]["name"] == "catalog.lookup"
    assert captured["body"]["messages"][2]["tool_calls"][0]["function"]["name"] == "catalog.lookup"
    assert captured["body"]["messages"][3]["role"] == "tool"
    assert completion.finish_reason == "tool_calls"
    assert completion.tool_calls[0].id == ""
    assert completion.input_tokens == 40
    assert completion.output_tokens == 12
    assert completion.server_address == "127.0.0.1"
    assert completion.server_port == 11434
    assert provider.provider_name == "ollama"


def test_ollama_adapters_drop_agent_ids_from_the_wire_shape():
    messages = ollama_messages(
        [{"role": "assistant", "content": "ok", "tool_calls": [{"id": "call_x", "name": "sensor.status", "arguments": {"sensor": "raw_orders_arrival"}}]}]
    )
    assert "id" not in messages[0]["tool_calls"][0]["function"]
    tools = ollama_tools([{"name": "sensor.status", "description": "Read a sensor.", "parameters": {"type": "object"}}])
    assert tools[0]["type"] == "function"
