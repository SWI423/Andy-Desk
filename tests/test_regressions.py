"""
回归测试：本轮功能审计/评测暴露的缺陷
每个用例对应一个真实出现过的问题，改代码时不许再退回去
"""

import os
import sys
from datetime import datetime, timedelta

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    from core import embedding
    monkeypatch.setattr(embedding, "_OVERRIDE", lambda texts: None)


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    from core import db, memory
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "reg.db"))
    monkeypatch.setattr(db, "_CONN", None)
    monkeypatch.setattr(memory, "_VEC_CACHE", {"key": None, "ids": None, "mat": None,
                                               "mean": None, "centered": False})
    db.init_db()
    yield db
    if db._CONN is not None:
        db._CONN.close()
        db._CONN = None


def test_fact_extract_allergy_prefix(tmp_db):
    """'我对花生过敏' 曾经抽出空对象（只取关键词后面的字）"""
    from core import memory
    memory.extract_facts("我对花生过敏")
    facts = [f for f in memory.get_facts() if f["predicate"] == "allergy"]
    assert facts and facts[0]["object"] == "花生"


def test_fact_extract_after_filler(tmp_db):
    """'记一下，我对花生过敏' 曾经抽成 '记一下'"""
    from core import memory
    memory.extract_facts("记一下，我对花生过敏")
    facts = [f for f in memory.get_facts() if f["predicate"] == "allergy"]
    assert facts and facts[0]["object"] == "花生"


def test_question_not_written_as_fact(tmp_db):
    """'我是不是对花生过敏' 是查询，不能反写成一条新事实"""
    from core import memory
    assert memory.extract_facts("我是不是对花生过敏") == []
    assert not memory.get_facts()


def test_claim_detection():
    from core import brain
    assert brain.claims_done("已设置提醒：明天下午两点")
    assert brain.claims_done("已记下你对花生过敏")
    assert brain.claims_done("已标记为完成")
    assert brain.claims_done("好的，我每天 8:30 叫你起床")
    assert not brain.claims_done("要不要我每天叫你起床？")
    assert not brain.claims_done("要不要我帮你安排个放松的任务？")
    assert not brain.claims_done("听起来你今天挺累的")


def test_force_tool_for_intents():
    from core import brain
    assert brain.force_tool_for("明天下午两点提醒我交实验报告") == "reminder_create"
    assert brain.force_tool_for("答辩稿那项我做完了") == "task_update"
    assert brain.force_tool_for("那个答辩的事怎么样了") == "task_list"
    assert brain.force_tool_for("记一下，我对花生过敏") is None


def test_intent_boost_prefers_health_and_allergy():
    from core import memory
    assert memory._intent_boost("今天状态怎么样", "今天好累，不想动") > 0
    assert memory._intent_boost("今天状态怎么样", "今天天气不错") == 0
    allergy = memory._intent_boost("中午吃饭要注意什么", "我对花生过敏，食堂别打带花生的菜")
    generic = memory._intent_boost("中午吃饭要注意什么", "晚上吃什么还没想好")
    assert allergy > generic


def test_fake_claim_is_caught_and_retried(tmp_db, monkeypatch):
    """历史带偏时模型只报喜不落库：必须重试并真正写进去"""
    import json
    from core import brain, scheduler

    rounds = []

    def fake(messages, on_chunk=None, tool_choice="auto", **kwargs):
        rounds.append(tool_choice)
        if len(rounds) == 1:
            return "好的，每天早上8:30叫你起床", []
        if len(rounds) == 2:
            return "", [{"id": "", "name": "reminder_create",
                         "arguments": json.dumps({"content": "交实验报告", "rule_type": "once",
                                                   "rule_expr": "2099-01-01 14:00"})}]
        return "提醒设好了，明天下午两点喊你。", []

    monkeypatch.setattr(brain, "_stream_once", fake)
    r = brain.ask_baymax("明天下午两点提醒我交实验报告")
    assert r["tools_called"] == ["reminder_create"]
    assert len(scheduler.list_reminders()) == 1
    assert "没写进系统" not in r["reply"]
    assert rounds[0]["function"]["name"] == "reminder_create"


