"""
向量检索测试（不联网，用假 embedder）
覆盖：余弦/序列化、RRF 融合、换说法召回、向量不可用降级、补索引幂等
"""

import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture(autouse=True)
def tmp_db(tmp_path, monkeypatch):
    from core import db, embedding, memory
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "vec.db"))
    monkeypatch.setattr(db, "_CONN", None)
    monkeypatch.setattr(memory, "_VEC_CACHE", {"key": None, "ids": None, "mat": None})
    embedding._CACHE.clear()
    db.init_db()
    yield db
    if db._CONN is not None:
        db._CONN.close()
        db._CONN = None
    embedding.set_embedder(None)


def _unit(v):
    v = np.asarray(v, dtype=np.float32)
    return v / np.linalg.norm(v)


FAKE_VECTORS = {
    "证": [1, 0, 0],
    "设计师": [1, 0, 0],
    "软考": [0.95, 0.05, 0],
    "花生": [0, 1, 0],
    "过敏": [0, 0.95, 0.05],
}


def fake_embed(texts):
    out = []
    for t in texts:
        vec = [0, 0, 1]
        for kw, v in FAKE_VECTORS.items():
            if kw in t:
                vec = v
                break
        out.append(_unit(vec))
    return out


def test_cosine_and_blob_roundtrip():
    from core import embedding
    a = _unit([1, 0, 0])
    assert embedding.cosine(a, a) == pytest.approx(1.0)
    assert embedding.cosine(a, _unit([0, 1, 0])) == pytest.approx(0.0)

    blob = embedding.to_blob(a)
    back = embedding.from_blob(blob, 3)
    assert back.shape == (3,)
    assert float(back[0]) == pytest.approx(1.0)


def test_rrf_prefers_both_lists():
    from core.memory import _rrf_merge
    lists = [
        ("vector", [{"id": 1, "content": "A", "role": "user", "ts": "t",
                     "vector_score": 0.9},
                    {"id": 2, "content": "B", "role": "user", "ts": "t",
                     "vector_score": 0.8}]),
        ("keyword", [{"id": 2, "content": "B", "role": "user", "ts": "t",
                      "keyword_score": 0.7}]),
    ]
    fused = _rrf_merge(lists)
    assert fused[0]["id"] == 2 and fused[0]["via"] == "both"
    assert fused[1]["id"] == 1


def test_vector_recall_paraphrase():
    """换说法：只共享“证”，关键词不够，向量要能召回"""
    from core import memory, embedding
    embedding.set_embedder(fake_embed)
    memory.log_event("user", "周三要交软件设计师的证，别忘了打印")
    memory.log_event("user", "今天好累，不想动")
    memory.backfill_vectors(limit=10)

    kw = memory.search_events("那个证的事怎么样了", mode="keyword", exclude_recent=0)
    assert isinstance(kw, list)          # 关键词侧不足以命中（或命中很弱）
    res = memory.search_events("那个证的事怎么样了", mode="auto", exclude_recent=0)
    assert res and res[0]["content"].startswith("周三要交")
    assert res[0]["via"] == "vector"
    assert res[0]["vector_score"] >= 0.9


def test_fallback_when_embedding_down():
    from core import memory, embedding
    embedding.set_embedder(lambda texts: None)
    memory.log_event("user", "我最近在做软考复习，报了下个月的考试")
    memory.log_event("assistant", "记下了，软考复习是学习线的主任务。")
    memory.backfill_vectors(limit=10)
    res = memory.search_events("软考复习到哪了", exclude_recent=0)
    assert res and res[0]["via"] == "keyword"
    assert "软考" in res[0]["content"]


def test_backfill_idempotent():
    from core import memory, embedding
    embedding.set_embedder(fake_embed)
    memory.log_event("user", "我对花生过敏")
    first = memory.backfill_vectors(limit=10)
    assert first["embedded"] == 1 and first["remaining"] == 0
    second = memory.backfill_vectors(limit=10)
    assert second["embedded"] == 0 and second["remaining"] == 0
    assert memory.missing_vector_count() == 0


def test_recent_window_excluded():
    """最近 12 条已被原文注入，不应重复召回"""
    from core import memory, embedding
    embedding.set_embedder(fake_embed)
    memory.log_event("user", "我对花生过敏，中午别点带花生的菜")
    for i in range(12):
        text = "周三交证" if i % 2 else "软考复习"
        memory.log_event("user", f"{text} 闲聊第 {i} 条")
    memory.backfill_vectors(limit=20)
    res = memory.search_events("花生", exclude_recent=12)
    assert res and "花生" in res[0]["content"]
