"""
多轮工具验证：先查再改（模拟"做完了"这类需要两轮工具的场景）
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("DATA_DIR", "data/tooltest")

from core import db, tasks, brain

db.init_db()
t = tasks.create_task("模电实验报告", "study", priority=1)
print("created:", t["id"], t["task"]["title"])

res = brain.ask_baymax("模电实验报告我做完了")
print("reply:", res["reply"])
print("tools:", res["tools_called"])
print("status now:", tasks.get_task(t["id"])["status"], tasks.get_task(t["id"])["progress"])
print("PASS" if tasks.get_task(t["id"])["status"] == "done" else "FAIL")
