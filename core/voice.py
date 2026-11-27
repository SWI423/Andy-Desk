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
                    TTS_SAPI_VOLUME, TTS_VOICE, TTS_VOICE_EN, TTS_VOICE_EN_CA,
                    TTS_VOICE_EN_GB, TTS_VOICE_EN_HK, TTS_VOICE_EN_US,
                    TTS_VOICE_ZH, TTS_VOICE_ZH_TW, TTS_VOLUME)
from console import logger

os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")

# 测试注入：设置后 _play 直接调用它，不碰真实音频
_OVERRIDE = None

# 两套独立设置：
# - LANGUAGE_PROFILES 决定“回复语言”
# - VOICE_PROFILES 决定“朗读音色”；默认 follow 跟随语种
LANGUAGE_PROFILES = {
    "auto": {
        "label": "自动 · 跟随提问语言",
        "lang": "",
    },
    "zh-CN": {
        "label": "中文（大陆）· 简体中文",
        "lang": "zh", "sample": "zh-CN",
    },
    "zh-TW": {
        "label": "中文（台灣）· 繁體中文",
        "lang": "zh", "sample": "zh-TW",
    },
    "en-US": {
        "label": "English (US) · 美式英语",
        "lang": "en", "sample": "en",
    },
    "en-GB": {
        "label": "English (UK) · 英式英语",
        "lang": "en", "sample": "en",
    },
    "en-CA": {
        "label": "English (Canada) · 加拿大英语",
        "lang": "en", "sample": "en",
    },
    "en-HK": {
        "label": "English (Hong Kong) · 香港英语",
        "lang": "en", "sample": "en",
    },
}
LANGUAGE_MODES = tuple(LANGUAGE_PROFILES)
LANGUAGE_ALIASES = {"zh": "zh-CN", "en": "en-US"}

VOICE_PROFILES = {
    "follow": {
        "label": "跟随语种（推荐）",
        "voice": "", "rate": TTS_RATE, "pitch": TTS_PITCH, "lang": "auto",
        "sample": "",
    },
    "zh-CN": {
        "label": "大陆中文 · zh-CN-YunxiNeural",
        "voice": TTS_VOICE_ZH, "rate": TTS_RATE_ZH,
        "pitch": TTS_PITCH_ZH, "lang": "zh", "sample": "zh-CN",
    },
    "zh-TW": {
        "label": "台灣中文 · zh-TW-HsiaoChenNeural",
        "voice": TTS_VOICE_ZH_TW, "rate": TTS_RATE_ZH,
        "pitch": TTS_PITCH_ZH, "lang": "zh", "sample": "zh-TW",
    },
    "en-US": {
        "label": "English (US) · en-US-BrianMultilingualNeural",
        "voice": TTS_VOICE_EN_US, "rate": TTS_RATE_EN,
        "pitch": TTS_PITCH_EN, "lang": "en", "sample": "en",
    },
    "en-GB": {
        "label": "English (UK) · en-GB-RyanNeural",
        "voice": TTS_VOICE_EN_GB, "rate": TTS_RATE_EN,
        "pitch": TTS_PITCH_EN, "lang": "en", "sample": "en",
    },
    "en-CA": {
        "label": "English (Canada) · en-CA-LiamNeural",
        "voice": TTS_VOICE_EN_CA, "rate": TTS_RATE_EN,
        "pitch": TTS_PITCH_EN, "lang": "en", "sample": "en",
    },
    "en-HK": {
        "label": "English (Hong Kong) · en-HK-SamNeural",
        "voice": TTS_VOICE_EN_HK, "rate": TTS_RATE_EN,
        "pitch": TTS_PITCH_EN, "lang": "en", "sample": "en",
    },
    "sapi": {
        "label": "离线 · Windows 系统语音",
        "voice": "", "rate": TTS_RATE, "pitch": TTS_PITCH, "lang": "auto",
        "sample": "",
    },
}
VOICE_MODES = tuple(VOICE_PROFILES)
VOICE_ALIASES = {"offline": "sapi", "zh": "zh-CN", "en": "en-US"}

_language_mode = "auto"
_voice_mode = "custom" if TTS_VOICE else "follow"

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


def _profile_for_language(mode: str) -> dict:
    """语种 → 对应音色档；auto 返回空，让调用方按文本判断"""
    mode = LANGUAGE_ALIASES.get(mode, mode)
    profile = VOICE_PROFILES.get(mode)
    return dict(profile) if profile else {}


def pick_profile(text: str) -> dict:
    """选音色：显式音色优先；否则按回复语种选；auto 再按文本语言选"""
    if _voice_mode == "custom" and TTS_VOICE:
        return {"voice": TTS_VOICE, "rate": TTS_RATE,
                "pitch": TTS_PITCH, "lang": "zh" if _has_chinese(text) else "en"}
    if _voice_mode not in ("follow", "sapi"):
        profile = VOICE_PROFILES.get(_voice_mode)
        if profile and profile["voice"]:
            return dict(profile)
    lang_mode = _language_mode
    if lang_mode in ("zh-CN", "zh-TW", "en-US", "en-GB", "en-CA", "en-HK"):
        profile = VOICE_PROFILES.get(lang_mode)
        if profile:
            return dict(profile)
    lang = "zh" if _has_chinese(text) else "en"
    if lang == "zh":
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


