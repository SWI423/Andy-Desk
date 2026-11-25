"""
记忆层 memory.py
- L2 情景记忆：chat_events（append-only）
- L3 语义记忆：semantic_facts（规则蒸馏 + 去重）
- 读路径：build_context() 按 token 预算组装注入内容
设计依据：docs/MEMORY_DESIGN.md
"""

import re
import threading
from difflib import SequenceMatcher

import numpy as np

import config
from config import SESSION_ID, HISTORY_LIMIT, CONTEXT_TOKEN_BUDGET
from console import logger
from core import db, embedding

# ---------------- 规则：事实抽取 ----------------

# (predicate, domain, 触发关键词)
FACT_RULES = [
    ("name", "general", ["我叫", "我的名字是", "叫我"]),
    ("allergy", "life", ["过敏"]),
    ("chronic", "life", ["慢性病", "老毛病"]),
    ("exam_anchor", "study", ["考试", "答辩", "面试"]),
    ("work_anchor", "work", ["截止", "deadline", "交付", "汇报"]),
    ("preference", "general", ["我不喜欢", "我喜欢", "别对我", "不要对我"]),
    ("habit_study", "study", ["每天.*学", "固定.*复习"]),
    ("habit_life", "life", ["每天.*睡", "作息"]),
]

# 意图关键词（决定注入哪些内容 / 是否需要任务能力）
INTENT_KEYWORDS = {
    "task": ["任务", "待办", "安排", "计划", "进度", "做完", "完成", "排一下", "拆"],
    "reminder": ["提醒", "叫我", "到点", "别忘了", "定时"],
    "health": ["头疼", "不舒服", "难受", "疼", "失眠", "焦虑", "累"],
    "memory": ["上次", "之前", "记得", "说过", "我们聊"],
}


def estimate_tokens(text: str) -> int:
    """粗估 token：中文按 1 字≈1 token，英文按 4 字符≈1 token"""
    if not text:
        return 0
    cn = len(re.findall(r"[\u4e00-\u9fff]", text))
    other = len(text) - cn
    return int(cn + other / 4)


def detect_intents(text: str) -> list:
    hits = []
    for intent, kws in INTENT_KEYWORDS.items():
        if any(k in text for k in kws):
            hits.append(intent)
    return hits or ["general"]


# ---------------- L2：事件写入 ----------------

def log_event(role: str, content: str, importance: float = 0.0,
              session_id: str = SESSION_ID) -> int:
    """追加一条事件流水"""
    if not content:
        return 0
    return db.execute(
        "INSERT INTO chat_events(session_id, role, content, ts, importance) VALUES(?,?,?,?,?)",
        (session_id, role, content.strip(), db.now_iso(), importance),
    )


def recent_events(limit: int = HISTORY_LIMIT * 2, session_id: str = SESSION_ID) -> list:
    """最近 N 条事件（按时间正序返回，便于拼 prompt）"""
    rows = db.query(
        "SELECT * FROM chat_events WHERE session_id=? ORDER BY id DESC LIMIT ?",
        (session_id, limit),
    )
    return list(reversed(rows))


# ---------------- L3：事实蒸馏 ----------------

def upsert_fact(predicate: str, obj: str, domain: str = "general",
                confidence: float = 0.8, source_event_id: int = None) -> dict:
    """
    写入/更新一条事实
    去重：同 predicate 下文本相似度 > 0.9 视为同一条，只更新时间
    """
    obj = (obj or "").strip()
    if not obj:
        return {"ok": False, "reason": "empty"}

    existing = db.query(
        "SELECT * FROM semantic_facts WHERE predicate=? AND subject='user'", (predicate,)
    )
    for row in existing:
        if row["object"] == obj:
            db.execute("UPDATE semantic_facts SET updated_at=?, confidence=? WHERE id=?",
                       (db.now_iso(), confidence, row["id"]))
            return {"ok": True, "action": "updated", "id": row["id"], "fact": obj}

    for row in existing:
        old = row["object"]
        shorter, longer = sorted((old, obj), key=len)
        similar = SequenceMatcher(None, old, obj).ratio() > 0.9
        contained = shorter in longer and len(longer) - len(shorter) <= 6
        if similar or contained:
            db.execute("UPDATE semantic_facts SET object=?, updated_at=?, confidence=? WHERE id=?",
                       (obj, db.now_iso(), confidence, row["id"]))
            return {"ok": True, "action": "merged", "id": row["id"], "fact": obj}

    fid = db.execute(
        """INSERT INTO semantic_facts
           (subject, predicate, object, domain, confidence, source_event_id, created_at, updated_at)
           VALUES('user',?,?,?,?,?,?,?)""",
        (predicate, obj, domain, confidence, source_event_id, db.now_iso(), db.now_iso()),
    )
    return {"ok": True, "action": "created", "id": fid, "fact": obj}


