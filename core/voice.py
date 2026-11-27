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

from config import (LOG_DIR, TTS_CHUNK_CHARS, TTS_ENABLED, TTS_MAX_CHARS,
                    TTS_PITCH, TTS_PITCH_EN, TTS_PITCH_ZH, TTS_RATE,
                    TTS_RATE_EN, TTS_RATE_ZH, TTS_SAPI_RATE,
                    TTS_SAPI_VOLUME, TTS_VOICE, TTS_VOICE_EN, TTS_VOICE_ZH,
                    TTS_VOLUME)
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
_CJK = re.compile(r"[\u4e00-\u9fff]")
_SENTENCE_END = re.compile(r"(?<=[。！？!?;；])\s*|(?<=\.)\s+")
_STREAM_END = re.compile(r"[。！？!?;；]|(?<=\.)\s+")
_COMMA = "，,、：:"


class StreamFeed:
    """把流式增量切成句子：第一句一出来就能先合成先播"""

    def __init__(self, min_chars: int = 8, max_chars: int = 120):
        self.min_chars = max(2, int(min_chars))
        self.max_chars = max(self.min_chars * 2, int(max_chars))
        self.buf = ""

    def feed(self, delta: str) -> list:
        self.buf += delta or ""
        out = []
        while True:
            m = _STREAM_END.search(self.buf, self.min_chars)
            if m:
                out.append(self.buf[:m.end()].strip())
                self.buf = self.buf[m.end():].lstrip()
                continue
            if len(self.buf) >= self.max_chars:
                cut = max(self.buf.rfind(c, 0, self.max_chars) for c in _COMMA)
                if cut < self.max_chars * 0.5:
                    cut = self.max_chars - 1
                out.append(self.buf[:cut + 1].strip())
                self.buf = self.buf[cut + 1:].lstrip()
                continue
            break
        return [s for s in out if s]

    def flush(self) -> list:
        tail = self.buf.strip()
        self.buf = ""
        return [tail] if tail else []


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


def _has_chinese(text: str) -> bool:
    return len(_CJK.findall(text or "")) >= 2


def pick_profile(text: str) -> dict:
    """选音色：显式 TTS_VOICE 优先；否则中文走 ZH、英文走 EN"""
    if TTS_VOICE:
        return {"voice": TTS_VOICE, "rate": TTS_RATE,
                "pitch": TTS_PITCH, "lang": "custom"}
    if _has_chinese(text):
        return {"voice": TTS_VOICE_ZH, "rate": TTS_RATE_ZH,
                "pitch": TTS_PITCH_ZH, "lang": "zh"}
    return {"voice": TTS_VOICE_EN, "rate": TTS_RATE_EN,
            "pitch": TTS_PITCH_EN, "lang": "en"}


def split_tts_text(text: str, max_len: int = None) -> list:
    """按句切分，让第一段先合成先播；段太长再按逗号/硬上限切"""
    text = (text or "").strip()
    if not text:
        return []
    limit = max(8, int(max_len or TTS_CHUNK_CHARS))
    parts = [p.strip() for p in _SENTENCE_END.split(text) if p and p.strip()]
    if not parts:
        return [text]

    chunks, buf = [], ""
    for part in parts:
        # 单句超长：优先在逗号处切，找不到再按上限硬切
        while len(part) > limit:
            cut = max(part.rfind(c, 0, limit) for c in _COMMA)
            if cut < limit * 0.5:
                cut = limit - 1
            piece, part = part[:cut + 1].strip(), part[cut + 1:].strip()
            if piece:
                chunks.append(piece)
        if not part:
            continue
        if len(buf) + len(part) <= limit:
            buf += part
        else:
            if buf:
                chunks.append(buf)
            buf = part
    if buf:
        chunks.append(buf)
    return chunks or [text]


def _sha1(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:20]


def _tts_cache_path(text: str, profile: dict) -> str:
    folder = os.path.join(LOG_DIR, "tts")
    os.makedirs(folder, exist_ok=True)
    key = "|".join([
        profile["voice"], profile["rate"], profile["pitch"], TTS_VOLUME, text,
    ])
    return os.path.join(folder, _sha1(key) + ".mp3")


def _edge_synthesize(text: str, path: str, profile: dict):
    """在线合成；导入放函数里，缺库时能明确降级"""
    import edge_tts

    async def run():
        communicate = edge_tts.Communicate(
            text, voice=profile["voice"], rate=profile["rate"],
            volume=TTS_VOLUME, pitch=profile["pitch"])
        await asyncio.wait_for(communicate.save(path), timeout=25)

    last = None
    for attempt in range(2):
        try:
            asyncio.run(run())
            if os.path.isfile(path) and os.path.getsize(path) > 100:
                return
            last = RuntimeError("合成结果为空")
        except Exception as e:
            last = e
            try:
                if os.path.isfile(path):
                    os.remove(path)
            except OSError:
                pass
            if attempt == 0:
                time.sleep(0.5)
    raise last


