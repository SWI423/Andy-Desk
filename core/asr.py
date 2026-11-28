"""
离线语音输入 ASR（Automatic Speech Recognition）
- Vosk：本地中文语音识别，不把录音发到云端
- sounddevice：读取麦克风 PCM（16kHz / 单声道 / 16bit）
- 识别结果是纯文本，交给桌面层填入输入框

设计原则：
- 模型、麦克风都按需加载；缺模型/缺依赖时抛 AsrError，由界面给出可操作提示
- 核心识别逻辑与 Qt 解耦，方便单测和以后复用到 CLI / Web
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import wave
from typing import Callable, Optional

import config


class AsrError(RuntimeError):
    """语音输入不可用或识别失败；message 直接给用户看"""


_model = None
_model_lock = threading.Lock()


def model_path() -> str:
    return os.path.abspath(config.ASR_MODEL_DIR)


def model_available() -> tuple[bool, str]:
    """返回 (是否可用, 原因)。不加载模型，启动时调用不会卡。"""
    if not config.ASR_ENABLED:
        return False, "语音输入已在配置中关闭（ASR_ENABLED=0）"
    path = model_path()
    if not os.path.isdir(path):
        return False, f"未找到离线语音模型：{path}"
    # Vosk 小模型目录通常包含 am / conf / graph / ivector
    if not os.path.isdir(os.path.join(path, "am")):
        return False, f"语音模型目录不完整：{path}"
    return True, ""


def _get_model():
    """懒加载 Vosk 模型；同一个进程只加载一次。"""
    global _model
    if _model is not None:
        return _model
    with _model_lock:
        if _model is not None:
            return _model
        ok, reason = model_available()
        if not ok:
            raise AsrError(reason)
        try:
            import vosk
        except Exception as e:
            raise AsrError("缺少 vosk 依赖，请先 pip install vosk") from e
        try:
            _model = vosk.Model(model_path())
        except Exception as e:
            raise AsrError(f"加载语音模型失败：{e}") from e
        return _model


def load_model():
    """公开入口：提前加载模型，让界面显示“准备中”。"""
    return _get_model()


def _new_recognizer(model, sample_rate: int):
    try:
        import vosk
    except Exception as e:
        raise AsrError("缺少 vosk 依赖，请先 pip install vosk") from e
    rec = vosk.KaldiRecognizer(model, sample_rate)
    try:
        rec.SetWords(False)
    except Exception:
        pass
    return rec


def _open_stream():
    try:
        import sounddevice as sd
    except Exception as e:
        raise AsrError("缺少 sounddevice 依赖，请先 pip install sounddevice") from e
    try:
        return sd.RawInputStream(
            samplerate=config.ASR_SAMPLE_RATE,
            blocksize=config.ASR_BLOCK_SIZE,
            dtype="int16",
            channels=1,
        )
    except Exception as e:
        raise AsrError(f"打不开麦克风：{e}") from e


def _read_block(stream, size: int) -> bytes:
    """兼容 sounddevice 的 (data, overflowed) 返回值和测试里的假流。"""
    data = stream.read(size)
    if isinstance(data, tuple):
        data = data[0]
    return bytes(data or b"")


def _parse_json(raw: str) -> dict:
    try:
        return json.loads(raw)
    except Exception:
        return {}


def normalize_text(text: str) -> str:
    """Vosk 中文结果会带空格；去掉汉字之间的空格，英文单词之间保留。"""
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    text = re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])", "", text)
    return text.strip()


def _join_parts(parts: list[str]) -> str:
    clean = [normalize_text(p) for p in parts]
    clean = [p for p in clean if p]
    if not clean:
        return ""
    # 长句中间通常有停顿；中文用逗号连接，英文用空格
    joined = "，".join(clean) if all(re.search(r"[\u4e00-\u9fff]", p) for p in clean) else " ".join(clean)
    return normalize_text(joined)


def record_until(stop_event: threading.Event,
                 on_partial: Optional[Callable[[str], None]] = None,
                 max_seconds: Optional[int] = None,
                 model=None,
                 stream_factory=None,
                 recognizer_factory=None) -> str:
    """
    阻塞式录音识别，直到 stop_event 被 set 或达到 max_seconds。

    参数：
    - stop_event：界面点“停止”时 set；录音循环每 0.25 秒检查一次
    - on_partial：边说边出临时结果，用于输入框实时预览
    - max_seconds：安全上限，默认 config.ASR_MAX_SECONDS
    - model/stream_factory/recognizer_factory：测试注入口，正常调用不用传

    返回：最终识别文本；没有识别到内容时返回空字符串。
    异常：AsrError 表示模型/麦克风/依赖问题；其他异常交给上层兜底。
    """
    if not config.ASR_ENABLED:
        raise AsrError("语音输入已在配置中关闭")

    model = model if model is not None else _get_model()
    rec = (recognizer_factory or _new_recognizer)(model, config.ASR_SAMPLE_RATE)
    stream = (stream_factory or _open_stream)()
    limit = max(3, int(max_seconds or config.ASR_MAX_SECONDS))
    deadline = time.monotonic() + limit
    parts: list[str] = []
    last_partial = ""

    try:
        with stream:
            while not stop_event.is_set() and time.monotonic() < deadline:
                data = _read_block(stream, config.ASR_BLOCK_SIZE)
                if not data:
                    continue
                if rec.AcceptWaveform(data):
                    text = normalize_text(_parse_json(rec.Result()).get("text", ""))
                    if text:
                        parts.append(text)
                else:
                    partial = normalize_text(_parse_json(rec.PartialResult()).get("partial", ""))
                    if partial and partial != last_partial and on_partial:
                        last_partial = partial
                        try:
                            on_partial(partial)
                        except Exception:
                            pass
    finally:
        try:
            final = normalize_text(_parse_json(rec.FinalResult()).get("text", ""))
        except Exception:
            final = ""
        if final:
            parts.append(final)

    return _join_parts(parts)


def recognize_wav(path: str, model=None, recognizer_factory=None) -> str:
    """识别 16kHz / 单声道 / 16bit WAV。用于自检或以后接音频文件。"""
    model = model if model is not None else _get_model()
    rec = (recognizer_factory or _new_recognizer)(model, config.ASR_SAMPLE_RATE)
    with wave.open(path, "rb") as wf:
        if wf.getnchannels() != 1 or wf.getsampwidth() != 2:
            raise AsrError("只支持单声道 16bit WAV")
        if wf.getframerate() != config.ASR_SAMPLE_RATE:
            raise AsrError(f"采样率必须是 {config.ASR_SAMPLE_RATE}Hz")
        parts: list[str] = []
        while True:
            data = wf.readframes(config.ASR_BLOCK_SIZE)
            if not data:
                break
            if rec.AcceptWaveform(data):
                text = normalize_text(_parse_json(rec.Result()).get("text", ""))
                if text:
                    parts.append(text)
        final = normalize_text(_parse_json(rec.FinalResult()).get("text", ""))
        if final:
            parts.append(final)
    return _join_parts(parts)


def status() -> dict:
    ok, reason = model_available()
    return {
        "enabled": config.ASR_ENABLED,
        "available": ok,
        "reason": reason,
        "model": model_path(),
        "sample_rate": config.ASR_SAMPLE_RATE,
    }


def health_check() -> bool:
    """依赖和模型目录都在，才认为语音输入可用；不真正打开麦克风。"""
    try:
        import importlib.util
        if not importlib.util.find_spec("vosk"):
            return False
        if not importlib.util.find_spec("sounddevice"):
            return False
        return model_available()[0]
    except Exception:
        return False
