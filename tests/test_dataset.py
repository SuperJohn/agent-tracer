import json

import pytest

from agenttrace.dataset import load_dataset, repo_root, resolve_dataset_path


def test_shipped_dataset_loads():
    document = load_dataset(repo_root() / "demo" / "data" / "tasks.json")
    assert document["featured"]["failing"] == "inc-1042"
    assert document["featured"]["healthy"] == "inc-1041"
    assert len(document["tasks"]) == 16


def test_dataset_uri_override(monkeypatch, tmp_path):
    source = json.loads((repo_root() / "demo" / "data" / "tasks.json").read_text())
    source["tasks"] = source["tasks"][:2]
    path = tmp_path / "tasks.json"
    path.write_text(json.dumps(source), encoding="utf-8")
    monkeypatch.setenv("HL_DATASET_URI", path.as_uri())
    assert resolve_dataset_path() == path
    loaded = load_dataset()
    assert [task["id"] for task in loaded["tasks"]] == ["inc-1042", "inc-1041"]


def test_dataset_rejects_unknown_tool(tmp_path):
    source = json.loads((repo_root() / "demo" / "data" / "tasks.json").read_text())
    source["tasks"] = source["tasks"][:1]
    source["tasks"][0]["replay"][0]["tool_calls"][0]["name"] = "not.a.tool"
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(source), encoding="utf-8")
    with pytest.raises(ValueError, match="unknown tool"):
        load_dataset(path)