def language_choices() -> list:
    return [(key, p["label"]) for key, p in LANGUAGE_PROFILES.items()]


def voice_choices() -> list:
    return [(key, p["label"]) for key, p in VOICE_PROFILES.items()]


def profile_choices() -> list:
    """兼容旧 UI；新 UI 用 language_choices / voice_choices"""
    return language_choices()


def set_language_mode(mode: str):
    """只切回复语言，不碰音色；offline 是旧设置，迁移成「语言自动 + SAPI」"""
    global _language_mode
    mode = LANGUAGE_ALIASES.get(mode, mode)
    if mode == "offline":
        set_voice_mode("sapi")
        _language_mode = "auto"
        return "auto"
    if mode not in LANGUAGE_PROFILES:
        mode = "auto"
    _language_mode = mode
    return mode


def set_voice_mode(mode: str):
    """只切朗读音色，不碰回复语言；follow=跟随语种，sapi=离线"""
    global TTS_VOICE, TTS_RATE, TTS_PITCH, _voice_mode
    mode = VOICE_ALIASES.get(mode, mode)
    if mode == "custom":
        _voice_mode = "custom" if TTS_VOICE else "follow"
        return _voice_mode
    if mode not in VOICE_MODES:
        mode = "follow"
    _voice_mode = mode
    profile = VOICE_PROFILES[mode]
    if mode in ("follow", "sapi"):
        TTS_VOICE = ""
        TTS_RATE, TTS_PITCH = TTS_RATE_ZH, TTS_PITCH_ZH
    else:
        TTS_VOICE, TTS_RATE, TTS_PITCH = (
            profile["voice"], profile["rate"], profile["pitch"])
    speaker.prefer_offline = bool(mode == "sapi")
    return mode


def current_language_mode() -> str:
    return _language_mode


def current_voice_mode() -> str:
    if _voice_mode == "custom" and TTS_VOICE:
        return "custom"
    return _voice_mode


def current_mode() -> str:
    """兼容旧调用名，返回当前回复语种"""
    return current_language_mode()


def current_language() -> str:
    """auto 返回空串，表示不强制回复语言"""
    lang = LANGUAGE_PROFILES.get(_language_mode, {}).get("lang", "")
    return "" if lang == "auto" else lang


def language_locale(mode: str = None) -> str:
    mode = mode or current_language_mode()
    return LANGUAGE_PROFILES.get(mode, {}).get("lang", "")


def voice_locale(mode: str = None) -> str:
    mode = mode or current_voice_mode()
    return VOICE_PROFILES.get(mode, {}).get("lang", "")


def language_label(mode: str = None) -> str:
    mode = mode or current_language_mode()
    return LANGUAGE_PROFILES.get(mode, LANGUAGE_PROFILES["auto"])["label"]


def voice_label(mode: str = None) -> str:
    mode = mode or current_voice_mode()
    if mode == "custom":
        return f"自定义 · {TTS_VOICE or '未设置'}"
    return VOICE_PROFILES.get(mode, VOICE_PROFILES["follow"])["label"]


def summary_label() -> str:
    return f"语种：{language_label()}｜音色：{voice_label()}"


def profile_label(mode: str = None) -> str:
    """兼容旧调用名；返回完整「语种｜音色」"""
    if mode and mode in LANGUAGE_PROFILES:
        return language_label(mode)
    return summary_label()


def mismatch_warning(language_mode: str = None, voice_mode: str = None) -> str:
    """跨语种组合的提示；空串表示无需提示"""
    lang = LANGUAGE_ALIASES.get(language_mode or current_language_mode(),
                               language_mode or current_language_mode())
    voice = VOICE_ALIASES.get(voice_mode or current_voice_mode(),
                              voice_mode or current_voice_mode())
    if voice in ("follow", "sapi"):
        return ""
    if voice == "custom":
        return "自定义音色无法判断适用语种；跨语言朗读可能不自然。"
    if lang == "auto":
        return "回复语种为自动，会随提问切换；指定音色可能与某轮回复语言不一致。"
    ll, vl = language_locale(lang), voice_locale(voice)
    if ll and vl and ll != vl:
        return (f"当前回复语种是「{language_label(lang)}」，"
                f"音色「{voice_label(voice)}」主要用于另一语言；"
                "跨语言朗读可能不自然，是否继续？")
    return ""


def sample_text(voice_mode: str = None, language_mode: str = None) -> str:
    """试听句：优先按音色本身的语言，follow 时按回复语种"""
    lang = LANGUAGE_ALIASES.get(language_mode or current_language_mode(),
                                language_mode or current_language_mode())
    vm = VOICE_ALIASES.get(voice_mode or current_voice_mode(),
                           voice_mode or current_voice_mode())
    if vm == "follow":
        sample = LANGUAGE_PROFILES.get(lang, {}).get("sample", "")
    elif vm == "sapi":
        sample = LANGUAGE_PROFILES.get(lang, {}).get("sample", "") or "zh-CN"
    else:
        sample = VOICE_PROFILES.get(vm, {}).get("sample", "") or "zh-CN"
    if sample == "en":
        return "Hello, I am Andy. This is my English voice."
    if sample == "zh-TW":
        return "你好，我是 Andy。這是台灣中文語音。"
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
