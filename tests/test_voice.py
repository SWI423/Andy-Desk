"""
语音模块测试：清洗、开关、异步队列、edge-tts 失败降级
不真正播放声音，使用 _OVERRIDE 和 monkeypatch。
"""

import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core import voice


def test_clean_removes_code_and_links():
    raw = "**你好**，看这个：`print(1)`\n```\ncode\n```\n链接 https://example.com 结束。"
    out = voice.clean_tts_text(raw)
    assert "print(1)" in out
    assert "https://" not in out
    assert "**" not in out
    assert "code" not in out


def test_clean_truncates():
    out = voice.clean_tts_text("这是一句话。" * 200)
    assert len(out) <= voice.TTS_MAX_CHARS + 2
    assert out.endswith("。")


def test_disabled_speak_is_noop():
    sp = voice.Speaker(enabled=False)
    assert sp.speak("你好") is False
    sp.shutdown()


def test_speak_queue_uses_override():
    got = []
    old = voice._OVERRIDE
    voice._OVERRIDE = lambda text: got.append(text)
    sp = voice.Speaker(enabled=True)
    try:
        assert sp.speak("第一条") is True
        deadline = time.time() + 2
        while time.time() < deadline and not got:
            time.sleep(0.05)
        assert got and got[0] == "第一条"
    finally:
        sp.shutdown()
        voice._OVERRIDE = old


def test_edge_failure_falls_back_to_sapi(monkeypatch):
    sp = voice.Speaker(enabled=True)
    monkeypatch.setattr(sp, "_play_edge", lambda text: False)
    monkeypatch.setattr(sp, "_play_sapi", lambda text: True)
    assert sp._play("测试") is True
    assert sp.last_backend == "sapi"
    sp.shutdown()


def test_health_check():
    assert voice.health_check() is True
    assert "enabled" in voice.status()
