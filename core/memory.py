"""
记忆层 memory.py
- L2 情景记忆：chat_events（append-only）
- L3 语义记忆：semantic_facts（规则蒸馏 + 去重）
- 读路径：build_context() 按 token 预算组装注入内容
设计依据：docs/MEMORY_DESIGN.md
"""

import re
import threading
from datetime import datetime
from difflib import SequenceMatcher

import numpy as np

import config
from config import SESSION_ID, HISTORY_LIMIT, CONTEXT_TOKEN_BUDGET
from console import logger
from core import db, embedding

# ---------------- 规则：事实抽取 ----------------

# (predicate, domain, 触发关键词, 取值方向)
# after  = 关键词后面才是内容（"我叫 Wen"）
# before = 关键词前面才是内容（"我对花生过敏"）
FACT_RULES = [
    ("name", "general", ["我叫", "我的名字是", "叫我"], "after"),
    ("allergy", "life", ["过敏"], "before"),
    ("chronic", "life", ["慢性病", "老毛病"], "after"),
    ("exam_anchor", "study", ["考试", "答辩", "面试"], "after"),
    ("work_anchor", "work", ["截止", "deadline", "交付", "汇报"], "after"),
    ("preference", "general", ["我不喜欢", "我喜欢", "别对我", "不要对我"], "after"),
    ("habit_study", "study", ["每天.*学", "固定.*复习"], "after"),
    ("habit_life", "life", ["每天.*睡", "作息"], "after"),
]

# 抽取前要剥掉的"口头前缀"，避免把"我对/记一下/是不是"存成事实内容
_OBJ_PREFIX = re.compile(
    r"^(?:我|本人)?(?:记一下|记一笔|顺便说一句|另外|对了)?"
    r"(?:是不是|是否|到底)?(?:对|吃了|吃|喝了|喝|碰了|碰|用了|用)?")


def _clean_obj(raw: str, direction: str = "after") -> str:
    """去掉标点尾巴与口语前缀，保留事实主体"""
    parts = [p.strip(" 　") for p in re.split(r"[，。！？,.;；:：]", raw) if p.strip(" 　")]
    if not parts:
        return ""
    # after：关键词后面第一段就是内容；before：离关键词最近的一段才是主体
    obj = parts[0] if direction == "after" else parts[-1]
    for _ in range(3):                        # 最多剥三层，如"我是不是对花生"
        new = _OBJ_PREFIX.sub("", obj).strip(" 　")
        if new == obj:
            break
        obj = new
    return obj.strip(" 　")


_QUESTION_PAT = re.compile(r"(是不是|是否|吗|有没有|还记得|？|\?)")


