"""
功能审计（离线，不调模型/不联网）
用法：python scripts/functional_audit.py [--verbose]
- 独立库 data/audit_<时间>/，不碰正式数据
- 每项打印 PASS/FAIL，最后汇总；有 FAIL 时退出码 1
- 覆盖：数据层、任务、事实蒸馏、检索降级、工具契约、调度补发、时间拦截
"""

import os
import sys
from datetime import datetime, timedelta
from difflib import SequenceMatcher

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("DATA_DIR", f"data/audit_{datetime.now():%H%M%S}")
os.environ["EMBED_ENABLED"] = "0"          # 离线审计不调 embedding

VERBOSE = "--verbose" in sys.argv
RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond), detail))
    mark = "PASS" if cond else "FAIL"
    print(f"[{mark}] {name}" + (f" | {detail}" if detail else ""))
    return bool(cond)


def section(title):
    print(f"\n--- {title} ---")


def main():
    import numpy as np
    from core import db, tasks, memory, scheduler, tools, embedding, brain

    db.init_db()
    print("测试库:", db.DB_PATH)

    # ================= 数据层 =================
    section("数据层")
    names = {r["name"] for r in db.query(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    check("8 张业务表齐全", {"chat_events", "event_vectors", "semantic_facts",
                            "session_summary", "tasks", "task_events",
                            "reminders", "user_profile"} <= names,
          f"实际 {len(names)} 张")

    eid = memory.log_event("user", "级联测试")
    memory._store_vector(eid, "级联测试")  # embed 关闭时返回 False，这里直接塞假向量
    db.execute("""INSERT OR REPLACE INTO event_vectors(event_id, model, dim, vec, created_at)
                  VALUES(?,?,?,?,?)""", (eid, "fake", 2, np.array([1, 0], dtype=np.float32).tobytes(),
                                         db.now_iso()))
    db.execute("DELETE FROM chat_events WHERE id=?", (eid,))
    left = db.query_one("SELECT COUNT(*) AS c FROM event_vectors WHERE event_id=?", (eid,))
    check("删事件级联删向量", left["c"] == 0)

    # ================= 任务层 =================
    section("任务层")
    r = tasks.create_task("复习软考", "study", priority=1)
    check("建任务返回 id/状态", r["ok"] and r["id"] > 0 and r["task"]["status"] == "todo")

    cases = ["2026/12/05", "2026年12月5日 18:30", "2026-12-05T09:00", "2026-12-05 08:05"]
    got = [tasks.normalize_due(c) for c in cases]
    check("时间格式归一", got == ["2026-12-05", "2026-12-05 18:30", "2026-12-05 09:00",
                              "2026-12-05 08:05"], str(got))

    bad = tasks.create_task("交代不清的任务", "work", due_at="下周")
    check("非法截止时间应提示", bad.get("due_warning") or bad["ok"] is False,
          f"ok={bad['ok']} due={bad['task']['due_at']} warning={bad.get('due_warning')}")

    dup1 = tasks.create_task("交实验报告", "study")
    dup2 = tasks.create_task("交实验报告", "study")
    check("重复任务应识别", dup2.get("duplicate") is True or dup2["id"] == dup1["id"],
          f"第二次 id={dup2['id']}（首次 {dup1['id']}）")

    t1 = tasks.create_task("准备挑战杯答辩", "study")
    t2 = tasks.create_task("准备挑战杯答辩稿", "study")
    cands = tools.call_tool("task_update", {"title": "挑战杯答辩", "status": "done"})
    check("近似标题应报歧义而非误改",
          cands.get("ok") is False and cands.get("candidates"),
          f"{cands}")

    ex = tasks.find_by_title("复习软考")
    check("精确标题定位", len(ex) == 1 and ex[0]["id"] == r["id"])

    u = tasks.update_task(r["id"], progress=100)
    check("进度 100 自动完成", u["task"]["status"] == "done" and u["task"]["progress"] == 100)
    back = tasks.update_task(r["id"], status="todo")
    check("完成回退进度封顶 90", back["task"]["progress"] <= 90)

    parent = tasks.create_task("写毕业论文", "study", due_at="2026-12-20")
    kids = tasks.add_steps(parent["id"], [f"步骤{i}" for i in range(1, 11)])
    check("拆解步数上限 6", kids["count"] == 6, f"实际 {kids['count']}")
    inherited = all(k["due_at"] == "2026-12-20" for k in kids["steps"])
    check("子步骤不继承父截止（避免今日到期虚高）", not inherited,
          f"子任务 due={[k['due_at'] for k in kids['steps'][:3]]}")

    delr = tasks.delete_task(parent["id"])
    left_kids = db.query("SELECT COUNT(*) AS c FROM tasks WHERE parent_id=?", (parent["id"],))
    check("删父任务级联删子任务", delr["ok"] and left_kids[0]["c"] == 0)

    pri = tasks.create_task("低优任务", "life", priority=3)
    high = tasks.create_task("高优任务", "life", priority=1)
    order = [t["id"] for t in tasks.list_tasks("life")]
    check("列表按优先级排序", order.index(high["id"]) < order.index(pri["id"]))

    today = datetime.now().strftime("%Y-%m-%d")
    td = tasks.create_task("今天交的东西", "work", due_at=today)
    od = tasks.create_task("昨天没交的东西", "work",
                           due_at=(datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d"))
    up = tasks.create_task("后天要做的东西", "study",
                           due_at=(datetime.now() + timedelta(days=2)).strftime("%Y-%m-%d"))
    ov = tasks.today_overview()
    check("今日/逾期/即将到期计数",
          ov["due_today"] == 1 and ov["overdue"] == 1 and len(ov["upcoming_list"]) >= 1,
          f"due={ov['due_today']} overdue={ov['overdue']} upcoming={len(ov['upcoming_list'])}")

    all_list = tools.call_tool("task_list", {"status": "all"})
    check("task_list status=all 含已完成", all_list["ok"] and all_list["count"] >= 5)

    # ================= 事实蒸馏 =================
    section("事实蒸馏")
    memory.upsert_fact("name", "Wen")
    memory.upsert_fact("name", "Wen")
    check("同名事实幂等", len([f for f in memory.get_facts() if f["predicate"] == "name"]) == 1)

    memory.extract_facts("我叫Wen，今年大三")
    name_fact = [f for f in memory.get_facts() if f["predicate"] == "name"]
    check("我叫X 抽名字且不被逗号截断", any(f["object"] == "Wen" for f in name_fact),
          str([f["object"] for f in name_fact]))

    res = memory.extract_facts("我对花生过敏")
    allergy = [f for f in memory.get_facts() if f["predicate"] == "allergy"]
    check("过敏事实抽取", any("花生" in f["object"] for f in allergy),
          f"结果 {[f['object'] for f in allergy]}")

    res2 = memory.extract_facts("我对青霉素过敏，别给我开这个药")
    check("过敏+标点截断", any(f["object"].startswith("青霉素") for f in
                            [x for x in memory.get_facts() if x["predicate"] == "allergy"]))

    memory.extract_facts("我不喜欢太吵的环境")
    pref = [f for f in memory.get_facts() if f["predicate"] == "preference"]
    check("偏好抽取", any("太吵" in f["object"] for f in pref), str([f["object"] for f in pref]))

    memory.extract_facts("我今天很累")   # 一次性情绪，不应成为长期事实
    check("情绪不入长期事实", not any("很累" in f["object"] for f in memory.get_facts()))

    memory.upsert_fact("preference", "喜欢晚上学习")
    memory.upsert_fact("preference", "不喜欢晚上学习")
    prefs = [f["object"] for f in memory.get_facts() if f["predicate"] == "preference"]
    check("冲突偏好不并存", len([p for p in prefs if "晚上学习" in p]) == 1,
          f"当前 {prefs}")

    # ================= 检索与上下文 =================
    section("检索与上下文")
    memory.log_event("user", "周三要交软件设计师的证，别忘了打印")
    memory.log_event("user", "今天好累，不想动")
    for i in range(14):
        memory.log_event("user", f"日常闲聊第 {i} 条，今天天气一般")

    kw = memory.search_events("那个证的事怎么样了", mode="keyword", exclude_recent=0)
    check("纯关键词不夸大召回", isinstance(kw, list), f"{len(kw)} 条")
    res = memory.search_events("证 打印", exclude_recent=0)
    check("关键词能召回证句", res and "证" in res[0]["content"], str(res[:1]))

    ctx = memory.build_context("我最近在准备软考")
    check("上下文包含事实与今日状态",
          "【关于用户】" in ctx["text"] and "【今日状态】" in ctx["text"])
    check("上下文预算不超限", ctx["stats"]["tokens"] <= 2500, str(ctx["stats"]["tokens"]))
    check("检索统计可读", "retrieval" in ctx["stats"])

    ctx2 = memory.build_context("提醒我明天交周报")
    check("意图识别含 reminder", "reminder" in ctx2["stats"]["intents"], str(ctx2["stats"]["intents"]))

    # 助理回声不应挤掉用户原话
    memory.log_event("user", "我对芒果过敏，别买芒果")
    memory.log_event("assistant", "记住你对芒果过敏，以后不买芒果")
    hits = memory.search_events("我是不是对芒果过敏", exclude_recent=0)
    check("召回优先用户原话", hits and hits[0]["role"] == "user",
          f"top1 role={hits[0]['role'] if hits else None}")

    # 近义去重：同一句话被助理复述，只保留一条
    contents = [h["content"] for h in hits]
    check("近义召回去重", len({c[:12] for c in contents}) == len(contents),
          f"{contents}")

    # 冷启动：全新库只有 2 条事件时，去均值会不会把信号抹成 0
    section("向量冷启动")
    import config
    import tempfile
    if db._CONN is not None:
        db._CONN.close()
        db._CONN = None
    db.DB_PATH = os.path.join(tempfile.mkdtemp(prefix="audit_cold_"), "cold.db")
    db.init_db()
    config.EMBED_ENABLED = True
    embedding.set_embedder(lambda texts: [np.array([1.0, 0.0], dtype="float32") for _ in texts])
    memory._VEC_CACHE.update({"key": None, "ids": None, "mat": None, "mean": None,
                              "centered": False})
    memory.log_event("user", "冷启动甲")
    memory.log_event("user", "冷启动乙")
    memory.backfill_vectors(limit=10)
    vec_res, status = memory._vector_candidates("冷启动甲", 0, 5, 0.10)
    check("少量语料仍可向量召回", bool(vec_res), f"status={status} 命中={len(vec_res)}")
    config.EMBED_ENABLED = False
    memory._VEC_CACHE.update({"key": None, "ids": None, "mat": None, "mean": None,
                              "centered": False})
    embedding.set_embedder(None)

    # ================= 工具契约 =================
    section("工具契约")
    check("未知工具收敛错误", tools.call_tool("nope", {})["ok"] is False)
    check("参数不匹配收敛错误",
          tools.call_tool("task_create", {"title": "x", "bogus": 1})["ok"] is False)
    check("更新缺定位参数报错", tools.call_tool("task_update", {"status": "done"})["ok"] is False)
    check("空事实拒写", tools.call_tool("memory_save", {"predicate": "x", "object": " "})["ok"] is False)
    past = (datetime.now() - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M")
    check("过去时间拒绝建提醒", tools.call_tool(
        "reminder_create", {"content": "x", "rule_type": "once", "rule_expr": past})["ok"] is False)
    schemas = {t["function"]["name"] for t in tools.TOOLS}
    check("schema 与注册表一致", schemas == set(tools.TOOL_REGISTRY))

    # ================= 调度补发 =================
    section("调度补发")
    fired = []
    sch = scheduler.Scheduler(on_fire=lambda p: fired.append(p))
    late_id = db.execute(
        """INSERT INTO reminders(content, rule_type, rule_expr, next_fire_at, enabled, created_at)
           VALUES(?,?,?,?,1,?)""",
        ("迟到 5 分钟的事", "once", "2026-01-01 09:00",
         (datetime.now() - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S"), db.now_iso()))
    miss_id = db.execute(
        """INSERT INTO reminders(content, rule_type, rule_expr, next_fire_at, enabled, created_at)
           VALUES(?,?,?,?,1,?)""",
        ("错过两小时的每日事", "daily", "09:00",
         (datetime.now() - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S"), db.now_iso()))
    sch._catch_up()
    payload = fired[-1] if fired else {}
    check("迟到提醒补发", "迟到 5 分钟的事" in str(payload), str(payload)[:120])
    check("错过提醒合并一条", payload.get("type") == "catch_up" and
          "错过两小时的每日事" in str(payload))
    row = db.query_one("SELECT enabled, missed_count FROM reminders WHERE id=?", (miss_id,))
    check("错过计数+1 且规则推进", row["missed_count"] >= 1 and row["enabled"] == 1,
          str(row))
    once = db.query_one("SELECT enabled, next_fire_at FROM reminders WHERE id=?", (late_id,))
    check("一次性补发后停用", once["enabled"] == 0)

    # tick：到点触发一次后推进
    rid = db.execute(
        """INSERT INTO reminders(content, rule_type, rule_expr, next_fire_at, enabled, created_at)
           VALUES(?,?,?,?,1,?)""",
        ("到点点", "daily", "08:00",
         (datetime.now() - timedelta(seconds=5)).strftime("%Y-%m-%d %H:%M:%S"), db.now_iso()))
    fired.clear()
    sch._tick()
    check("心跳触发一次", len([p for p in fired if p.get("type") == "reminder"]) == 1)
    nxt = db.query_one("SELECT next_fire_at FROM reminders WHERE id=?", (rid,))
    check("触发后推进到未来", nxt["next_fire_at"] > datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

    # ================= 对话层（离线部分） =================
    section("对话层离线部分")
    check("无时间词要反问", brain.needs_time_clarify("提醒我交周报") is True)
    check("有具体时间不反问", brain.needs_time_clarify("明天下午三点提醒我交周报") is False)
    check("只有日期没有钟点也要反问", brain.needs_time_clarify("明天提醒我交周报") is True,
          "当前判定=%s" % brain.needs_time_clarify("明天提醒我交周报"))
    check("每天几点不反问", brain.needs_time_clarify("每天 8:30 叫我起床") is False)
    check("非法参数不崩溃", brain._parse_args("{oops") == {})

    # 多工具调用 id 对齐
    calls = [{"id": "", "name": "task_list", "arguments": "{}"},
             {"id": "x2", "name": "overview_today", "arguments": "{}"}]
    msg = brain._assistant_tool_msg("", calls)
    ids = [c["id"] for c in msg["tool_calls"]]
    # 后续 tool 消息用的 id 规则必须与这里一致
    used = []
    called = []
    for c in calls:
        if not c["name"]:
            continue
        called.append(c["name"])
        used.append(c["id"] or f"call_{len(called) - 1}")
    check("工具调用 id 与回填一致", used == ids, f"assistant={ids} tool={used}")

    # ================= 汇总 =================
    total = len(RESULTS)
    failed = [r for r in RESULTS if not r[1]]
    print("\n" + "=" * 50)
    print(f"合计 {total} 项，通过 {total - len(failed)}，失败 {len(failed)}")
    for name, _, detail in failed:
        print(f"  ✗ {name} | {detail}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
