"""
模块与核心逻辑测试
- 只测纯逻辑和本地数据库（临时库），不联网、不写正式数据
"""

import os
import sys
from datetime import datetime, timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    from core import db
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "baymax_test.db"))
    monkeypatch.setattr(db, "_CONN", None)
    db.init_db()
    yield db
    if db._CONN is not None:
        db._CONN.close()
        db._CONN = None


def test_config_import():
    import config
    assert config.ZHIPU_CHAT_MODEL
    assert config.DB_PATH.endswith("baymax.db")


def test_console_import():
    import console
    assert console.logger is not None


def test_brain_import():
    from core import brain
    assert callable(brain.ask_baymax)
    assert callable(brain.needs_time_clarify)


def test_memory_import():
    from core import memory
    assert callable(memory.build_context)
    assert callable(memory.extract_facts)


def test_rag_import():
    from core import rag
    assert callable(rag.search_knowledge)


def test_tools_import():
    from core import tools
    assert isinstance(tools.TOOLS, list)
    names = {t["function"]["name"] for t in tools.TOOLS}
    assert names == set(tools.TOOL_REGISTRY)
    assert tools.health_check()


def test_prompts_import():
    import prompts
    assert "Andy" in prompts.SYSTEM_PROMPT
    assert prompts.build_system_prompt().startswith(prompts.SYSTEM_PROMPT)


def test_task_flow(tmp_db):
    from core import tasks
    r = tasks.create_task("写周报", "work", priority=2, due_at="2026-12-01 18:00")
    assert r["ok"] and r["task"]["due_at"] == "2026-12-01 18:00"

    u = tasks.update_task(r["id"], status="doing", progress=50)
    assert u["task"]["status"] == "doing" and u["task"]["progress"] == 50

    tasks.update_task(r["id"], status="done")
    assert tasks.get_task(r["id"])["progress"] == 100

    assert tasks.update_task(9999, status="done")["ok"] is False
    assert tasks.update_task(r["id"], status="oops")["ok"] is False

    kids = tasks.add_steps(r["id"], ["a", "b"])
    assert kids["count"] == 2

    doing = tasks.create_task("在做的活", "life")
    tasks.update_task(doing["id"], status="doing")
    ov = tasks.today_overview()
    assert ov["doing"] >= 1 and ov["reminders_today"] == 0


def test_domain_move(tmp_db):
    from core import tasks
    r = tasks.create_task("背单词", "study")
    tasks.update_task(r["id"], domain="life")
    assert tasks.get_task(r["id"])["domain"] == "life"
    assert tasks.update_task(r["id"], domain="bad")["ok"] is False


def test_reminder_rules():
    from core import scheduler as sch
    now = datetime(2026, 11, 25, 10, 0)
    assert sch.compute_next("daily", "07:30", after=now).day == 26
    assert sch.compute_next("daily", "21:00", after=now).day == 25
    assert sch.compute_next("weekly", "1 09:00", after=now).day == 30  # 下一个周一
    assert sch.compute_next("weekly", "周一 09:00", after=now).day == 30  # 中文写法
    assert sch.parse_expr("weekly", "周一 09:00")[0] is True
    assert sch.parse_expr("weekly", "星期八 09:00")[0] is False
    assert sch.compute_next("once", "2026-11-20 09:00", after=now) is None
    assert sch.parse_expr("daily", "25:00")[0] is False
    assert sch.parse_expr("weekly", "8 09:00")[0] is False


def test_reminder_crud(tmp_db):
    from core import scheduler as sch
    r = sch.create_reminder("喝水", "daily", "09:00")
    assert r["ok"]
    assert sch.create_reminder("错的", "daily", "99:99")["ok"] is False
    assert len(sch.list_reminders()) >= 1
    sch.set_enabled(r["id"], False)
    assert sch.set_enabled(r["id"], True)["ok"] is True
    assert sch.delete_reminder(r["id"])["ok"] is True


def test_context_assembly(tmp_db):
    from core import memory, tasks
    tasks.create_task("复习软考", "study", priority=1)
    memory.upsert_fact("name", "小林", domain="general")
    memory.log_event("user", "我最近在做软考复习，报了下个月的考试")
    memory.log_event("assistant", "记下了，软考复习是学习线的主任务。")

    ctx = memory.build_context("软考复习到哪了")
    assert ctx["stats"]["tokens"] <= 2500
    assert "小林" in ctx["text"]
    assert "今日状态" in ctx["text"]


def test_fact_dedup(tmp_db):
    from core import memory
    memory.upsert_fact("name", "小林")
    r1 = memory.upsert_fact("name", "小林")
    assert r1["action"] in ("updated", "merged")
    r2 = memory.upsert_fact("name", "小林同学")
    assert r2["action"] == "merged"
    assert len(memory.get_facts()) == 1


def test_fact_extract_boundary(tmp_db):
    from core import memory
    res = memory.extract_facts("我叫小林，晚上十点后不要提醒我学习")
    assert res and res[0]["fact"] == "小林"


def test_time_intercept():
    from core import brain
    assert brain.needs_time_clarify("提醒我交周报") is True
    assert brain.needs_time_clarify("明天下午三点提醒我交周报") is False
    assert brain.needs_time_clarify("每天 8:30 叫我起床") is False
    assert brain.needs_time_clarify("今天还有什么要紧的") is False


def test_user_bootstrap(tmp_db):
    from core.profile import ensure_first_user, get_profile_summary
    from core import memory
    r1 = ensure_first_user("Wen")
    assert r1 == {"created": True, "name": "Wen"}
    assert get_profile_summary()["user_name"] == "Wen"
    assert get_profile_summary()["assistant_name"] == "Andy"
    assert any(f["object"] == "Wen" for f in memory.get_facts())

    r2 = ensure_first_user("别人")
    assert r2["created"] is False and r2["name"] == "Wen"   # 不覆盖已有档案


def test_overview_counts(tmp_db):
    from core import tasks
    today = datetime.now().strftime("%Y-%m-%d")
    tasks.create_task("今天到期的事", "life", due_at=today)
    tasks.create_task("昨天逾期的事", "work",
                      due_at=(datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d"))
    ov = tasks.today_overview()
    assert ov["due_today"] == 1 and ov["overdue"] == 1
    txt = tasks.overview_text()
    assert "逾期" in txt