def test_fake_claim_without_tool_is_not_believed(tmp_db, monkeypatch):
    from core import brain
    monkeypatch.setattr(brain, "_stream_once",
                        lambda messages, on_chunk=None, tool_choice="auto", **kwargs:
                        ("已记下你的偏好", []))
    r = brain.ask_baymax("记住我下周去北京出差")
    assert "没写进系统" in r["reply"] or "再说一次" in r["reply"]
    assert "已记下你的偏好" not in r["reply"]


def test_fact_extract_allergy_with_punctuation(tmp_db):
    from core import memory
    memory.extract_facts("我对青霉素过敏，别给我开这个药")
    facts = [f for f in memory.get_facts() if f["predicate"] == "allergy"]
    assert facts and facts[0]["object"].startswith("青霉素")


def test_name_never_crowded_out(tmp_db):
    """12 条偏好把事实位占满时，称呼仍要在上下文里"""
    from core import memory
    memory.upsert_fact("name", "Wen")
    for i in range(14):
        memory.upsert_fact("preference", f"偏好{i}")
    ctx = memory.build_context("随便聊聊")
    assert "称呼：Wen" in ctx["text"]


def test_memory_save_tool_object_field(tmp_db):
    """schema 字段叫 object，函数参数曾写成 obj，导致长期记忆写不进去"""
    from core import tools
    r = tools.call_tool("memory_save", {"predicate": "preference", "object": "喜欢早起"})
    assert r["ok"] is True
    assert any(f["object"] == "喜欢早起" for f in tools.memory.get_facts())


def test_task_duplicate_not_created(tmp_db):
    from core import tasks
    a = tasks.create_task("买牛奶", "life")
    b = tasks.create_task("买牛奶", "life")
    assert b.get("duplicate") is True and b["id"] == a["id"]
    assert len(tasks.list_tasks("life")) == 1


def test_task_due_warning(tmp_db):
    from core import tasks
    r = tasks.create_task("模糊截止", "work", due_at="下周")
    assert r["ok"] and r["due_warning"]


def test_steps_cap_and_no_due_inherit(tmp_db):
    from core import tasks
    p = tasks.create_task("毕业论文", "study", due_at="2026-12-20")
    r = tasks.add_steps(p["id"], [f"步{i}" for i in range(10)])
    assert r["count"] == 6
    assert all(s["due_at"] is None for s in r["steps"])


def test_ambiguous_title_reports_candidates(tmp_db):
    """模糊标题匹配到多条时不能闷头改第一条"""
    from core import tasks, tools
    tasks.create_task("准备挑战杯答辩", "study")
    tasks.create_task("准备挑战杯答辩稿", "study")
    r = tools.call_tool("task_update", {"title": "挑战杯答辩", "status": "done"})
    assert r["ok"] is False and len(r["candidates"]) == 2
    assert all(t["status"] != "done" for t in tasks.list_tasks("study"))


def test_progress_auto_starts_task(tmp_db):
    """只报进度不报状态时，待办应自动转进行中"""
    from core import tasks
    t = tasks.create_task("算法题", "study")
    tasks.update_task(t["id"], progress=50)
    got = tasks.get_task(t["id"])
    assert got["progress"] == 50 and got["status"] == "doing"


