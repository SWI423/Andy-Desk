"""
MCP 客户端 mcp_client.py
- 通过 stdio 启动 MCP server，走 JSON-RPC 2.0 做 initialize / tools/list / tools/call
- 不依赖第三方 mcp 包：协议自己实现，便于离线、可控、好排查
- 安全边界：读工具直接放行；写工具需要 server 级 allow_write + 回调确认
- 失败隔离：单个 server 起不来 / 超时，只影响它自己，不拖垮对话
"""

import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from config import (BASE_DIR, MCP_ALLOW_WRITE, MCP_CONFIG_PATH, MCP_ENABLED,
                    MCP_MAX_TOOLS, MCP_TIMEOUT)
from console import logger

PROTOCOL_VERSION = "2024-11-05"
_CLIENT_INFO = {"name": "Andy", "version": "1.0.0"}


class MCPError(RuntimeError):
    """MCP 协议或进程错误"""


class MCPTimeout(MCPError):
    """等待 server 响应超时"""


class MCPPermissionDenied(MCPError):
    """写工具未授权或用户拒绝"""


def _safe_name(value: str, limit: int = 28) -> str:
    """OpenAI function name 只允许字母数字下划线横线，且不能太长"""
    cleaned = re.sub(r"[^A-Za-z0-9_-]", "_", value or "tool")
    if len(cleaned) > limit:
        cleaned = cleaned[:limit]
    return cleaned or "tool"


def make_function_name(server: str, tool: str) -> str:
    """统一前缀，避免和内置工具重名；模型看到的名字也能看出是 MCP"""
    return f"mcp__{_safe_name(server)}__{_safe_name(tool)}"


def _expand(value: str) -> str:
    """展开 $VAR / ${VAR} / %VAR% / ~，方便配置写相对当前环境"""
    if not isinstance(value, str):
        return value
    value = os.path.expanduser(os.path.expandvars(value))
    for key in re.findall(r"%([^%]+)%", value):
        value = value.replace(f"%{key}%", os.getenv(key, ""))
    return value


def _load_json_config(path: str) -> dict:
    if not path or not os.path.isfile(path):
        return {}
    with open(path, "r", encoding="utf-8-sig") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise MCPError("MCP 配置必须是 JSON 对象")
    return data


@dataclass
class ServerConfig:
    name: str
    command: str
    args: List[str] = field(default_factory=list)
    env: Dict[str, str] = field(default_factory=dict)
    cwd: str = ""
    enabled: bool = True
    allow_write: bool = False
    assume_read_only: bool = False
    timeout: float = 20.0


def load_servers(path: str = None) -> Dict[str, ServerConfig]:
    """读取 mcp_servers.json；兼容 mcpServers / servers 两种写法"""
    path = path or MCP_CONFIG_PATH
    data = _load_json_config(path)
    raw = data.get("mcpServers") or data.get("servers") or {}
    if not isinstance(raw, dict):
        raise MCPError("mcpServers 必须是对象")
    config_dir = os.path.dirname(os.path.abspath(path)) if path else BASE_DIR
    servers: Dict[str, ServerConfig] = {}
    for name, item in raw.items():
        if not isinstance(item, dict):
            logger.warning("[mcp] 跳过配置 %s：不是对象", name)
            continue
        command = _expand(str(item.get("command") or "")).strip()
        if not command:
            logger.warning("[mcp] 跳过配置 %s：缺少 command", name)
            continue
        args = [_expand(str(a)) for a in (item.get("args") or [])]
        env = {str(k): _expand(str(v)) for k, v in (item.get("env") or {}).items()}
        cwd = _expand(str(item.get("cwd") or ""))
        if cwd and not os.path.isabs(cwd):
            cwd = os.path.join(config_dir, cwd)
        servers[name] = ServerConfig(
            name=name,
            command=command,
            args=args,
            env=env,
            cwd=cwd,
            enabled=bool(item.get("enabled", True)),
            allow_write=bool(item.get("allow_write", False)),
            assume_read_only=bool(item.get("assume_read_only", False)),
            timeout=float(item.get("timeout") or MCP_TIMEOUT),
        )
    return servers


