"""运行时事件协议与轨迹日志测试。"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import brain
from core.agent_runtime import AgentRuntime
from core.trajectory import read_trajectory


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    from core import embedding
    monkeypatch.setattr(embedding, "_OVERRIDE", lambda texts: None)


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    from core import db, memory
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "runtime.db"))
    monkeypatch.setattr(db, "_CONN", None)
    monkeypatch.setattr(memory, "_VEC_CACHE", {"key": None, "ids": None, "mat": None,
                                               "mean": None, "centered": False})
    db.init_db()
    yield db
    if db._CONN is not None:
        db._CONN.close()
        db._CONN = None


def test_runtime_wraps_brain_into_ordered_events(tmp_path, monkeypatch):
    path = tmp_path / "trajectory.jsonl"

    def fake_ask(text, on_event=None, should_stop=None, session_id=None):
        assert callable(should_stop)
        on_event({"type": "text_delta", "text": "你好"})
        on_event({"type": "tool_start", "name": "task_list", "args": {}})
        on_event({"type": "tool_result", "name": "task_list", "ok": True,
                  "summary": "1 条任务", "error": ""})
        result = {"reply": "你好，今天有 1 条任务。", "tools_called": ["task_list"],
                  "tool_results": [], "context_meta": {}, "error": None,
                  "cancelled": False}
        on_event({"type": "done", **result})
        return result

    monkeypatch.setattr(brain, "ask_baymax", fake_ask)
    events = []
    runtime = AgentRuntime(trajectory_path=str(path))
    result = runtime.run("今天有什么任务？", on_event=events.append)

    kinds = [e["type"] for e in events]
    assert kinds[0] == "turn_start"
    assert kinds[-1] == "turn_end"
    assert kinds[1:4] == ["text_delta", "tool_start", "tool_result"]
    assert "done" in kinds
    assert result["reply"] == "你好，今天有 1 条任务。"
    assert result["turn_id"] == events[0]["turn_id"]

    rows = read_trajectory(path=str(path))
    assert len(rows) >= len(events)
    assert all(r["turn_id"] == result["turn_id"] for r in rows)
    assert any(r["type"] == "tool_start" for r in rows)


def test_runtime_marks_cancelled_without_polluting_reply(tmp_path, monkeypatch):
    path = tmp_path / "trajectory.jsonl"

    def fake_ask(text, on_event=None, should_stop=None, session_id=None):
        on_event({"type": "text_delta", "text": "说到一半"})
        return {"reply": "", "tools_called": [], "tool_results": [],
                "context_meta": {}, "error": None, "cancelled": True}

    monkeypatch.setattr(brain, "ask_baymax", fake_ask)
    events = []
    runtime = AgentRuntime(trajectory_path=str(path))
    result = runtime.run("讲个长故事", on_event=events.append)

    done = [e for e in events if e["type"] == "done"][-1]
    assert done["cancelled"] is True
    assert done["reply"] == ""
    assert result["cancelled"] is True
    assert events[-1]["type"] == "turn_end"


def test_brain_stop_returns_cancelled_done(tmp_db, monkeypatch):
    """用户点停止：保留已流出的字，done 标 cancelled，不写假回复。"""
    def fake_stream(messages, on_chunk=None, tool_choice="auto", **kwargs):
        on_chunk("说到一半")
        raise brain.TurnCancelled()

    monkeypatch.setattr(brain, "_stream_once", fake_stream)
    events = []
    result = brain.ask_baymax("讲个很长的故事", on_chunk=lambda s: None,
                              on_event=events.append, should_stop=lambda: True)
    done = [e for e in events if e["type"] == "done"][-1]
    assert done["cancelled"] is True
    assert done["reply"] == ""
    assert result["cancelled"] is True
    assert "嗯，我在" not in result["reply"]


def test_brain_emits_tool_events(tmp_db, monkeypatch):
    """工具调用不只改返回值，还要给出 start/result 事件。"""
    calls = {"n": 0}

    def fake_stream(messages, on_chunk=None, tool_choice="auto", **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return "", [{"id": "call_1", "name": "task_list",
                         "arguments": "{}"}]
        return "今天没有任务。", []

    monkeypatch.setattr(brain, "_stream_once", fake_stream)
    monkeypatch.setattr(brain.tools, "call_tool",
                        lambda name, args: {"ok": True, "tasks": []})
    events = []
    result = brain.ask_baymax("今天有什么任务？", on_event=events.append)

    kinds = [e["type"] for e in events]
    assert "tool_start" in kinds
    assert "tool_result" in kinds
    assert kinds[-1] == "done"
    assert result["tools_called"] == ["task_list"]
    assert result["reply"] == "今天没有任务。"