def extract_facts(text: str, source_event_id: int = None) -> list:
    """规则蒸馏：从一句话里抽取稳定事实；抽不到返回空列表"""
    results = []
    for predicate, domain, kws in FACT_RULES:
        for kw in kws:
            if ".*" in kw:
                m = re.search(kw, text)
                if not m:
                    continue
                rest = text[m.end():]
            else:
                if kw not in text:
                    continue
                rest = text.split(kw, 1)[1]
            # 事实只取到第一个标点为止，避免把半句话都记下来
            obj = re.split(r"[，。！？,.;；:：]", rest)[0].strip(" 　")
            if 1 <= len(obj) <= 60:
                results.append(upsert_fact(predicate, obj, domain=domain,
                                           source_event_id=source_event_id))
            break
    return results


def get_facts(domain: str = None, limit: int = 20) -> list:
    if domain:
        return db.query(
            "SELECT * FROM semantic_facts WHERE domain=? ORDER BY updated_at DESC LIMIT ?",
            (domain, limit))
    return db.query("SELECT * FROM semantic_facts ORDER BY updated_at DESC LIMIT ?", (limit,))


# ---------------- 检索：embedding 语义 + 关键词，RRF 融合 ----------------
#
# 设计（ADR-008，取代 ADR-003）：
#   向量侧：embedding-3（2048 维）→ 余弦 ≥ EMBED_MIN_SCORE → 取前 EMBED_TOPK
#   关键词侧：字符重合度 ≥ 0.35 → 取前 KEYWORD_TOPK
#   两路用 RRF（1/(k+rank)）融合，取前 top_k
# 降级：向量不可用（未配 key / 网络失败 / 尚未建索引）时只用关键词，不报错

_VEC_CACHE = {"key": None, "ids": None, "mat": None}
_BG_LOCK = threading.Lock()
_BG_THREAD = None


def _score(query_text: str, content: str) -> float:
    """关键词侧：查询字集合被历史覆盖的比例"""
    q = set(re.findall(r"[\u4e00-\u9fff]|[a-zA-Z]{2,}", query_text))
    if not q:
        return 0.0
    c = set(re.findall(r"[\u4e00-\u9fff]|[a-zA-Z]{2,}", content))
    return len(q & c) / len(q)


def _recent_event_ids(limit: int) -> set:
    rows = db.query("SELECT id FROM chat_events ORDER BY id DESC LIMIT ?", (limit,))
    return {r["id"] for r in rows}


def _load_vectors(model: str, limit: int = 1000):
    """
    加载最近 limit 条向量（带进程内缓存；事件新增后自动失效重载）
    同时做“去均值”中心化：embedding-3 的中文向量有明显公共方向，
    直接算余弦时无关句也有 0.5+；减去语料均值后，相关与无关才拉得开
    """
    stat = db.query_one(
        "SELECT COUNT(*) AS c, MAX(event_id) AS m FROM event_vectors WHERE model=?", (model,))
    key = (model, stat["c"] if stat else 0, stat["m"] if stat else None)
    if _VEC_CACHE["key"] == key and _VEC_CACHE["ids"] is not None:
        return _VEC_CACHE["ids"], _VEC_CACHE["mat"], _VEC_CACHE["mean"]

    rows = db.query(
        """SELECT v.event_id, v.dim, v.vec FROM event_vectors v
           JOIN chat_events e ON e.id=v.event_id
           WHERE v.model=? AND e.role IN ('user','assistant')
           ORDER BY e.id DESC LIMIT ?""", (model, limit))
    if not rows:
        _VEC_CACHE.update({"key": key, "ids": [], "mat": None, "mean": None})
        return [], None, None

    ids = [r["event_id"] for r in rows]
    raw = np.vstack([embedding.from_blob(r["vec"], r["dim"]) for r in rows])
    mean = raw.mean(axis=0)
    centered = raw - mean
    norms = np.linalg.norm(centered, axis=1, keepdims=True)
    centered = centered / np.maximum(norms, 1e-8)
    _VEC_CACHE.update({"key": key, "ids": ids, "mat": centered, "mean": mean})
    return ids, centered, mean


