"""
MCP 客户端测试：协议握手、工具发现、schema 转换、读写权限、失败隔离
全部用本地 demo server，不联网。
"""

import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core import mcp_client


def _make_manager(tmp_path, allow_write=True, confirm=None, command=None):
    cfg = {
        "mcpServers": {
            "demo": {
                "command": command or sys.executable,
                "args": [os.path.join(ROOT, "scripts", "mcp_demo_server.py")],
                "cwd": ROOT,
                "env": {"MCP_DEMO_NOTE": str(tmp_path / "note.txt")},
                "allow_write": True,
                "timeout": 5,
            }
        }
    }
    path = tmp_path / "mcp.json"
    path.write_text(json.dumps(cfg), encoding="utf-8")
    return mcp_client.MCPManager(config_path=str(path), enabled=True,
                                 allow_write=allow_write, timeout=5,
                                 max_tools=10, confirm=confirm)


def test_function_name_sanitized():
    assert mcp_client.make_function_name("my server", "read.file") == \
        "mcp__my_server__read_file"
    assert len(mcp_client.make_function_name("x" * 40, "y" * 40)) <= 64


def test_tool_schema_and_readonly_call(tmp_path):
    mgr = _make_manager(tmp_path)
    try:
        mgr.start_all()
        assert mgr.health_check(), mgr.errors
        names = {t["function"]["name"] for t in mgr.get_openai_tools()}
        assert "mcp__demo__echo" in names
        assert "mcp__demo__now" in names
        assert "mcp__demo__write_note" in names
        echo = next(t for t in mgr.get_openai_tools()
                    if t["function"]["name"] == "mcp__demo__echo")
        assert echo["function"]["parameters"]["required"] == ["text"]
        res = mgr.call_tool("mcp__demo__echo", {"text": "hello"})
        assert res["ok"] and "echo: hello" in res["content"]
        now = mgr.call_tool("mcp__demo__now", {})
        assert now["ok"] and now["content"]
    finally:
        mgr.shutdown()


def test_write_tool_denied_without_confirm(tmp_path):
    mgr = _make_manager(tmp_path, allow_write=True, confirm=None)
    try:
        mgr.start_all()
        res = mgr.call_tool("mcp__demo__write_note", {"text": "x"})
        assert not res["ok"] and "确认" in res["error"]
        assert not (tmp_path / "note.txt").exists()
    finally:
        mgr.shutdown()


def test_write_tool_requires_allow_write(tmp_path):
    mgr = _make_manager(tmp_path, allow_write=False,
                        confirm=lambda *_: True)
    try:
        mgr.start_all()
        res = mgr.call_tool("mcp__demo__write_note", {"text": "x"})
        assert not res["ok"] and "未授权" in res["error"]
    finally:
        mgr.shutdown()


def test_write_tool_confirmed_writes(tmp_path):
    seen = []
    mgr = _make_manager(tmp_path, allow_write=True,
                        confirm=lambda s, t, a: seen.append((s, t, a)) or True)
    try:
        mgr.start_all()
        res = mgr.call_tool("mcp__demo__write_note", {"text": "记一笔"})
        assert res["ok"], res
        assert seen and seen[0][0] == "demo" and seen[0][1] == "write_note"
        assert "记一笔" in (tmp_path / "note.txt").read_text(encoding="utf-8")
    finally:
        mgr.shutdown()


def test_bad_config_does_not_raise(tmp_path):
    mgr = _make_manager(tmp_path, command=str(tmp_path / "no_such_cmd.exe"))
    mgr.start_all()
    assert not mgr.health_check()
    assert mgr.errors
    mgr.shutdown()


def test_no_config_is_healthy(tmp_path):
    mgr = mcp_client.MCPManager(config_path=str(tmp_path / "missing.json"),
                                enabled=True)
    mgr.start_all()
    assert mgr.health_check()
    assert mgr.get_openai_tools() == []
    mgr.shutdown()
