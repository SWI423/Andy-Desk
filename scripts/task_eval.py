"""
任务链路真实模型评测（独立库，逐场景隔离）
用法：python scripts/task_eval.py
- 每个场景一个全新库，避免上下文串味
- 真实调用 glm-4-flash，检查工具是否被调用、数据库是否落到期望状态
- 输出 PASS/FAIL 与回复摘要；失败项最后汇总
"""

import os
import sys
import tempfile
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ["EMBED_ENABLED"] = "0"          # 任务链路不需要向量，省调用
os.environ.setdefault("DATA_DIR", f"data/taskeval_{datetime.now():%H%M%S}")

RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond), detail))
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" | {detail}" if detail else ""))


def fresh_db(tag):
    from core import db, memory
    if db._CONN is not None:
        db._CONN.close()
        db._CONN = None
    db.DB_PATH = os.path.join(tempfile.mkdtemp(prefix=f"taskeval_{tag}_"), "baymax.db")
    memory._VEC_CACHE.update({"key": None, "ids": None, "mat": None, "mean": None,
                              "centered": False})
    db.init_db()
    memory.upsert_fact("name", "Wen")
    return db


def ask(text):
    from core import brain
    r = brain.ask_baymax(text)
    print(f"  用户：{text}")
    print(f"  Andy：{(r['reply'] or '')[:90]}")
    print(f"  工具：{r['tools_called']} | 错误：{r['error']}")
    return r


