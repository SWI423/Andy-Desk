"""
语音播报 voice.py
- 主用 edge-tts（在线，音色自然）；失败自动退回 Windows SAPI（离线）
- 独立工作线程 + 队列，绝不阻塞对话和窗口
- 播报前清洗 Markdown / 代码块 / 链接，长回复截断到 TTS_MAX_CHARS
- 开关由界面复选框或 TTS_ENABLED 控制；关闭时 speak() 直接返回 False
"""

import asyncio
import hashlib
import os
import queue
import re
import threading
import time
from typing import Optional

from config import (LOG_DIR, TTS_ENABLED, TTS_MAX_CHARS, TTS_RATE,
                    TTS_SAPI_RATE, TTS_SAPI_VOLUME, TTS_VOICE, TTS_VOLUME)
from console import logger

os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")

# 测试注入：设置后 _play 直接调用它，不碰真实音频
_OVERRIDE = None

_CODE_BLOCK = re.compile(r"```.*?```", re.S)
_INLINE_CODE = re.compile(r"`([^`]*)`")
_MD_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_URL = re.compile(r"https?://\S+")
_HTML_TAG = re.compile(r"<[^>]+>")
_MD_SYMBOL = re.compile(r"[*_#>~|]")
_EMOJI = re.compile(
    "[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F000-\U0001F2FF]")
_SPACE = re.compile(r"[ \t]+")
_BLANK = re.compile(r"\n{2,}")


def clean_tts_text(text: str) -> str:
    """把给眼睛看的内容改成给耳朵听的：去代码、去链接、去符号、截断"""
    text = str(text or "")
    text = _CODE_BLOCK.sub(" 代码略过。 ", text)
    text = _INLINE_CODE.sub(r"\1", text)
    text = _MD_LINK.sub(r"\1", text)
    text = _URL.sub("链接略过", text)
    text = _HTML_TAG.sub("", text)
    text = _MD_SYMBOL.sub("", text)
    text = _EMOJI.sub("", text)
    text = text.replace("\r", "").replace("\t", " ")
    text = _SPACE.sub(" ", text)
    text = _BLANK.sub("\n", text).strip(" \n-")
    if not text:
        return ""
    limit = max(40, int(TTS_MAX_CHARS))
    if len(text) > limit:
        cut = text[:limit]
        # 尽量在句号/问号/分号处收尾，避免念半句
        pos = max(cut.rfind("。"), cut.rfind("！"), cut.rfind("？"), cut.rfind("；"))
        if pos >= limit * 0.6:
            cut = cut[:pos + 1]
        text = cut.rstrip("，,、；;：:") + "。"
    return text.strip()


def _sha1(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:20]


def _tts_cache_path(text: str) -> str:
    folder = os.path.join(LOG_DIR, "tts")
    os.makedirs(folder, exist_ok=True)
    key = f"{TTS_VOICE}|{TTS_RATE}|{TTS_VOLUME}|{text}"
    return os.path.join(folder, _sha1(key) + ".mp3")


def _edge_synthesize(text: str, path: str):
    """在线合成；导入放函数里，缺库时能明确降级"""
    import edge_tts

    async def run():
        communicate = edge_tts.Communicate(
            text, voice=TTS_VOICE, rate=TTS_RATE, volume=TTS_VOLUME)
        await asyncio.wait_for(communicate.save(path), timeout=25)

    asyncio.run(run())