def _is_question(text: str) -> bool:
    """问句只用于查询，不能反向写成事实"""
    return bool(_QUESTION_PAT.search(text or ""))

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
    if _is_question(text):
        return []          # "我是不是对花生过敏"是查询，不能反写成新事实
    results = []
    for predicate, domain, kws, direction in FACT_RULES:
        for kw in kws:
            if ".*" in kw:
                m = re.search(kw, text)
                if not m:
                    continue
                before, after = text[:m.start()], text[m.end():]
            else:
                if kw not in text:
                    continue
                before, after = text.split(kw, 1)
            # direction=before 时优先取关键词前面的主体（过敏原），取不到再退回后面
            candidates = [before, after] if direction == "before" else [after, before]
            obj = ""
            for raw in candidates:
                cand = _clean_obj(raw, direction)
                if 1 <= len(cand) <= 60:
                    obj = cand
                    break
            if obj:
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
    加载最近 limit 条用户侧向量（带进程内缓存；事件新增后自动失效重载）
    - 只取 role in ('user','system')：助手回复是用户话的复述，混进来会挤掉原话
    - 语料够多时做“去均值”中心化：embedding-3 中文向量有明显公共方向，
      直接算余弦时无关句也有 0.5+；减去均值后相关与无关才拉得开
    - 语料太少（< EMBED_MIN_CORPUS）时去均值会把信号抹成 0，此时退化为原始余弦
    返回 (ids, mat, mean, centered)
    """
    stat = db.query_one(
        "SELECT COUNT(*) AS c, MAX(event_id) AS m FROM event_vectors WHERE model=?", (model,))
    key = (model, stat["c"] if stat else 0, stat["m"] if stat else None)
    if _VEC_CACHE["key"] == key and _VEC_CACHE["ids"] is not None:
        return (_VEC_CACHE["ids"], _VEC_CACHE["mat"], _VEC_CACHE["mean"],
                _VEC_CACHE.get("centered", True))

    rows = db.query(
        """SELECT v.event_id, v.dim, v.vec FROM event_vectors v
           JOIN chat_events e ON e.id=v.event_id
           WHERE v.model=? AND e.role IN ('user','system')
           ORDER BY e.id DESC LIMIT ?""", (model, limit))
    if not rows:
        _VEC_CACHE.update({"key": key, "ids": [], "mat": None, "mean": None,
                           "centered": False})
        return [], None, None, False

    ids = [r["event_id"] for r in rows]
    raw = np.vstack([embedding.from_blob(r["vec"], r["dim"]) for r in rows])
    raw_norm = raw / np.maximum(np.linalg.norm(raw, axis=1, keepdims=True), 1e-8)

    centered = len(ids) >= config.EMBED_MIN_CORPUS
    if centered:
        mean = raw.mean(axis=0)
        mat = raw - mean
        mat = mat / np.maximum(np.linalg.norm(mat, axis=1, keepdims=True), 1e-8)
    else:
        mean, mat = None, raw_norm
    _VEC_CACHE.update({"key": key, "ids": ids, "mat": mat, "mean": mean,
                       "centered": centered})
    return ids, mat, mean, centered


def _keyword_candidates(query_text: str, exclude_recent: int, top: int, threshold: float) -> list:
    rows = db.query(
        """SELECT * FROM chat_events WHERE role IN ('user','system')
           ORDER BY id DESC LIMIT 300""")
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

    ids, mat, mean, centered = _load_vectors(config.EMBED_MODEL)
    if not ids:
        return [], "empty"

    qn = np.asarray(qv, dtype=np.float32)
    if centered and mean is not None:
        qc = qn - mean
        qnorm = float(np.linalg.norm(qc))
        if qnorm < 1e-8:                 # 查询向量与语料均值重合，无区分度
            return [], "ok"
        q = qc / qnorm
        eff_min = min_score
    else:
        # 小语料不做中心化，公共方向还在，阈值必须抬高
        qnorm = float(np.linalg.norm(qn))
        if qnorm < 1e-8:
            return [], "ok"
        q = qn / qnorm
        eff_min = max(min_score, config.EMBED_SMALL_SCORE)

    scores = mat @ q
    recent = _recent_event_ids(exclude_recent)
    ranked = [(i, float(s)) for i, s in zip(ids, scores) if i not in recent]
    if not ranked:
        return [], "ok"

    # 关键：最高分只能取自"可召回"集合。若把最近事件计入 best，
    # cutoff 会被抬到只有那几条命中，导致老记忆全部被误过滤
    ranked.sort(key=lambda x: -x[1])
    best = ranked[0][1]
    cutoff = max(eff_min, best - config.EMBED_MARGIN)
    picked = [x for x in ranked if x[1] >= cutoff][:top]
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


def _recency_weight(ts: str) -> float:
    """
    轻量时间衰减：半衰期 120 天，最低 0.35
    - 只用来打破"同题多句"的排序，不让三个月前的事彻底消失
    - ts 解析失败（测试数据）按 1.0 处理
    """
    try:
        dt = datetime.fromisoformat((ts or "").replace("Z", ""))
    except (ValueError, TypeError):
        return 1.0
    age_days = max((datetime.now() - dt).total_seconds() / 86400.0, 0.0)
    hl = max(config.RECENCY_HALF_LIFE_DAYS, 1e-6)
    return max(0.5 ** (age_days / hl), config.RECENCY_MIN_WEIGHT)


def _rrf_merge(lists: list, k: int = config.RRF_K, query: str = "") -> list:
    """
    融合排序：语义分为主，RRF 与时间只做小幅修正
    - 只用 RRF 排序时，单路候选等同于按名次排，分数差距被抹平：
      一个 0.03 的语义优势会被 40 天的"旧"直接压到第二
    - 所以：score 先按各路最高分归一化，占 75%；RRF 占 25%；再乘 0.9~1.0 的时间因子
    """
    fused = {}
    best_by_tag = {}
    for tag, items in lists:
        best_by_tag[tag] = max((item.get(f"{tag}_score") or 0.0 for item in items),
                               default=0.0) or 1.0

    for tag, items in lists:
        for rank, item in enumerate(items):
            node = fused.setdefault(item["id"], {"id": item["id"], "rrf": 0.0,
                                                 "content": item["content"], "role": item["role"],
                                                 "ts": item["ts"], "via": set(),
                                                 "vector_score": None, "keyword_score": None,
                                                 "v_norm": 0.0, "k_norm": 0.0})
            node["rrf"] += 1.0 / (k + rank + 1)
            node["via"].add(tag)
            raw = item.get(f"{tag}_score") or 0.0
            norm = min(max(raw / best_by_tag[tag], 0.0), 1.0)
            if tag == "vector":
                node["vector_score"] = item.get("vector_score")
                node["v_norm"] = norm
            else:
                node["keyword_score"] = item.get("keyword_score")
                node["k_norm"] = norm

    out = list(fused.values())
    max_rrf = max((n["rrf"] for n in out), default=1.0) or 1.0
    for n in out:
        n["score"] = round(max(n["vector_score"] or 0.0, n["keyword_score"] or 0.0), 2)
        n["via"] = "both" if len(n["via"]) > 1 else next(iter(n["via"]))
        n["recency"] = round(_recency_weight(n["ts"]), 2)
        sim = max(n.pop("v_norm"), n.pop("k_norm"))
        n["sim"] = round(sim, 3)
        base = 0.75 * sim + 0.25 * (n["rrf"] / max_rrf)
        base += _intent_boost(query, n["content"])
        n["rank_score"] = round(base * (0.9 + 0.1 * n["recency"]), 6)
    out.sort(key=lambda n: -n["rank_score"])
    return out


INTENT_WORDS = {
    "health": ["累", "头疼", "难受", "失眠", "焦虑", "状态", "睡", "困", "不舒服", "身体"],
    "food": ["吃", "饭", "外卖", "食堂", "过敏", "花生", "芒果", "牛奶"],
    "work": ["实习", "面试", "工作", "简历", "公司", "入职"],
    "study": ["学习", "考试", "答辩", "论文", "复习", "作业", "实验"],
    "money": ["钱", "预算", "花费", "工资", "生活费"],
}


def _intent_boost(query: str, content: str) -> float:
    """
    问句和内容落在同一主题时给一点加分：
    纯语义分数对"累/天气"这类短句区分力弱，靠主题词把明显该中的那条拉回来
    """
    if not query or not content:
        return 0.0
    best = 0.0
    for words in INTENT_WORDS.values():
        hits = sum(1 for w in words if w in query)
        if hits and any(w in content for w in words):
            best = max(best, config.RECALL_INTENT_BOOST)
    # 吃/过敏是安全相关：宁可多召回过敏原原话，也不让"晚上吃什么"这类闲聊压过去
    if ("过敏" in query or "吃" in query) and "过敏" in content:
        best = max(best, 0.16)
    return best


def _near_duplicate(a: str, b: str) -> bool:
    """两句内容近义（助理复述/标点差异）视为同一条记忆"""
    na = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", a or "")
    nb = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", b or "")
    if not na or not nb:
        return False
    if na == nb:
        return True
    shorter, longer = sorted((na, nb), key=len)
    if shorter in longer and len(longer) - len(shorter) <= 10:
        return True
    return SequenceMatcher(None, na, nb).ratio() >= 0.85


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

    fused = _rrf_merge(lists, query=query_text)
    # 同义句去重：保留排序更靠前的那条（用户原话优先于助理复述）
    kept = []
    for item in fused:
        if any(_near_duplicate(item["content"], k["content"]) for k in kept):
            continue
        kept.append(item)

    # 语义门槛：跟最高分差太多的候选不往上下文里塞（宁少勿滥）
    if kept:
        best_sim = max(item["sim"] for item in kept) or 1.0
        kept = [item for item in kept
                if item["sim"] >= config.RECALL_SIM_GATE * best_sim or item["via"] == "both"]

    _LAST_RETRIEVAL.update({"mode": "hybrid" if len(lists) == 2 else lists[0][0],
                            "vector_status": status, "keyword": len(kw), "vector": len(vec)})
    return kept[:top_k]


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
           WHERE e.role IN ('user','system')
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
               WHERE e.role IN ('user','system')
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


# 事实的中文标签：直接塞 name: Wen 这种英文键，模型容易看不懂或答错身份
FACT_LABELS = {
    "name": "称呼",
    "allergy": "过敏源",
    "chronic": "慢性病",
    "exam_anchor": "考试/答辩节点",
    "work_anchor": "交付节点",
    "preference": "偏好",
    "habit_study": "学习习惯",
    "habit_life": "作息习惯",
    "health": "身体状况",
    "schedule_anchor": "长期安排",
}


def _fact_line(f: dict) -> str:
    label = FACT_LABELS.get(f["predicate"], f["predicate"])
    return f"- {label}：{f['object']}"


def build_context(user_input: str, budget: int = CONTEXT_TOKEN_BUDGET) -> dict:
    """
    组装注入内容（不含 system prompt 与最近对话）
    返回 {"text", "stats", "sections"}
    """
    intents = detect_intents(user_input)
    sections = {}

    # ① 事实（称呼永远保留：被一堆偏好挤掉会导致"我是谁"答不出来）
    name_facts = db.query(
        "SELECT * FROM semantic_facts WHERE predicate='name' ORDER BY updated_at DESC LIMIT 2")
    others = [f for f in get_facts(domain=_domain_for(intents), limit=11)
              if f["predicate"] != "name"]
    facts = list(name_facts) + others
    if facts:
        sections["facts"] = "【关于用户】\n" + "\n".join(_fact_line(f) for f in facts[:12])

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
