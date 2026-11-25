"""
记忆层 memory.py
- L2 情景记忆：chat_events（append-only）
- L3 语义记忆：semantic_facts（规则蒸馏 + 去重）
- 读路径：build_context() 按 token 预算组装注入内容
设计依据：docs/MEMORY_DESIGN.md
"""

import re
from difflib import SequenceMatcher

from config import SESSION_ID, HISTORY_LIMIT, CONTEXT_TOKEN_BUDGET
from core import db

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


# ---------------- 检索（MVP：字符重合度打分，零 embedding 成本）-----------------

def _score(query_text: str, content: str) -> float:
    q = set(re.findall(r"[\u4e00-\u9fff]|[a-zA-Z]{2,}", query_text))
    if not q:
        return 0.0
    c = set(re.findall(r"[\u4e00-\u9fff]|[a-zA-Z]{2,}", content))
    return len(q & c) / len(q)


def search_events(query_text: str, top_k: int = 3, threshold: float = 0.35,
                  exclude_recent: int = HISTORY_LIMIT * 2) -> list:
    """从历史事件召回相关片段（排除最近窗口内已注入的）"""
    rows = db.query(
        "SELECT * FROM chat_events WHERE role IN ('user','assistant') ORDER BY id DESC LIMIT 300"
    )
    rows = rows[exclude_recent:] if len(rows) > exclude_recent else []
    scored = [(_score(query_text, m["content"]), m) for m in rows]
    scored = [(s, m) for s, m in scored if s >= threshold]
    scored.sort(key=lambda x: (-x[0], -x[1]["id"]))
    return [{"content": m["content"], "role": m["role"], "ts": m["ts"], "score": round(s, 2)}
            for s, m in scored[:top_k]]


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
        "stats": {"intents": intents, "tokens": used, "sections": list(sections.keys())},
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
