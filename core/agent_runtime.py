"""Agent 运行时：把一次对话变成可消费的事件流，并写轨迹日志。

事件协议（JSON 友好）：
    text_delta      {"text": "..."}
    tool_start      {"name": "...", "args": {...}}
    approval_request{"name": "...", "args": {...}, "server": "..."}
    tool_result     {"name": "...", "ok": true/false, "error": "...", "summary": "..."}
    done            {"reply": "...", "tools_called": [...], "tool_results": [...],
                     "context_meta": {...}, "context_preview": "...", "error": null,
                     "cancelled": false}
    error           {"message": "...", "kind": "..."}

桌面和 CLI 都只消费这些事件，不再各自解析 `brain.ask_baymax` 的返回值。
"""

import time
import uuid
from datetime import datetime

from console import logger
from core import brain
from core.trajectory import TrajectoryWriter

EVENT_TYPES = ("text_delta", "tool_start", "tool_result",
               "approval_request", "done", "error")


def new_turn_id() -> str:
    return uuid.uuid4().hex[:12]


class AgentRuntime:
    def __init__(self, on_event=None, session_id: str = None, trajectory_path: str = None):
        self.on_event = on_event
        self.session_id = session_id
        self.trajectory_path = trajectory_path

    def run(self, text: str, should_stop=None, on_event=None) -> dict:
        """跑一轮对话，返回 done 事件里的结果字典。"""
        text = (text or "").strip()
        turn_id = new_turn_id()
        sink = on_event if on_event is not None else self.on_event
        writer = TrajectoryWriter(turn_id, self.trajectory_path)
        started = time.perf_counter()
        state = {"done": None, "error": None}

        def emit(event: dict) -> None:
            row = dict(event or {})
            row["turn_id"] = turn_id
            row.setdefault("ts", datetime.now().isoformat(timespec="milliseconds"))
            if row.get("type") == "done":
                state["done"] = row
            try:
                writer.record(row)
            except Exception as e:
                logger.warning("[runtime] 轨迹写入失败: %s", e)
            if sink:
                try:
                    sink(row)
                except Exception as e:
                    # UI 自己的绘制错误不能反向打断 Agent 循环
                    logger.warning("[runtime] 事件消费失败: %s", e)

        def stop_requested() -> bool:
            if should_stop is None:
                return False
            try:
                return bool(should_stop() if callable(should_stop) else should_stop)
            except Exception:
                return False

        emit({"type": "turn_start", "input": text})
        try:
            result = brain.ask_baymax(
                text,
                on_event=emit,
                should_stop=stop_requested,
                session_id=self.session_id or brain.SESSION_ID,
            )
        except Exception as e:
            logger.error("[runtime] 对话运行失败: %s", e)
            emit({"type": "error", "kind": type(e).__name__, "message": str(e)})
            result = {"reply": "", "tools_called": [], "tool_results": [],
                      "error": str(e), "cancelled": False}

        if not isinstance(result, dict):
            result = {"reply": str(result or ""), "error": None}
        if not state["done"]:
            emit({"type": "done", **result})

        elapsed_ms = int((time.perf_counter() - started) * 1000)
        result.setdefault("turn_id", turn_id)
        result.setdefault("duration_ms", elapsed_ms)
        emit({"type": "turn_end", "duration_ms": elapsed_ms,
              "ok": not bool(result.get("error"))})
        return result