def _keyword_candidates(query_text: str, exclude_recent: int, top: int, threshold: float) -> list:
    rows = db.query(
        "SELECT * FROM chat_events WHERE role IN ('user','assistant') ORDER BY id DESC LIMIT 300")
    rows = rows[exclude_recent:] if len(rows) > exclude_recent else []
    scored = [(_score(query_text, m["content"]), m) for m in rows]
    scored = [(s, m) for s, m in scored if s >= threshold]
    scored.sort(key=lambda x: (-x[0], -x[1]["id"]))
    return [{"id": m["id"], "content": m["content"], "role": m["role"], "ts": m["ts"],
             "keyword_score": round(s, 3)} for s, m in scored[:top]]


def _vector_candidates(query_text: str, exclude_recent: int, top: int, min_score: float):
    """
    返回 (候选列表, 状态)
    状态：ok / disabled / no_query_vec / empty
    """
    if not config.EMBED_ENABLED:
        return [], "disabled"
    qv = embedding.embed_query(query_text)
    if qv is None:
        return [], "no_query_vec"

    ids, mat, mean = _load_vectors(config.EMBED_MODEL)
    if not ids:
        return [], "empty"

    qc = qv - mean
    qnorm = float(np.linalg.norm(qc))
    if qnorm < 1e-8:                     # 查询向量与语料均值重合，无区分度
        return [], "ok"
    scores = mat @ (qc / qnorm)
    recent = _recent_event_ids(exclude_recent)
    ranked = sorted(zip(ids, scores), key=lambda x: -float(x[1]))

    best = float(ranked[0][1]) if ranked else 0.0
    cutoff = max(min_score, best - config.EMBED_MARGIN)
    picked = [(i, float(s)) for i, s in ranked if i not in recent and s >= cutoff][:top]
    if not picked:
        return [], "ok"
    picked_ids = [i for i, _ in picked]
    rows = {r["id"]: r for r in db.query(
        "SELECT * FROM chat_events WHERE id IN (%s)" % ",".join("?" * len(picked_ids)),
        tuple(picked_ids))}
    out = []
    for i, s in picked:
        r = rows.get(i)
        if not r:
            continue
        out.append({"id": i, "content": r["content"], "role": r["role"], "ts": r["ts"],
                    "vector_score": round(s, 3)})
    return out, "ok"


def _rrf_merge(lists: list, k: int = config.RRF_K) -> list:
    """RRF：对多路排名各贡献 1/(k+rank)，按总分降序"""
    fused = {}
    for tag, items in lists:
        for rank, item in enumerate(items):
            node = fused.setdefault(item["id"], {"id": item["id"], "rrf": 0.0,
                                                 "content": item["content"], "role": item["role"],
                                                 "ts": item["ts"], "via": set(),
                                                 "vector_score": None, "keyword_score": None})
            node["rrf"] += 1.0 / (k + rank + 1)
            node["via"].add(tag)
            if tag == "vector":
                node["vector_score"] = item.get("vector_score")
            else:
                node["keyword_score"] = item.get("keyword_score")
    out = list(fused.values())
    for n in out:
        n["score"] = round(max(n["vector_score"] or 0.0, n["keyword_score"] or 0.0), 2)
        n["via"] = "both" if len(n["via"]) > 1 else next(iter(n["via"]))
    out.sort(key=lambda n: -n["rrf"])
    return out


