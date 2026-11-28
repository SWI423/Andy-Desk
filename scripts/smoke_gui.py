"""
GUI 打通验证：建任务 -> 发消息 -> 模型回复 -> 触发提醒 -> 截图
用法：python scripts/smoke_gui.py
会使用独立库 data/gui_smoke/baymax.db，不动正式数据
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ["DATA_DIR"] = sys.argv[1] if len(sys.argv) > 1 else "data/gui_smoke"

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QApplication

from config import LOG_DIR, ensure_dirs
from desktop.theme import STYLE
from desktop.main_window import BaymaxWindow
from core import db, tasks, scheduler

ensure_dirs()
db.init_db()

app = QApplication(sys.argv)
app.setStyleSheet(STYLE)
win = BaymaxWindow()
win.show()

state = {"reply": None, "shot": os.path.join(LOG_DIR, "gui_flow.png")}


def phase1():
    print("[1] 建任务")
    t = tasks.create_task("模电实验报告", "study", priority=1, due_at="2026-11-27")
    print("    task id:", t["id"])
    win._refresh_all()

    print("[2] 发消息（走模型 + 工具）")
    win.input.setText("今天学习上有什么要做的？")
    win.send()
    win._worker.event.connect(lambda e: phase2(e) if e.get("type") == "done" else None)


def phase2(event):
    state["reply"] = event.get("reply")
    print("    reply:", event.get("reply"))
    print("    tools:", event.get("tools_called"), "error:", event.get("error"))

    print("[3] 触发提醒")
    r = scheduler.create_reminder("喝水休息一下", "daily", "09:00")
    print("    reminder:", r.get("ok"), r.get("next_fire_at"))
    win.sched.fire_now(r["id"])

    QTimer.singleShot(800, phase3)


def phase3():
    win.grab().save(state["shot"])
    print("[4] 截图:", state["shot"])
    chat_text = win.chat.plain_text()
    print("    chat tail:", chat_text.strip().splitlines()[-2:])
    ok = bool(state["reply"]) and "正在思考" not in chat_text
    print("RESULT:", "PASS" if ok else "FAIL")
    app.quit()


QTimer.singleShot(500, phase1)
QTimer.singleShot(25000, app.quit)   # 兜底超时

sys.exit(app.exec())