def test_reminder_missed_count_same_day(tmp_db):
    """错过 2 小时的每日提醒，旧算法按天数差算成 0 次"""
    from core import scheduler, db
    rid = db.execute(
        """INSERT INTO reminders(content, rule_type, rule_expr, next_fire_at, enabled, created_at)
           VALUES(?,?,?,?,1,?)""",
        ("每日事", "daily", "09:00",
         (datetime.now() - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S"), db.now_iso()))
    fired = []
    scheduler.Scheduler(on_fire=lambda p: fired.append(p))._catch_up()
    row = db.query_one("SELECT missed_count FROM reminders WHERE id=?", (rid,))
    assert row["missed_count"] >= 1


def test_catch_up_text_has_no_duplicate_prefix(tmp_db):
    from core import scheduler, db
    db.execute(
        """INSERT INTO reminders(content, rule_type, rule_expr, next_fire_at, enabled, created_at)
           VALUES(?,?,?,?,1,?)""",
        ("迟到", "once", "2026-01-01 09:00",
         (datetime.now() - timedelta(minutes=3)).strftime("%Y-%m-%d %H:%M:%S"), db.now_iso()))
    fired = []
    scheduler.Scheduler(on_fire=lambda p: fired.append(p))._catch_up()
    assert "启动时补发" not in fired[-1]["content"]


def test_date_only_reminder_needs_clock():
    from core import brain
    assert brain.needs_time_clarify("明天提醒我交周报") is True
    assert brain.needs_time_clarify("明天下午三点提醒我交周报") is False
    assert brain.needs_time_clarify("每天提醒我喝水") is True


def _fake_vec(text):
    from core import embedding
    mapping = {
        "查询": [1.0, 0.0],
        "最近的高分": [1.0, 0.0],
        "老目标": [0.9, 0.43589],
    }
    return embedding._normalize(mapping.get(text, [0.0, 1.0]))


def test_best_score_ignores_recent_events(tmp_db, monkeypatch):
    """
    最近事件不参与召回，但曾经仍参与 best 计算，
    结果 cutoff 被抬到 0.94，0.9 的老目标被误过滤
    """
    import config
    from core import memory, embedding
    monkeypatch.setattr(config, "EMBED_ENABLED", True)
    monkeypatch.setattr(config, "EMBED_MIN_CORPUS", 8)   # 小语料 -> 不做去均值
    embedding.set_embedder(lambda texts: [_fake_vec(t) for t in texts])

    memory.log_event("user", "老目标")
    memory.log_event("user", "最近的高分")
    memory.backfill_vectors(limit=10)
    res = memory.search_events("查询", exclude_recent=1)
    assert res and "老目标" in res[0]["content"]


def test_small_corpus_still_recalls(tmp_db, monkeypatch):
    """只有 3 条语料时，去均值会把同向向量抹成 0，导致完全召回不到"""
    import config
    from core import memory, embedding
    monkeypatch.setattr(config, "EMBED_ENABLED", True)
    monkeypatch.setattr(config, "EMBED_MIN_CORPUS", 8)
    embedding.set_embedder(lambda texts: [np.array([1.0, 0.0], dtype="float32")
                                          for _ in texts])
    memory.log_event("user", "我对花生过敏")
    memory.log_event("user", "周三交证")
    memory.backfill_vectors(limit=10)
    res = memory.search_events("花生能吃吗", exclude_recent=0)
    assert res, "小语料下不应该召回为空"


def test_assistant_echo_not_recalled(tmp_db, monkeypatch):
    """助理复述用户原话会抢排名，检索池应只留用户侧"""
    import config
    from core import memory, embedding
    monkeypatch.setattr(config, "EMBED_ENABLED", True)
    embedding.set_embedder(lambda texts: [np.array([1.0, 0.0], dtype="float32")
                                          for _ in texts])
    memory.log_event("user", "我对芒果过敏，别买芒果")
    memory.log_event("assistant", "记住你对芒果过敏，以后不买芒果")
    memory.backfill_vectors(limit=10)
    res = memory.search_events("我是不是对芒果过敏", exclude_recent=0)
    assert res and res[0]["role"] == "user"
    assert len([r for r in res if "芒果" in r["content"]]) == 1


def test_tool_call_ids_align_with_assistant_message():
    from core import brain
    calls = [{"id": "", "name": "task_list", "arguments": "{}"},
             {"id": "", "name": "overview_today", "arguments": "{}"}]
    msg = brain._assistant_tool_msg("", calls)
    assert [c["id"] for c in msg["tool_calls"]] == ["call_0", "call_1"]