def search_events(query_text: str, top_k: int = 3, mode: str = "auto",
                  threshold: float = 0.35, exclude_recent: int = HISTORY_LIMIT * 2) -> list:
    """
    混合检索（mode: auto / keyword / vector）
    返回 [{"id","content","role","ts","score","via","vector_score","keyword_score"}]
    """
    kw = _keyword_candidates(query_text, exclude_recent, config.KEYWORD_TOPK, threshold)
    vec, status = [], "disabled"
    if mode in ("auto", "vector"):
        vec, status = _vector_candidates(query_text, exclude_recent,
                                         config.EMBED_TOPK, config.EMBED_MIN_SCORE)

    lists = []
    if vec:
        lists.append(("vector", vec))
    if kw:
        lists.append(("keyword", kw))
    if not lists:
        _LAST_RETRIEVAL.update({"mode": "none", "vector_status": status,
                                "keyword": 0, "vector": 0})
        return []

    fused = _rrf_merge(lists)
    _LAST_RETRIEVAL.update({"mode": "hybrid" if len(lists) == 2 else lists[0][0],
                            "vector_status": status, "keyword": len(kw), "vector": len(vec)})
    return fused[:top_k]


_LAST_RETRIEVAL = {}


def last_retrieval() -> dict:
    """最近一次检索的统计（写日志/排查用）"""
    return dict(_LAST_RETRIEVAL)


# ---------------- 向量索引维护 ----------------

def _store_vector(event_id: int, content: str, model: str = None) -> bool:
    model = model or config.EMBED_MODEL
    vecs = embedding.embed_texts([content], model=model)
    if not vecs:
        return False
    vec = vecs[0]
    db.execute(
        """INSERT OR REPLACE INTO event_vectors(event_id, model, dim, vec, created_at)
           VALUES(?,?,?,?,?)""",
        (event_id, model, int(len(vec)), embedding.to_blob(vec), db.now_iso()))
    return True


def missing_vector_count(model: str = None) -> int:
    model = model or config.EMBED_MODEL
    row = db.query_one(
        """SELECT COUNT(*) AS c FROM chat_events e
           WHERE e.role IN ('user','assistant')
             AND NOT EXISTS (SELECT 1 FROM event_vectors v
                             WHERE v.event_id=e.id AND v.model=?)""", (model,))
    return int(row["c"]) if row else 0


def backfill_vectors(batch: int = None, limit: int = None, model: str = None) -> dict:
    """
    给还没有向量的历史事件补向量（幂等）
    batch 每批条数；limit 本次最多处理多少条；失败即停（避免网络异常时刷接口）
    """
    batch = batch or config.EMBED_BATCH
    limit = limit or config.EMBED_BACKFILL_MAX
    model = model or config.EMBED_MODEL
    embedded, failed = 0, 0

    while embedded < limit:
        rows = db.query(
            """SELECT e.id, e.content FROM chat_events e
               WHERE e.role IN ('user','assistant')
                 AND NOT EXISTS (SELECT 1 FROM event_vectors v
                                 WHERE v.event_id=e.id AND v.model=?)
               ORDER BY e.id ASC LIMIT ?""", (model, min(batch, limit - embedded)))
        if not rows:
            break
        vecs = embedding.embed_texts([r["content"] for r in rows], model=model)
        if not vecs:
            failed += len(rows)
            break
        for r, v in zip(rows, vecs):
            db.execute(
                """INSERT OR REPLACE INTO event_vectors(event_id, model, dim, vec, created_at)
                   VALUES(?,?,?,?,?)""",
                (r["id"], model, int(len(v)), embedding.to_blob(v), db.now_iso()))
            embedded += 1

    return {"embedded": embedded, "failed": failed, "remaining": missing_vector_count(model)}


def _bg_worker(limit: int):
    try:
        r = backfill_vectors(limit=limit)
        logger.info("[embed] 后台补向量：+%s 条，剩余 %s 条", r["embedded"], r["remaining"])
    except Exception as e:
        logger.warning("[embed] 后台补向量失败: %s: %s", type(e).__name__, e)


def kick_backfill(limit: int = None) -> bool:
    """启动/每轮对话后触发一次后台补向量（不阻塞界面）"""
    global _BG_THREAD
    if not config.EMBED_ENABLED:
        return False
    with _BG_LOCK:
        if _BG_THREAD is not None and _BG_THREAD.is_alive():
            return False
        _BG_THREAD = threading.Thread(target=_bg_worker, args=(limit or config.EMBED_BACKFILL_MAX,),
                                      daemon=True, name="andy-embed-backfill")
        _BG_THREAD.start()
        return True


