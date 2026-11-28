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


def test_split_keeps_sentences_and_limit():
    text = "第一句在这里。第二句稍微长一点！第三句是问句吗？最后一句。"
    chunks = voice.split_tts_text(text, max_len=14)
    assert chunks
    assert all(len(c) <= 14 for c in chunks)
    assert "".join(chunks) == text


def _reset():
    voice.set_language_mode("auto")
    voice.set_voice_mode("follow")


def test_pick_profile_zh_and_en(monkeypatch):
    _reset()
    monkeypatch.setattr(voice, "TTS_VOICE", "")
    zh = voice.pick_profile("你好，我是 Andy。")
    en = voice.pick_profile("Hello, I am Andy.")
    assert zh["voice"] == voice.TTS_VOICE_ZH and zh["lang"] == "zh"
    assert en["voice"] == voice.TTS_VOICE_EN and en["lang"] == "en"
    assert en["pitch"] == voice.TTS_PITCH_EN


def test_language_and_voice_are_independent():
    try:
        # 语种决定回复语言；音色跟随语种时自动选对应音色
        assert voice.set_language_mode("en-GB") == "en-GB"
        assert voice.set_voice_mode("follow") == "follow"
        assert voice.current_language_mode() == "en-GB"
        assert voice.current_voice_mode() == "follow"
        assert voice.current_language() == "en"
        assert voice.pick_profile("你好")["voice"] == voice.TTS_VOICE_EN_GB

        # 音色单独覆盖，不改变回复语种
        assert voice.set_voice_mode("zh-CN") == "zh-CN"
        assert voice.current_language_mode() == "en-GB"
        assert voice.current_voice_mode() == "zh-CN"
        assert voice.TTS_VOICE == voice.TTS_VOICE_ZH
        assert voice.pick_profile("Hello")["voice"] == voice.TTS_VOICE_ZH

        # 切语种不会偷偷改音色
        assert voice.set_language_mode("zh-TW") == "zh-TW"
        assert voice.current_voice_mode() == "zh-CN"
        assert voice.TTS_VOICE == voice.TTS_VOICE_ZH
    finally:
        _reset()


def test_legacy_voice_modes_still_map():
    try:
        assert voice.set_language_mode("zh") == "zh-CN"
        assert voice.set_language_mode("en") == "en-US"
        assert voice.set_language_mode("offline") == "auto"
        assert voice.current_voice_mode() == "sapi"
        assert voice.speaker.prefer_offline is True
        assert voice.set_voice_mode("offline") == "sapi"
        assert voice.current_language_mode() == "auto"
        assert voice.set_voice_mode("follow") == "follow"
        assert voice.speaker.prefer_offline is False
    finally:
        _reset()


def test_all_selected_voices_are_mapped():
    chosen = {"zh-CN": "zh-CN-YunxiNeural",
              "zh-TW": "zh-TW-HsiaoChenNeural",
              "en-US": "en-US-BrianMultilingualNeural",
              "en-GB": "en-GB-RyanNeural",
              "en-CA": "en-CA-LiamNeural",
              "en-HK": "en-HK-SamNeural"}
    try:
        for mode, voice_name in chosen.items():
            voice.set_voice_mode(mode)
            assert voice.current_voice_mode() == mode
            assert voice.TTS_VOICE == voice_name
            assert voice.voice_locale(mode) in ("zh", "en")
    finally:
        _reset()


def test_mismatch_warning_and_sample():
    try:
        voice.set_language_mode("zh-CN")
        voice.set_voice_mode("en-GB")
        warning = voice.mismatch_warning()
        assert "另一语言" in warning
        assert voice.sample_text().startswith("Hello")

        voice.set_language_mode("en-GB")
        voice.set_voice_mode("follow")
        assert voice.mismatch_warning() == ""
        assert voice.sample_text().startswith("Hello")

        voice.set_language_mode("auto")
        voice.set_voice_mode("en-GB")
        assert "自动" in voice.mismatch_warning()
    finally:
        _reset()


def test_stream_feed_speaks_first_sentence():
    feed = voice.StreamFeed()
    assert feed.feed("你好") == []
    out = feed.feed("，我是 Andy。今天天气不错")
    assert out and out[0].startswith("你好")
    tail = feed.feed("。")
    assert tail == []
    assert feed.flush() == ["今天天气不错。"]


def test_stream_feed_flush_tail():
    feed = voice.StreamFeed()
    out1 = feed.feed("Hello, this is Andy.")
    out2 = feed.feed(" I am here.")
    assert out1 == []
    assert out2 == ["Hello, this is Andy."]
    assert feed.flush() == ["I am here."]


def test_play_edge_synthesizes_whole_reply_once(tmp_path, monkeypatch):
    played = []
    sp = voice.Speaker(enabled=True)
    _reset()
    monkeypatch.setattr(voice, "TTS_VOICE", "")

    def fake_path(chunk, profile):
        return str(tmp_path / (voice._sha1(chunk) + ".mp3"))

    def fake_synth(chunk, path, profile):
        with open(path, "wb") as f:
            f.write(b"x" * 200)

    monkeypatch.setattr(voice, "_tts_cache_path", fake_path)
    monkeypatch.setattr(voice, "_edge_synthesize", fake_synth)
    monkeypatch.setattr(sp, "_play_file", lambda path: played.append(path) or True)

    assert sp._play_edge("第一句在这里。第二句稍微长一点！") is True
    assert len(played) == 1
    assert sp.last_backend == "edge-tts"
    assert sp.last_voice == voice.TTS_VOICE_ZH
    sp.shutdown()


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
