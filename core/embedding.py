"""
向量层 embedding.py
职责：把文本变成向量（远程 embedding-3）、本地向量运算、查询缓存
设计：调用失败一律返回 None，由调用方降级到关键词检索，不阻断对话
"""

import hashlib
import threading

import numpy as np
from openai import OpenAI

import config
from console import logger

_CLIENT = None
_LOCK = threading.Lock()
_CACHE = {}                 # sha1(text) -> np.ndarray（只缓存查询向量）
_CACHE_LIMIT = 64
_OVERRIDE = None            # 测试注入：fn(list[str]) -> list[np.ndarray] | None
_WARNED = False


def client() -> OpenAI:
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = OpenAI(api_key=config.ZHIPU_API_KEY,
                         base_url=config.ZHIPU_BASE_URL, timeout=20)
    return _CLIENT


def is_enabled() -> bool:
    return bool(config.EMBED_ENABLED and config.ZHIPU_API_KEY)


def set_embedder(fn) -> None:
    """测试用：注入假 embedder；传 None 恢复远程调用"""
    global _OVERRIDE
    _OVERRIDE = fn


def _normalize(vec) -> np.ndarray:
    v = np.asarray(vec, dtype=np.float32)
    n = float(np.linalg.norm(v))
    return v / n if n else v


def embed_texts(texts: list, model: str = None) -> list:
    """
    批量文本 → 单位向量列表
    失败返回 None（网络/额度/模型不支持），调用方据此降级
    """
    if not texts:
        return []
    if _OVERRIDE is not None:
        return _OVERRIDE(texts)
    if not is_enabled():
        return None
    model = model or config.EMBED_MODEL
    try:
        resp = client().embeddings.create(model=model, input=texts)
        return [_normalize(d.embedding) for d in resp.data]
    except Exception as e:
        global _WARNED
        if not _WARNED:
            logger.warning("[embed] 向量化失败，降级关键词检索: %s: %s", type(e).__name__, e)
            _WARNED = True
        return None


def embed_query(text: str) -> np.ndarray:
    """查询向量（带进程内缓存，同一句话不重复计费）"""
    if not text:
        return None
    key = hashlib.sha1(text.strip().encode("utf-8")).hexdigest()
    with _LOCK:
        hit = _CACHE.get(key)
    if hit is not None:
        return hit
    vecs = embed_texts([text])
    if not vecs:
        return None
    v = vecs[0]
    with _LOCK:
        if len(_CACHE) >= _CACHE_LIMIT:
            _CACHE.clear()
        _CACHE[key] = v
    return v


def to_blob(vec) -> bytes:
    """单位向量 → BLOB（float32，小端，与 numpy 默认一致）"""
    return np.asarray(vec, dtype=np.float32).tobytes()


def from_blob(blob: bytes, dim: int) -> np.ndarray:
    return np.frombuffer(blob, dtype=np.float32, count=dim).astype(np.float32)


def cosine(a, b) -> float:
    """余弦相似度；输入已是单位向量时等价于点积"""
    if a is None or b is None:
        return 0.0
    a = np.asarray(a, dtype=np.float32)
    b = np.asarray(b, dtype=np.float32)
    denom = float(np.linalg.norm(a) * np.linalg.norm(b))
    return float(a @ b) / denom if denom else 0.0


def health_check() -> bool:
    """功能可用性：关闭或未配 key 时视为“正常关闭”，不算故障"""
    return True
