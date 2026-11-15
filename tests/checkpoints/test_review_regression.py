import json
from typing import TypedDict
from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.sqlite import SqliteSaver
from langsmith import tracing_context
from agentwrench.checkpoints.codec import preview
from .test_scope import run


class State(TypedDict):
    data: dict


def test_real_checkpoint_diff_with_bytes_map_keys(tmp_path):
    path = tmp_path / "bytes.db"
    with SqliteSaver.from_conn_string(str(path)) as saver:
        graph = StateGraph(State)
        graph.add_node("step", lambda state: {"data": {b"key": state["data"][b"key"] + 1}})
        graph.add_edge(START, "step")
        graph.add_edge("step", END)
        with tracing_context(enabled=False):
            graph.compile(checkpointer=saver).invoke({"data": {b"key": 1}}, {"configurable": {"thread_id": "bytes"}})
    result = run("diff", path, "--thread", "bytes", "--last", 2, "--json")
    assert result.returncode == 0, result.stderr
    changes = json.loads(result.stdout)["changes"]
    data = next(row for row in changes if row["channel"] == "data")
    assert "key" in data["before"] and "key" in data["after"]
    assert "1" in data["before"] and "2" in data["after"]


def test_non_json_map_preview_is_bounded_and_terminal_safe():
    result = preview({b"\x1b[31m": {b"nested": "x" * 500}}, limit=80)
    assert "\x1b" not in result
    assert len(result) <= 83
    assert result.endswith("...")
