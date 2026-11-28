"""轨迹日志：每轮对话按事件写 JSONL，便于回放和定位问题。

文件默认在 `logs/trajectory.jsonl`，一行一个事件，不写密钥、不写完整系统提示词。
"""

import json
import os
import threading
from datetime import datetime

from config import LOG_DIR

_LOCK = threading.Lock()


def trajectory_path() -> str:
    return os.path.join(LOG_DIR, "trajectory.jsonl")


class TrajectoryWriter:
    """一轮对话一个 writer；线程内追加，跨线程加锁。"""

    def __init__(self, turn_id: str, path: str = None):
        self.turn_id = turn_id
        self.path = path or trajectory_path()
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)

    def record(self, event: dict) -> None:
        row = dict(event or {})
        row.setdefault("turn_id", self.turn_id)
        row.setdefault("ts", datetime.now().isoformat(timespec="milliseconds"))
        line = json.dumps(row, ensure_ascii=False, default=str)
        with _LOCK:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(line + "\n")


def read_trajectory(turn_id: str = None, limit: int = None, path: str = None) -> list:
    """读取轨迹；可按 turn_id 过滤，limit 只保留最后 N 条事件。"""
    path = path or trajectory_path()
    if not os.path.isfile(path):
        return []
    rows = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if turn_id and row.get("turn_id") != turn_id:
                continue
            rows.append(row)
    if limit:
        rows = rows[-limit:]
    return rows


def last_turn_id() -> str:
    rows = read_trajectory(limit=1)
    return rows[-1].get("turn_id", "") if rows else ""
