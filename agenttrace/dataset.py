"""Load the synthetic incident dataset. HL_DATASET_URI overrides the path."""

from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.parse import unquote, urlparse

from agenttrace.tools import TOOLS


def repo_root() -> Path:
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "pyproject.toml").exists() and (parent / "demo").is_dir():
            return parent
    return here.parents[1]


def resolve_dataset_path() -> Path:
    raw = (os.environ.get("HL_DATASET_URI") or "").strip()
    if not raw:
        return repo_root() / "demo" / "data" / "tasks.json"
    if raw.startswith("file://"):
        parsed = urlparse(raw)
        return Path(unquote(parsed.path))
    return Path(raw)


def load_dataset(path: Path | None = None) -> dict:
    dataset_path = path or resolve_dataset_path()
    with dataset_path.open(encoding="utf-8") as handle:
        document = json.load(handle)
    _validate(document)
    return document


def _validate(document: dict) -> None:
    if document.get("schema_version") != 1:
        raise ValueError("dataset schema_version must be 1")
    tasks = document.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        raise ValueError("dataset tasks must be a non-empty list")
    seen: set[str] = set()
    for task in tasks:
        task_id = task.get("id")
        if not task_id or task_id in seen:
            raise ValueError(f"duplicate or missing task id: {task_id!r}")
        seen.add(task_id)
        if not task.get("replay"):
            raise ValueError(f"{task_id} has no replay")
        for turn, step in enumerate(task["replay"]):
            for call in step.get("tool_calls") or []:
                name = call.get("name")
                if name not in TOOLS:
                    raise ValueError(f"{task_id} turn {turn} calls unknown tool {name}")
                if name not in (task.get("fixtures") or {}):
                    raise ValueError(f"{task_id} has no fixture for {name}")
    featured = document.get("featured") or {}
    for key in ("failing", "healthy"):
        if featured.get(key) not in seen:
            raise ValueError(f"featured.{key} is not a task id")
    if document["default_model"] not in document["price_card"]:
        raise ValueError("default_model is missing from price_card")
