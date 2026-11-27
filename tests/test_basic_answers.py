"""基本问答校准：日期/星期/时间必须按本机时钟，不能让模型猜"""
import os
import sys
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core import brain

NOW = datetime(2026, 11, 27, 19, 50)


def test_weekday_answer_is_local():
    out = brain.answer_basic_query("今天星期几", now=NOW)
    assert out == "今天是 2026年11月27日，星期五。"
    assert "星期日" not in out


def test_date_answer_is_local():
    out = brain.answer_basic_query("今天是几号？", now=NOW)
    assert out == "今天是 2026年11月27日，星期五。"


def test_time_answer_is_local():
    out = brain.answer_basic_query("现在几点", now=NOW)
    assert "19:50" in out and "星期五" in out
    out2 = brain.answer_basic_query("现在是几点？", now=NOW)
    assert "19:50" in out2


def test_task_sentence_not_intercepted():
    assert brain.answer_basic_query("明天下午三点提醒我交周报", now=NOW) is None
    assert brain.answer_basic_query("今天星期几提醒我开会", now=NOW) is None


def test_calibrate_full_date_sentence():
    bad = "现在是 2026年11月27日 星期日 14:02。"
    out = brain.calibrate_reply_time(bad, now=NOW)
    assert out == "现在是 2026年11月27日 星期五 19:50。"


def test_calibrate_weekday_only():
    out = brain.calibrate_reply_time("今天是星期日。", now=NOW)
    assert out == "今天是星期五。"


def test_calibrate_keeps_other_date():
    out = brain.calibrate_reply_time("2026年11月28日 星期六 10:00 出发。", now=NOW)
    assert out == "2026年11月28日 星期六 10:00 出发。"


def test_calibrate_skips_conditional():
    text = "如果今天是星期日，我就休息。"
    assert brain.calibrate_reply_time(text, now=NOW) == text