class Speaker:
    def __init__(self, enabled: bool = None):
        self._enabled = TTS_ENABLED if enabled is None else bool(enabled)
        self._queue: queue.Queue = queue.Queue(maxsize=3)
        self._lock = threading.Lock()
        self._worker: Optional[threading.Thread] = None
        self._stop_all = threading.Event()
        self._interrupted = threading.Event()
        self._warned = False
        self._mixer_ready = False
        self.last_backend = ""

    @property
    def enabled(self) -> bool:
        return self._enabled

    def set_enabled(self, value: bool):
        self._enabled = bool(value)
        if not self._enabled:
            self.stop()

    def speak(self, text: str, interrupt: bool = True) -> bool:
        if not self._enabled:
            return False
        clean = clean_tts_text(text)
        if not clean:
            return False
        if interrupt:
            self.stop(clear=True)
        self._ensure_worker()
        try:
            self._queue.put_nowait(clean)
        except queue.Full:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self._queue.put_nowait(clean)
            except queue.Full:
                return False
        return True

    def stop(self, clear: bool = True):
        self._interrupted.set()
        try:
            import pygame
            if pygame.mixer.get_init():
                pygame.mixer.music.stop()
        except Exception:
            pass
        if clear:
            while True:
                try:
                    self._queue.get_nowait()
                except queue.Empty:
                    break

    def _ensure_worker(self):
        with self._lock:
            if self._worker is not None and self._worker.is_alive():
                return
            self._stop_all.clear()
            self._worker = threading.Thread(
                target=self._run, daemon=True, name="andy-tts")
            self._worker.start()

    def _run(self):
        while not self._stop_all.is_set():
            try:
                text = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if text is None:
                break
            self._interrupted.clear()
            try:
                self._play(text)
            except Exception as e:
                self._warn_once(f"语音播放失败：{e}")

    def _play(self, text: str) -> bool:
        if _OVERRIDE is not None:
            _OVERRIDE(text)
            return True
        if self._play_edge(text):
            return True
        if self._play_sapi(text):
            self.last_backend = self.last_backend or "sapi"
            return True
        self._warn_once("edge-tts 与 Windows SAPI 都不可用，本次只显示文字")
        return False

    def _play_edge(self, text: str) -> bool:
        try:
            path = _tts_cache_path(text)
            if not (os.path.isfile(path) and os.path.getsize(path) > 100):
                _edge_synthesize(text, path)
            if self._play_file(path):
                self.last_backend = "edge-tts"
                return True
        except Exception as e:
            logger.info("[voice] edge-tts 不可用，尝试 SAPI：%s", e)
        return False

    def _play_file(self, path: str) -> bool:
        try:
            import pygame
            if not self._mixer_ready:
                pygame.mixer.init()
                self._mixer_ready = True
            pygame.mixer.music.load(path)
            pygame.mixer.music.play()
            while pygame.mixer.music.get_busy():
                if self._interrupted.is_set() or self._stop_all.is_set():
                    pygame.mixer.music.stop()
                    break
                time.sleep(0.1)
            return True
        except Exception as e:
            logger.info("[voice] 音频设备不可用：%s", e)
            return False

    def _play_sapi(self, text: str) -> bool:
        try:
            import pyttsx3
        except Exception:
            return False
        engine = None
        try:
            engine = pyttsx3.init()
            engine.setProperty("rate", TTS_SAPI_RATE)
            engine.setProperty("volume", TTS_SAPI_VOLUME)
            for voice in engine.getProperty("voices"):
                langs = [str(x).lower() for x in (getattr(voice, "languages", None) or [])]
                name = str(getattr(voice, "name", "")).lower()
                if any("zh" in x or "chinese" in x for x in langs) or "chinese" in name:
                    engine.setProperty("voice", voice.id)
                    break
            engine.say(text)
            engine.runAndWait()
            self.last_backend = "sapi"
            return True
        except Exception as e:
            logger.info("[voice] SAPI 不可用：%s", e)
            return False
        finally:
            try:
                if engine is not None:
                    engine.stop()
            except Exception:
                pass

    def _warn_once(self, msg: str):
        if self._warned:
            return
        self._warned = True
        logger.warning("[voice] %s", msg)

    def shutdown(self):
        self._stop_all.set()
        self.stop(clear=True)
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass
        if self._worker is not None:
            self._worker.join(timeout=2)
        try:
            import pygame
            if pygame.mixer.get_init():
                pygame.mixer.quit()
        except Exception:
            pass


speaker = Speaker()


def health_check() -> bool:
    """有任意一个后端就认为可用；真正的网络/设备问题在播放时降级"""
    try:
        import importlib.util
        return bool(importlib.util.find_spec("edge_tts") or
                    importlib.util.find_spec("pyttsx3"))
    except Exception:
        return False


def status() -> dict:
    return {"enabled": speaker.enabled, "backend": speaker.last_backend or "未播放"}
