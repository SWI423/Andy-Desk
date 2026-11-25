"""
多轮真实对话流程测试（独立库）
用法：python scripts/chat_flow.py
验证：任务跨轮定位、拆解、长期事实、提醒、跨轮记忆召回
"""

import os
import sys
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("DATA_DIR", f"data/chatflow_{datetime.now():%H%M%S}")

TOURNS = [
    "帮我把挑战杯答辩记到学习里，12月20号截止",
    "帮我把这个任务拆成几步",
    "答辩稿那项我开始了",
    "答辩稿那项我做完了",
    "记一下，我对花生过敏",
    "明天下午两点提醒我交实验报告",
    "今天有点累",
    "晚饭吃过了",
    "先这样吧",
    "我是不是对花生过敏",
    "那个答辩的事到头来怎么样了",
]

RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond), detail))
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" | {detail}" if detail else ""))


def main():
    from core import db, brain, tasks, memory, scheduler
    db.init_db()
    memory.upsert_fact("name", "Wen")
    print("测试库:", db.DB_PATH)

    for i, text in enumerate(TOURNS, 1):
        r = brain.ask_baymax(text)
        print(f"{i:02d} 用户：{text}")
        print(f"   Andy：{r['reply'][:100]}")
        print(f"   工具：{r['tools_called']}")
        if r["error"]:
            print("   错误：", r["error"])

    all_tasks = tasks.list_tasks("study", include_done=True)
    by_title = {t["title"]: t for t in all_tasks}
    parent = next((t for t in all_tasks if "答辩" in t["title"] and t["parent_id"] is None), None)
    kids = [t for t in all_tasks if parent and t["parent_id"] == parent["id"]]
    check("任务与子步骤都在", parent and len(kids) >= 3,
          f"parent={bool(parent)} kids={len(kids)}")

    allergy = [f for f in memory.get_facts() if f["predicate"] == "allergy"]
    check("长期事实写入", any("花生" in f["object"] for f in allergy),
          str([f["object"] for f in allergy]))

    tomorrow = (datetime.now().replace(hour=0, minute=0, second=0) +
                __import__("datetime").timedelta(days=1)).strftime("%Y-%m-%d")
    rems = scheduler.list_reminders()
    check("提醒创建成功", bool(rems) and rems[0]["next_fire_at"][:10] == tomorrow,
          str([(x["content"], x["next_fire_at"]) for x in rems]))

    last = memory.last_retrieval()
    check("最后一轮发生召回", last.get("mode") not in (None, "none"), str(last))

    total = len(RESULTS)
    failed = [x for x in RESULTS if not x[1]]
    print("\n" + "=" * 50)
    print(f"流程检查 {total} 项，通过 {total - len(failed)}，失败 {len(failed)}")
    for n, _, d in failed:
        print(f"  ✗ {n} | {d}")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