class Speaker:
    def __init__(self, enabled: bool = None):
        self._enabled = TTS_ENABLED if enabled is None else bool(enabled)
        self._queue: queue.Queue = queue.Queue(maxsize=12)
        self._lock = threading.Lock()
        self._worker: Optional[threading.Thread] = None
        self._stop_all = threading.Event()
        self._interrupted = threading.Event()
        self._warned = False
        self._mixer_ready = False
        self._mixer_lock = threading.Lock()
        self.last_backend = ""
        self.last_voice = ""
        self.prefer_offline = False

    @property
    def enabled(self) -> bool:
        return self._enabled

    def set_enabled(self, value: bool):
        self._enabled = bool(value)
        if not self._enabled:
            self.stop()

    def warmup(self):
        """启动时先把 pygame 音频设备初始化好，避开第一次播放的额外等待"""
        if self._mixer_ready:
            return
        threading.Thread(target=self._ensure_mixer, daemon=True,
                         name="andy-tts-warmup").start()

    def _ensure_mixer(self) -> bool:
        if self._mixer_ready:
            return True
        with self._mixer_lock:
            if self._mixer_ready:
                return True
            try:
                import pygame
                pygame.mixer.init()
                self._mixer_ready = True
                return True
            except Exception as e:
                logger.info("[voice] 音频设备预热失败：%s", e)
                return False

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
        if self.prefer_offline:
            if self._play_sapi(text):
                return True
            self._warn_once("Windows SAPI 不可用，本次只显示文字")
            return False
        if self._play_edge(text):
            return True
        if self._play_sapi(text):
            self.last_backend = self.last_backend or "sapi"
            return True
        self._warn_once("edge-tts 与 Windows SAPI 都不可用，本次只显示文字")
        return False

    def _play_edge(self, text: str) -> bool:
        chunks = split_tts_text(text)
        if not chunks:
            return False
        profile = pick_profile(text)
        played = False
        sapi_only = False
        for idx, chunk in enumerate(chunks):
            if self._interrupted.is_set() or self._stop_all.is_set():
                break
            if sapi_only:
                if self._play_sapi(chunk, profile["lang"]):
                    played = True
                continue
            try:
                path = _tts_cache_path(chunk, profile)
                if not (os.path.isfile(path) and os.path.getsize(path) > 100):
                    t0 = time.time()
                    _edge_synthesize(chunk, path, profile)
                    if idx == 0:
                        logger.info("[voice] 首段合成 %.2fs，共 %d 段，音色 %s",
                                    time.time() - t0, len(chunks),
                                    profile["voice"])
                if self._play_file(path):
                    played = True
                    self.last_backend = "edge-tts"
                    self.last_voice = profile["voice"]
            except Exception as e:
                logger.info("[voice] edge-tts 第 %d 段不可用，后续改 SAPI：%s",
                            idx + 1, e)
                sapi_only = True
                if self._play_sapi(chunk, profile["lang"]):
                    played = True
        return played

    def _play_file(self, path: str) -> bool:
        try:
            import pygame
            if not self._ensure_mixer():
                return False
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

    def _play_sapi(self, text: str, lang: str = None) -> bool:
        try:
            import pyttsx3
        except Exception:
            return False
        engine = None
        try:
            engine = pyttsx3.init()
            engine.setProperty("rate", TTS_SAPI_RATE)
            engine.setProperty("volume", TTS_SAPI_VOLUME)
            want = lang if lang in ("zh", "en") else (
                "zh" if _has_chinese(text) else "en")
            chosen = ""
            for voice in engine.getProperty("voices"):
                langs = [str(x).lower() for x in (getattr(voice, "languages", None) or [])]
                name = str(getattr(voice, "name", "")).lower()
                if any(want in x or ("chinese" if want == "zh" else "english") in x
                       for x in langs) or want in name:
                    engine.setProperty("voice", voice.id)
                    chosen = str(getattr(voice, "name", ""))
                    break
            engine.say(text)
            engine.runAndWait()
            self.last_backend = "sapi"
            self.last_voice = chosen or "sapi"
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

VOICE_MODES = ("auto", "zh", "en", "offline")


def set_voice_mode(mode: str):
    """auto=按语言自动；zh/en=强制在线音色；offline=只用 Windows SAPI"""
    global TTS_VOICE, TTS_RATE, TTS_PITCH
    mode = mode if mode in VOICE_MODES else "auto"
    if mode == "zh":
        TTS_VOICE, TTS_RATE, TTS_PITCH = TTS_VOICE_ZH, TTS_RATE_ZH, TTS_PITCH_ZH
        speaker.prefer_offline = False
    elif mode == "en":
        TTS_VOICE, TTS_RATE, TTS_PITCH = TTS_VOICE_EN, TTS_RATE_EN, TTS_PITCH_EN
        speaker.prefer_offline = False
    elif mode == "offline":
        TTS_VOICE = ""
        speaker.prefer_offline = True
    else:
        TTS_VOICE = ""
        speaker.prefer_offline = False
    return mode


def current_mode() -> str:
    if speaker.prefer_offline:
        return "offline"
    if TTS_VOICE == TTS_VOICE_ZH:
        return "zh"
    if TTS_VOICE == TTS_VOICE_EN:
        return "en"
    return "auto"


def sample_text() -> str:
    mode = current_mode()
    if mode == "en":
        return "Hello, I am Andy. Voice is ready."
    return "你好，我是 Andy。语音已经准备好了。"


def health_check() -> bool:
    """有任意一个后端就认为可用；真正的网络/设备问题在播放时降级"""
    try:
        import importlib.util
        return bool(importlib.util.find_spec("edge_tts") or
                    importlib.util.find_spec("pyttsx3"))
    except Exception:
        return False


def status() -> dict:
    return {"enabled": speaker.enabled, "backend": speaker.last_backend or "未播放",
            "voice": speaker.last_voice or "未播放"}