class MCPClient:
    """一个 MCP server 对应一个子进程 + 一条请求队列"""

    def __init__(self, cfg: ServerConfig):
        self.cfg = cfg
        self.proc: Optional[subprocess.Popen] = None
        self._pending: Dict[str, queue.Queue] = {}
        self._pending_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._reader: Optional[threading.Thread] = None
        self._err_reader: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._stderr_tail: List[str] = []
        self._stderr_lock = threading.Lock()
        self.server_info: dict = {}

    # ---- 生命周期 ----
    def start(self):
        if self.proc is not None:
            return
        env = os.environ.copy()
        env.update(self.cfg.env)
        kwargs = dict(
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            cwd=self.cfg.cwd or None,
            env=env,
        )
        if os.name == "nt":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        cmd = [self.cfg.command] + list(self.cfg.args)
        logger.info("[mcp] 启动 %s: %s", self.cfg.name, " ".join(cmd))
        self.proc = subprocess.Popen(cmd, **kwargs)
        self._reader = threading.Thread(target=self._read_stdout, daemon=True,
                                        name=f"mcp-{self.cfg.name}-out")
        self._err_reader = threading.Thread(target=self._read_stderr, daemon=True,
                                            name=f"mcp-{self.cfg.name}-err")
        self._reader.start()
        self._err_reader.start()
        self._initialize()

    def stop(self):
        self._stop.set()
        if self.proc is None:
            return
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.terminate()
            self.proc.wait(timeout=2)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass
        self.proc = None

    def _read_stdout(self):
        assert self.proc and self.proc.stdout
        for line in self.proc.stdout:
            if self._stop.is_set():
                break
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                logger.warning("[mcp:%s] 非 JSON 输出: %s", self.cfg.name, line[:200])
                continue
            if isinstance(msg, list):
                for item in msg:
                    self._dispatch(item)
            else:
                self._dispatch(msg)

    def _dispatch(self, msg: dict):
        if not isinstance(msg, dict):
            return
        req_id = msg.get("id")
        if req_id is not None:
            with self._pending_lock:
                q = self._pending.get(str(req_id))
            if q is not None:
                q.put(msg)
            else:
                logger.debug("[mcp:%s] 收到未知 id 响应: %s", self.cfg.name, req_id)
        else:
            method = msg.get("method")
            if method:
                logger.info("[mcp:%s] 通知 %s", self.cfg.name, method)

    def _read_stderr(self):
        assert self.proc and self.proc.stderr
        for line in self.proc.stderr:
            if self._stop.is_set():
                break
            line = line.rstrip()
            if not line:
                continue
            with self._stderr_lock:
                self._stderr_tail.append(line)
                self._stderr_tail = self._stderr_tail[-20:]
            logger.debug("[mcp:%s] %s", self.cfg.name, line[:300])

    def _request(self, method: str, params: dict, timeout: float = None) -> dict:
        if self.proc is None or self.proc.poll() is not None:
            raise MCPError(f"{self.cfg.name} 进程未运行")
        req_id = uuid.uuid4().hex
        q: queue.Queue = queue.Queue(maxsize=1)
        with self._pending_lock:
            self._pending[req_id] = q
        payload = {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}
        try:
            with self._write_lock:
                self.proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
                self.proc.stdin.flush()
            wait = timeout if timeout is not None else self.cfg.timeout
            try:
                msg = q.get(timeout=wait)
            except queue.Empty:
                if self.proc.poll() is not None:
                    stderr = self._stderr()
                    raise MCPError(f"{self.cfg.name} 已退出：{stderr}")
                raise MCPTimeout(f"{self.cfg.name} 在 {wait:g}s 内没有响应 {method}")
        finally:
            with self._pending_lock:
                self._pending.pop(req_id, None)
        if "error" in msg:
            err = msg.get("error") or {}
            raise MCPError(f"{self.cfg.name} {method} 返回错误：{err.get('message') or err}")
        return msg.get("result") or {}

    def _notify(self, method: str, params: dict = None):
        if self.proc is None or self.proc.poll() is not None:
            return
        payload = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        with self._write_lock:
            self.proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
            self.proc.stdin.flush()

    def _stderr(self) -> str:
        with self._stderr_lock:
            return " | ".join(self._stderr_tail[-5:])

    def _initialize(self):
        result = self._request("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": _CLIENT_INFO,
        })
        self.server_info = result.get("serverInfo") or {}
        self._notify("notifications/initialized", {})
        logger.info("[mcp:%s] 已连接 %s", self.cfg.name,
                    self.server_info.get("name") or "server")

    # ---- 能力 ----
    def list_tools(self) -> List[dict]:
        tools: List[dict] = []
        cursor = None
        for _ in range(10):
            params = {"cursor": cursor} if cursor else {}
            result = self._request("tools/list", params)
            batch = result.get("tools") or []
            if not isinstance(batch, list):
                raise MCPError(f"{self.cfg.name} tools/list 返回格式错误")
            tools.extend(batch)
            cursor = result.get("nextCursor")
            if not cursor:
                break
        return tools

    def call_tool(self, name: str, arguments: dict) -> dict:
        result = self._request("tools/call", {"name": name, "arguments": arguments or {}})
        is_error = bool(result.get("isError"))
        structured = result.get("structuredContent")
        if isinstance(structured, dict):
            out = dict(structured)
            out.setdefault("ok", not is_error)
            return out
        texts = []
        for item in result.get("content") or []:
            if isinstance(item, dict) and item.get("type") == "text":
                texts.append(str(item.get("text") or ""))
            elif isinstance(item, dict):
                texts.append(f"[{item.get('type') or 'content'} 已返回]")
        text = "\n".join(t for t in texts if t).strip()
        if is_error:
            return {"ok": False, "error": text or "MCP 工具返回错误"}
        return {"ok": True, "content": text, "server": self.cfg.name}


