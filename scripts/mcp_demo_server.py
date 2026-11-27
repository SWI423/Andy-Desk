"""
最小 MCP server 演示脚本（stdio + JSON-RPC 2.0）
用途：本地验证 Andy 的 MCP 客户端，不依赖第三方 mcp 包。
工具：
  echo(text)        只读，原样返回
  now()             只读，返回当前时间
  write_note(text)  写入型，追加到 MCP_DEMO_NOTE 指定的文件
"""

import json
import os
import sys
from datetime import datetime

PROTOCOL_VERSION = "2024-11-05"

TOOLS = [
    {
        "name": "echo",
        "description": "原样返回输入文本，用于验证 MCP 链路",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string", "description": "要回显的文本"}},
            "required": ["text"],
        },
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "now",
        "description": "返回本机当前日期和时间",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "write_note",
        "description": "把一段文字追加到演示笔记文件（写入型）",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string", "description": "要追加的内容"}},
            "required": ["text"],
        },
        "annotations": {"readOnlyHint": False},
    },
]


def _text_result(text: str, is_error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def _call(name: str, args: dict) -> dict:
    if name == "echo":
        return _text_result(f"echo: {args.get('text', '')}")
    if name == "now":
        return _text_result(datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    if name == "write_note":
        path = os.getenv("MCP_DEMO_NOTE", "").strip()
        if not path:
            return _text_result("未配置 MCP_DEMO_NOTE，拒绝写入", True)
        try:
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                f.write(str(args.get("text", "")) + "\n")
            return _text_result(f"已写入 {path}")
        except Exception as e:
            return _text_result(f"写入失败：{e}", True)
    return _text_result(f"未知工具 {name}", True)


def _response(req_id, result=None, error=None) -> dict:
    msg = {"jsonrpc": "2.0", "id": req_id}
    if error is not None:
        msg["error"] = error
    else:
        msg["result"] = result
    return msg


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError as e:
            sys.stdout.write(json.dumps(_response(None, error={
                "code": -32700, "message": f"Parse error: {e}"})) + "\n")
            sys.stdout.flush()
            continue
        req_id = req.get("id")
        method = req.get("method")
        params = req.get("params") or {}
        if method == "initialize":
            result = {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "andy-demo", "version": "1.0.0"},
            }
        elif method in ("notifications/initialized", "notifications/cancelled"):
            continue
        elif method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "tools/call":
            result = _call(params.get("name"), params.get("arguments") or {})
        else:
            if req_id is not None:
                sys.stdout.write(json.dumps(_response(req_id, error={
                    "code": -32601, "message": f"Method not found: {method}"})) + "\n")
                sys.stdout.flush()
            continue
        if req_id is not None:
            sys.stdout.write(json.dumps(_response(req_id, result), ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
