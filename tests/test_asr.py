"""语音输入 ASR 的纯逻辑测试；不依赖真实麦克风和模型。"""

import os
import sys
import threading

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core import asr


class FakeStream:
    def __init__(self, blocks, stop_event):
        self.blocks = list(blocks)
        self.stop_event = stop_event

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, _n):
        if self.blocks:
            return self.blocks.pop(0), False
        self.stop_event.set()
        return b"", False


class FakeRecognizer:
    def __init__(self, results, partials=None, final=""):
        self.results = list(results)
        self.partials = list(partials or [])
        self.final = final
        self.i = 0

    def AcceptWaveform(self, _data):
        self.i += 1
        return self.i <= len(self.results)

    def Result(self):
        return '{"text": "%s"}' % self.results[self.i - 1]

    def PartialResult(self):
        text = self.partials.pop(0) if self.partials else ""
        return '{"partial": "%s"}' % text

    def FinalResult(self):
        return '{"text": "%s"}' % self.final


def test_normalize_removes_cjk_spaces():
    assert asr.normalize_text("你 好  世 界") == "你好世界"
    assert asr.normalize_text("Hello  world") == "Hello world"


def test_model_available_reports_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(asr.config, "ASR_ENABLED", True)
    monkeypatch.setattr(asr.config, "ASR_MODEL_DIR", str(tmp_path / "no-model"))
    ok, reason = asr.model_available()
    assert ok is False
    assert "未找到" in reason


def test_model_available_reports_incomplete(monkeypatch, tmp_path):
    path = tmp_path / "model"
    path.mkdir()
    monkeypatch.setattr(asr.config, "ASR_ENABLED", True)
    monkeypatch.setattr(asr.config, "ASR_MODEL_DIR", str(path))
    ok, reason = asr.model_available()
    assert ok is False
    assert "不完整" in reason


def test_record_until_joins_results(monkeypatch):
    stop = threading.Event()
    rec = FakeRecognizer(["第一句", "第二句"], final="")
    stream = FakeStream([b"a", b"b"], stop)
    monkeypatch.setattr(asr.config, "ASR_BLOCK_SIZE", 1)
    out = asr.record_until(
        stop,
        model=object(),
        stream_factory=lambda: stream,
        recognizer_factory=lambda _m, _r: rec,
        max_seconds=3,
    )
    assert out == "第一句，第二句"


def test_record_until_emits_partial(monkeypatch):
    stop = threading.Event()
    seen = []

    def on_partial(text):
        seen.append(text)
        stop.set()

    rec = FakeRecognizer([], partials=["你 好"], final="你 好 呀")
    stream = FakeStream([b"a"], stop)
    monkeypatch.setattr(asr.config, "ASR_BLOCK_SIZE", 1)
    out = asr.record_until(
        stop,
        on_partial=on_partial,
        model=object(),
        stream_factory=lambda: stream,
        recognizer_factory=lambda _m, _r: rec,
        max_seconds=3,
    )
    assert seen == ["你好"]
    assert out == "你好呀"


def test_record_until_respects_disabled(monkeypatch):
    monkeypatch.setattr(asr.config, "ASR_ENABLED", False)
    try:
        asr.record_until(threading.Event(), model=object(), stream_factory=lambda: None)
    except asr.AsrError as e:
        assert "关闭" in str(e)
    else:
        raise AssertionError("应当抛 AsrError")


def test_status_contains_path(monkeypatch, tmp_path):
    monkeypatch.setattr(asr.config, "ASR_ENABLED", True)
    monkeypatch.setattr(asr.config, "ASR_MODEL_DIR", str(tmp_path))
    s = asr.status()
    assert s["model"].endswith(str(tmp_path))
    assert s["sample_rate"] == 16000
