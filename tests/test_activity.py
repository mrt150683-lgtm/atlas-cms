import json
from pathlib import Path

from cms.activity import log_activity, read_activity
from tests.test_mcp import _server, _tool


def test_log_and_read(tmp_path: Path) -> None:
    log_activity(tmp_path, "query_codebase", ["file:a.py", "func:a.py::f"], label="find auth")
    events = read_activity(tmp_path, since=0)
    assert len(events) == 1
    assert events[0]["tool"] == "query_codebase"
    assert events[0]["nodes"] == ["file:a.py", "func:a.py::f"]
    assert read_activity(tmp_path, since=events[0]["ts"]) == []


def test_rotation_keeps_file_bounded(tmp_path: Path) -> None:
    for i in range(3000):
        log_activity(tmp_path, "t", [f"file:{i}.py"])
    size = (tmp_path / "activity.jsonl").stat().st_size
    assert size < 256_000


def test_mcp_calls_write_activity(tmp_path: Path) -> None:
    server = _server(tmp_path)
    _tool(server, "query_codebase", {"query": "greet"})
    _tool(server, "get_feature_trace", {"name": "Greeting"})
    lines = (tmp_path / ".memory" / "activity.jsonl").read_text(encoding="utf-8").splitlines()
    events = [json.loads(l) for l in lines]
    assert [e["tool"] for e in events] == ["query_codebase", "get_feature_trace"]
    assert any(n.startswith("func:app.py") for n in events[0]["nodes"])
    assert "feature:Greeting" in events[1]["nodes"]
    assert events[0]["label"] == "greet"