class MCPManager:
    """多 server 管理 + 工具适配 + 权限网关"""

    def __init__(self, config_path: str = None, enabled: bool = None,
                 allow_write: bool = None, timeout: float = None,
                 max_tools: int = None, confirm: Callable[[str, str, dict], bool] = None):
        self.config_path = config_path or MCP_CONFIG_PATH
        self.enabled = MCP_ENABLED if enabled is None else bool(enabled)
        self.allow_write = MCP_ALLOW_WRITE if allow_write is None else bool(allow_write)
        self.timeout = MCP_TIMEOUT if timeout is None else float(timeout)
        self.max_tools = MCP_MAX_TOOLS if max_tools is None else int(max_tools)
        self.confirm = confirm
        self.clients: Dict[str, MCPClient] = {}
        self.tools: List[dict] = []
        self._by_name: Dict[str, dict] = {}
        self.errors: List[str] = []
        self.started = False

    def set_confirm_callback(self, fn: Callable[[str, str, dict], bool]):
        self.confirm = fn

    def start_all(self):
        if not self.enabled:
            logger.info("[mcp] 已关闭（MCP_ENABLED=0）")
            return
        if self.started:
            self.shutdown()
        self.errors = []
        self.tools = []
        self._by_name = {}
        if not os.path.isfile(self.config_path):
            logger.info("[mcp] 未配置 %s，跳过", self.config_path)
            self.started = True
            return
        try:
            servers = load_servers(self.config_path)
        except Exception as e:
            self.errors.append(f"配置读取失败：{e}")
            logger.error("[mcp] 配置读取失败: %s", e)
            self.started = True
            return
        for name, cfg in servers.items():
            if not cfg.enabled:
                logger.info("[mcp] %s 已禁用", name)
                continue
            if self.timeout and not cfg.timeout:
                cfg.timeout = self.timeout
            client = MCPClient(cfg)
            try:
                client.start()
                raw_tools = client.list_tools()
            except Exception as e:
                self.errors.append(f"{name}: {e}")
                logger.error("[mcp] %s 启动失败: %s", name, e)
                try:
                    client.stop()
                except Exception:
                    pass
                continue
            self.clients[name] = client
            for raw in raw_tools:
                self._adapt_tool(name, cfg, raw)
        self.started = True
        logger.info("[mcp] 已连接 %s 个 server，暴露 %s 个工具",
                    len(self.clients), len(self.tools))

    def _adapt_tool(self, server: str, cfg: ServerConfig, raw: dict):
        if not isinstance(raw, dict) or not raw.get("name"):
            return
        if len(self.tools) >= self.max_tools:
            logger.warning("[mcp] 工具数超过上限 %s，跳过 %s/%s",
                           self.max_tools, server, raw.get("name"))
            return
        tool_name = str(raw["name"])
        fn_name = make_function_name(server, tool_name)
        if fn_name in self._by_name:
            logger.warning("[mcp] 工具重名，跳过 %s/%s", server, tool_name)
            return
        annotations = raw.get("annotations") or {}
        read_only = annotations.get("readOnlyHint") is True
        if not read_only and cfg.assume_read_only:
            read_only = True
        description = str(raw.get("description") or f"{server} 提供的 {tool_name} 工具")
        schema = raw.get("inputSchema") or {"type": "object", "properties": {}}
        if not isinstance(schema, dict) or schema.get("type") != "object":
            schema = {"type": "object", "properties": {}}
        adapter = {
            "server": server,
            "tool": tool_name,
            "fn_name": fn_name,
            "read_only": read_only,
            "allow_write": bool(self.allow_write and cfg.allow_write),
            "schema": {
                "type": "function",
                "function": {
                    "name": fn_name,
                    "description": f"[MCP:{server}] {description}",
                    "parameters": schema,
                },
            },
        }
        self.tools.append(adapter["schema"])
        self._by_name[fn_name] = adapter

    def get_openai_tools(self) -> List[dict]:
        return [t for t in self.tools]

    def call_tool(self, fn_name: str, arguments: dict) -> dict:
        adapter = self._by_name.get(fn_name)
        if not adapter:
            return {"ok": False, "error": f"未知 MCP 工具 {fn_name}"}
        if not adapter["read_only"]:
            if not adapter["allow_write"]:
                return {"ok": False,
                        "error": (f"MCP 工具 {adapter['server']}/{adapter['tool']} 属于写入型，"
                                  "当前未授权；请在配置里 allow_write=true 并设置 MCP_ALLOW_WRITE=1")}
            if self.confirm is not None:
                try:
                    approved = bool(self.confirm(adapter["server"], adapter["tool"], arguments or {}))
                except Exception as e:
                    return {"ok": False, "error": f"确认回调失败：{e}"}
                if not approved:
                    return {"ok": False, "error": "用户拒绝执行该 MCP 写入操作"}
            else:
                return {"ok": False, "error": "写入型 MCP 工具需要确认回调；当前未设置，已拒绝"}
        client = self.clients.get(adapter["server"])
        if client is None:
            return {"ok": False, "error": f"MCP server {adapter['server']} 未连接"}
        try:
            return client.call_tool(adapter["tool"], arguments or {})
        except Exception as e:
            logger.error("[mcp] 调用失败 %s/%s: %s", adapter["server"], adapter["tool"], e)
            return {"ok": False, "error": f"MCP 调用失败：{e}"}

    def is_write_tool(self, fn_name: str) -> bool:
        adapter = self._by_name.get(fn_name)
        return bool(adapter and not adapter["read_only"])

    def status(self) -> dict:
        return {
            "enabled": self.enabled,
            "config": self.config_path,
            "configured": os.path.isfile(self.config_path),
            "servers": list(self.clients),
            "tools": [a["fn_name"] for a in self._by_name.values()],
            "errors": list(self.errors),
        }

    def health_check(self) -> bool:
        if not self.enabled:
            return True
        if not os.path.isfile(self.config_path):
            return True
        return bool(self.clients) and not self.errors

    def shutdown(self):
        for name, client in list(self.clients.items()):
            try:
                client.stop()
            except Exception as e:
                logger.warning("[mcp] 关闭 %s 失败: %s", name, e)
        self.clients = {}
        self.started = False


# ---- 全局单例 ----
_MANAGER: Optional[MCPManager] = None


def get_manager() -> MCPManager:
    global _MANAGER
    if _MANAGER is None:
        _MANAGER = MCPManager()
    return _MANAGER


def start_all():
    get_manager().start_all()
    return get_manager()


def get_openai_tools() -> List[dict]:
    return get_manager().get_openai_tools()


def call_tool(fn_name: str, arguments: dict) -> dict:
    return get_manager().call_tool(fn_name, arguments)


def set_confirm_callback(fn):
    get_manager().set_confirm_callback(fn)


def is_write_tool(fn_name: str) -> bool:
    return get_manager().is_write_tool(fn_name)


def health_check() -> bool:
    return get_manager().health_check()


def shutdown():
    global _MANAGER
    if _MANAGER is not None:
        _MANAGER.shutdown()