def retrieval_report(query_text: str, top_k: int = 3, exclude_recent: int = HISTORY_LIMIT * 2) -> dict:
    """对比三路结果（验证脚本用）"""
    kw = _keyword_candidates(query_text, exclude_recent, config.KEYWORD_TOPK, 0.35)
    vec, status = _vector_candidates(query_text, exclude_recent,
                                     config.EMBED_TOPK, config.EMBED_MIN_SCORE)
    return {"keyword": kw, "vector": vec,
            "hybrid": search_events(query_text, top_k=top_k, exclude_recent=exclude_recent),
            "vector_status": status, "missing": missing_vector_count()}


# ---------------- 读路径：上下文组装 ----------------

def _domain_for(intents: list):
    if "task" in intents or "reminder" in intents or "memory" in intents:
        return None  # 任务/记忆类需要全领域
    if "health" in intents:
        return "life"
    return None


def build_context(user_input: str, budget: int = CONTEXT_TOKEN_BUDGET) -> dict:
    """
    组装注入内容（不含 system prompt 与最近对话）
    返回 {"text", "stats", "sections"}
    """
    intents = detect_intents(user_input)
    sections = {}

    # ① 事实
    facts = get_facts(domain=_domain_for(intents), limit=12)
    if facts:
        sections["facts"] = "【关于用户】\n" + "\n".join(
            f"- {f['predicate']}: {f['object']}" for f in facts)

    # ② 今日状态 + 未完成任务编号（避免模型编造 id）
    try:
        from core import tasks
        ov = tasks.today_overview()
        lines = [
            "【今日状态】",
            f"今日到期 {ov['due_today']} 项 / 逾期 {ov['overdue']} 项 / "
            f"进行中 {ov['doing']} 项 / 今日提醒 {ov['reminders_today']} 条",
        ]
        brief = tasks.list_brief(limit=8)
        if brief:
            lines.append("未完成任务（用编号操作）：" + "；".join(
                f"#{b['id']} {b['title']}（{b['domain']}·{b['status']}）" for b in brief))
        sections["overview"] = "\n".join(lines)
    except Exception:
        pass

    # ③ 检索召回
    recall = search_events(user_input)
    if recall:
        sections["recall"] = "【可能相关的往事】\n" + "\n".join(
            f"- ({r['ts'][:10]}) {r['content'][:80]}" for r in recall)

    # ④ 预算裁剪：facts > overview > recall
    parts, used = [], 0
    for key in ("facts", "overview", "recall"):
        if key not in sections:
            continue
        cost = estimate_tokens(sections[key])
        if used + cost > budget:
            continue
        parts.append(sections[key])
        used += cost

    return {
        "text": "\n\n".join(parts),
        "stats": {"intents": intents, "tokens": used, "sections": list(sections.keys()),
                  "retrieval": last_retrieval(), "embed_missing": missing_vector_count()},
        "sections": sections,
    }


# ---------------- 用户画像（KV）-----------------

def set_profile(key: str, value: str) -> None:
    db.execute(
        """INSERT INTO user_profile(key, value, updated_at) VALUES(?,?,?)
           ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at""",
        (key, value, db.now_iso()))


def get_profile(key: str = None):
    if key:
        row = db.query_one("SELECT * FROM user_profile WHERE key=?", (key,))
        return row["value"] if row else None
    return {r["key"]: r["value"] for r in db.query("SELECT * FROM user_profile")}


def health_check() -> bool:
    return True


# ---------------- 兼容接口（健康模块 S5 预留）-----------------

def save_health_event(event_type: str, detail: str) -> int:
    """记录一条健康事件：先落事件流水，后续可单独建表"""
    return log_event("system", f"[health] {event_type}: {detail}", importance=0.6)


def get_user_profile() -> dict:
    return get_profile()


def add_reminder(content: str, remind_time: str):
    """兼容旧接口：默认按一次性提醒处理"""
    from core import scheduler
    return scheduler.create_reminder(content, "once", remind_time)