def run():
    from core import tasks, scheduler, db
    tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")

    print("\n=== 场景 1：建学习任务 + 截止日期 ===")
    fresh_db("s1")
    r = ask("帮我把软考冲刺记到学习里，12月5号截止")
    found = [t for t in tasks.list_tasks("study", include_done=True) if "软考" in t["title"]]
    check("建学习任务", "task_create" in r["tools_called"] and found, str(found[:1]))
    check("截止日期落到 12-05", found and (found[0]["due_at"] or "")[:10].endswith("12-05"),
          found[0]["due_at"] if found else "")

    print("\n=== 场景 2：建工作任务（相对时间） ===")
    fresh_db("s2")
    r = ask("明天要交周报，帮我记一下")
    found = [t for t in tasks.list_tasks("work") if "周报" in t["title"]]
    check("建工作任务且截止=明天", found and (found[0]["due_at"] or "")[:10] == tomorrow,
          f"due={found[0]['due_at'] if found else None} 期望 {tomorrow}")

    print("\n=== 场景 3：生活任务 ===")
    fresh_db("s3")
    r = ask("记得买牛奶")
    found = [t for t in tasks.list_tasks("life") if "牛奶" in t["title"]]
    check("生活线建任务", "task_create" in r["tools_called"] and found, str(found[:1]))

    print("\n=== 场景 4：问今天学习 ===")
    fresh_db("s4")
    tasks.create_task("准备软考", "study", priority=1)
    r = ask("今天学习上有什么要做的")
    check("查询任务而非编造", "task_list" in r["tools_called"] and "软考" in r["reply"],
          r["reply"][:60])

    print("\n=== 场景 5：按标题置完成 ===")
    fresh_db("s5")
    t = tasks.create_task("模电实验报告", "study")["id"]
    r = ask("模电实验报告我做完了")
    check("按标题定位并置 done", tasks.get_task(t)["status"] == "done",
          f"status={tasks.get_task(t)['status']} tools={r['tools_called']}")

    print("\n=== 场景 6：开始做 ===")
    fresh_db("s6")
    t = tasks.create_task("写周报", "work")["id"]
    r = ask("周报我开始写了")
    got = tasks.get_task(t)
    check("置进行中", got["status"] == "doing", f"status={got['status']}")

    print("\n=== 场景 7：进度一半 ===")
    fresh_db("s7")
    t = tasks.create_task("算法题", "study")["id"]
    r = ask("算法题做到一半了")
    got = tasks.get_task(t)
    check("进度=50 且进行中", got["progress"] == 50 and got["status"] == "doing",
          f"progress={got['progress']} status={got['status']}")

    print("\n=== 场景 8：拆解任务 ===")
    fresh_db("s8")
    t = tasks.create_task("准备挑战杯答辩", "study", due_at="2026-12-20")["id"]
    r = ask("帮我把挑战杯答辩拆成几步")
    kids = db.query("SELECT * FROM tasks WHERE parent_id=?", (t,))
    check("拆出 3-6 步且不继承截止",
          "task_breakdown" in r["tools_called"] and 3 <= len(kids) <= 6
          and all(k["due_at"] is None for k in kids),
          f"steps={len(kids)} due={[k['due_at'] for k in kids]}")

    print("\n=== 场景 9：带钟点的一次性提醒 ===")
    fresh_db("s9")
    r = ask("明天下午三点提醒我交周报")
    rems = scheduler.list_reminders()
    ok = rems and rems[0]["next_fire_at"][:10] == tomorrow and rems[0]["next_fire_at"][11:16] == "15:00"
    check("提醒落到明天 15:00", ok,
          f"{[(x['content'], x['next_fire_at']) for x in rems]}")

    print("\n=== 场景 10：只有日期没有钟点 ===")
    fresh_db("s10")
    r = ask("明天提醒我交周报")
    rems = scheduler.list_reminders()
    check("追问钟点且不建提醒", not rems and ("几点" in r["reply"]),
          f"提醒数={len(rems)} 回复={r['reply'][:40]}")

    print("\n=== 场景 11：每日提醒 ===")
    fresh_db("s11")
    r = ask("每天 8:30 叫我起床")
    rems = scheduler.list_reminders()
    check("每日 08:30", rems and rems[0]["rule_type"] == "daily",
          f"{[(x['rule_type'], x['rule_expr']) for x in rems]}")

    print("\n=== 场景 12：重复建任务 ===")
    fresh_db("s12")
    tasks.create_task("买牛奶", "life")
    r = ask("再记一次买牛奶")
    n = db.query_one("SELECT COUNT(*) AS c FROM tasks WHERE title='买牛奶'")["c"]
    check("不产生重复记录", n == 1, f"条数={n} 回复={r['reply'][:40]}")

    print("\n=== 场景 13：只给别名，按语义定位 ===")
    fresh_db("s13")
    t = tasks.create_task("准备挑战杯答辩稿", "study")["id"]
    r = ask("那个答辩的活儿我开始了")
    got = tasks.get_task(t)
    check("模糊标题能定位（允许反问）",
          got["status"] == "doing" or "哪个" in r["reply"] or "编号" in r["reply"],
          f"status={got['status']} 回复={r['reply'][:50]}")

    print("\n=== 场景 14：长期偏好不该变成提醒 ===")
    fresh_db("s14")
    r = ask("以后晚上十点后别提醒我学习")
    check("不建提醒", not scheduler.list_reminders(), f"提醒数={len(scheduler.list_reminders())}")

    print("\n=== 场景 15：身份问题 ===")
    fresh_db("s15")
    r = ask("我是谁")
    check("回答 Wen", "Wen" in r["reply"], r["reply"][:50])

    print("\n=== 场景 16：过去时间设提醒 ===")
    fresh_db("s16")
    past = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d %H:%M")
    r = ask(f"{past} 提醒我交材料")
    rems = scheduler.list_reminders()
    check("过去时间不建提醒", not rems, f"提醒数={len(rems)} 回复={r['reply'][:50]}")

    print("\n=== 场景 17：一句话两件事 ===")
    fresh_db("s17")
    r = ask("记两件事：交实验报告，明天买牛奶")
    found = db.query("SELECT title FROM tasks")
    titles = [x["title"] for x in found]
    check("两件事都记下", len(titles) >= 2, str(titles))

    print("\n=== 场景 18：查已设提醒 ===")
    fresh_db("s18")
    scheduler.create_reminder("喝水", "daily", "09:00")
    r = ask("我设了哪些提醒")
    check("调用提醒查询并答出内容",
          "reminder_list" in r["tools_called"] and "喝水" in r["reply"],
          r["reply"][:50])

    total = len(RESULTS)
    failed = [x for x in RESULTS if not x[1]]
    print("\n" + "=" * 50)
    print(f"合计 {total} 项，通过 {total - len(failed)}，失败 {len(failed)}")
    for name, _, detail in failed:
        print(f"  ✗ {name} | {detail}")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(run())
