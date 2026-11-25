"""
简单打通验证（不做大规模测试）
用法：python scripts/smoke_core.py [--no-llm]
会写入独立测试库 data/smoke_<时间>/，不污染正式数据；--no-llm 同时关闭联网向量
"""

import os
import sys
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("DATA_DIR", f"data/smoke_{datetime.now():%H%M%S}")
if "--no-llm" in sys.argv:
    os.environ["EMBED_ENABLED"] = "0"


def line(title):
    print(f"\n=== {title} ===")


def main():
    from core import db, tasks, memory, scheduler, tools

    line("1. 建表")
    db.init_db()
    print("DB:", db.DB_PATH)

    line("2. 任务 CRUD")
    due = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    r1 = tasks.create_task("软考冲刺复习", "study", priority=1, due_at=due)
    r2 = tasks.create_task("写周报", "work", priority=2)
    r3 = tasks.create_task("买牛奶", "life")
    print("created:", r1["id"], r2["id"], r3["id"])

    print("update:", tasks.update_task(r2["id"], status="doing", progress=50)["task"]["status_text"])
    print("done:", tasks.update_task(r1["id"], status="done")["task"]["progress"])
    print("list study:", [(t["title"], t["status_text"]) for t in tasks.list_tasks("study", include_done=True)])

    ov = tasks.today_overview()
    print("overview:", ov["due_today"], ov["overdue"], ov["doing"], ov["reminders_today"])
    print(tasks.overview_text())

    line("3. 拆解")
    parent = tasks.create_task("准备挑战杯答辩", "study", priority=1)
    print(tasks.add_steps(parent["id"], ["整理讲稿", "做 PPT", "模拟答辩"])["count"])

    line("4. 提醒规则")
    print(scheduler.compute_next("daily", "07:30"))
    print(scheduler.compute_next("weekly", "1 09:00"))
    rem = scheduler.create_reminder("弟弟写作业", "daily", "19:30")
    print("reminder:", rem)
    print("list:", [(x["content"], x["rule_desc"], x["next_fire_at"]) for x in scheduler.list_reminders()])

    line("5. 记忆")
    memory.log_event("user", "我叫小林，晚上十点后不要提醒我学习")
    print(memory.extract_facts("我叫小林，晚上十点后不要提醒我学习"))
    ctx = memory.build_context("今晚还学不学？")
    print("ctx tokens:", ctx["stats"], "\n", ctx["text"])

    line("6. 工具直调")
    r = tools.call_tool("task_create", {"title": "整理面试复盘", "domain": "work", "priority": 2})
    print("tool ok:", r.get("ok"), r.get("id"))
    print("tool bad args:", tools.call_tool("task_create", {"title": "x"}))
    print("bad json args:", tools.call_tool("task_update", {"task_id": 99999}))

    if "--no-llm" in sys.argv:
        print("\n跳过模型调用")
        return

    line("7. 对话 + 工具循环（真调模型）")
    from core import brain
    out = brain.ask_baymax("今天还有什么要紧的？")
    print("reply:", out["reply"])
    print("tools:", out["tools_called"], "error:", out["error"])
    out2 = brain.ask_baymax("帮我记一下，明天下午三点提醒我交周报")
    print("reply2:", out2["reply"])
    print("tools2:", out2["tools_called"])
    print("reminders now:", [(x["content"], x["next_fire_at"]) for x in scheduler.list_reminders()])

    line("8. 时间硬拦截")
    print(brain.ask_baymax("提醒我交周报")["reply"])


if __name__ == "__main__":
    main()
