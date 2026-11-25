"""
任务层 tasks.py
三线任务（study/work/life）的增删改查 + 今日概览
状态机：todo -> doing -> done（可回退）
"""

from datetime import datetime, date, timedelta

from config import DOMAIN_KEYS
from core import db

VALID_STATUS = {"todo", "doing", "done"}
STATUS_TEXT = {"todo": "待办", "doing": "进行中", "done": "已完成"}
PRIORITY_TEXT = {1: "高", 2: "中", 3: "低"}


def normalize_due(due_at):
    """把各种时间写法统一成 'YYYY-MM-DD HH:MM' / 'YYYY-MM-DD'；无法识别返回 None"""
    if not due_at:
        return None
    if isinstance(due_at, (datetime, date)):
        return due_at.strftime("%Y-%m-%d %H:%M") if isinstance(due_at, datetime) else due_at.isoformat()
    t = str(due_at).strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d",
                "%Y/%m/%d %H:%M", "%Y/%m/%d", "%Y年%m月%d日 %H:%M", "%Y年%m月%d日"):
        try:
            return datetime.strptime(t, fmt).strftime("%Y-%m-%d %H:%M" if "%H" in fmt else "%Y-%m-%d")
        except ValueError:
            continue
    return None


def _row(task: dict) -> dict:
    """补充可读字段"""
    if not task:
        return task
    task["domain_text"] = {"study": "学习", "work": "工作", "life": "生活"}.get(task["domain"], task["domain"])
    task["status_text"] = STATUS_TEXT.get(task["status"], task["status"])
    task["priority_text"] = PRIORITY_TEXT.get(task["priority"], str(task["priority"]))
    return task


def create_task(title: str, domain: str = "life", priority: int = 2, due_at=None,
                notes: str = "", parent_id: int = None, source: str = "manual") -> dict:
    """新建任务，返回任务详情（含 id）"""
    title = (title or "").strip()
    if not title:
        return {"ok": False, "error": "任务标题不能为空"}
    if domain not in DOMAIN_KEYS:
        domain = "life"
    try:
        priority = int(priority)
    except (TypeError, ValueError):
        priority = 2
    priority = min(max(priority, 1), 3)
    due = normalize_due(due_at)

    tid = db.execute(
        """INSERT INTO tasks(title, domain, status, priority, due_at, progress,
                             parent_id, source, notes, created_at, updated_at)
           VALUES(?,?,'todo',?,?,0,?,?,?,?,?)""",
        (title, domain, priority, due, parent_id, source, notes,
         db.now_iso(), db.now_iso()))
    db.execute("INSERT INTO task_events(task_id, event_type, payload, ts) VALUES(?,?,?,?)",
               (tid, "created", f"source={source}", db.now_iso()))
    return {"ok": True, "id": tid, "task": _row(get_task(tid))}


def get_task(task_id: int):
    return _row(db.query_one("SELECT * FROM tasks WHERE id=?", (int(task_id),)))


def list_tasks(domain: str = None, status: str = None, include_done: bool = False,
               limit: int = 100) -> list:
    """按领域/状态查询；默认不含已完成"""
    sql = "SELECT * FROM tasks WHERE 1=1"
    params = []
    if domain:
        sql += " AND domain=?"
        params.append(domain)
    if status:
        sql += " AND status=?"
        params.append(status)
    elif not include_done:
        sql += " AND status<>'done'"
    sql += " ORDER BY status='done', priority ASC, COALESCE(due_at,'9999') ASC, id DESC LIMIT ?"
    params.append(limit)
    rows = db.query(sql, tuple(params))
    return [_row(r) for r in rows]


def update_task(task_id: int, status: str = None, progress: int = None, due_at=None,
                priority: int = None, notes: str = None, title: str = None,
                domain: str = None) -> dict:
    """更新任务字段，返回最新任务"""
    task = db.query_one("SELECT * FROM tasks WHERE id=?", (int(task_id),))
    if not task:
        return {"ok": False, "error": f"任务 {task_id} 不存在"}

    sets, params, events = [], [], []
    if status is not None:
        if status not in VALID_STATUS:
            return {"ok": False, "error": f"状态只能是 {VALID_STATUS}"}
        sets.append("status=?")
        params.append(status)
        events.append(("status", f"{task['status']}->{status}"))
        if status == "done":
            sets.append("progress=?")
            params.append(100)
        elif task["status"] == "done" and status != "done":
            sets.append("progress=?")
            params.append(min(task["progress"], 90))
    if progress is not None:
        try:
            p = min(max(int(progress), 0), 100)
        except (TypeError, ValueError):
            return {"ok": False, "error": "进度需为 0-100 的整数"}
        sets.append("progress=?")
        params.append(p)
        if p == 100 and status is None:
            sets.append("status=?")
            params.append("done")
    if due_at is not None:
        sets.append("due_at=?")
        params.append(normalize_due(due_at))
    if priority is not None:
        sets.append("priority=?")
        params.append(min(max(int(priority), 1), 3))
    if notes is not None:
        sets.append("notes=?")
        params.append(str(notes))
    if title is not None and str(title).strip():
        sets.append("title=?")
        params.append(str(title).strip())
    if domain is not None:
        if domain not in DOMAIN_KEYS:
            return {"ok": False, "error": f"领域只能是 {DOMAIN_KEYS}"}
        sets.append("domain=?")
        params.append(domain)

    if not sets:
        return {"ok": True, "task": _row(task), "changed": False}

    sets.append("updated_at=?")
    params.append(db.now_iso())
    params.append(int(task_id))
    db.execute(f"UPDATE tasks SET {', '.join(sets)} WHERE id=?", tuple(params))
    for etype, payload in events:
        db.execute("INSERT INTO task_events(task_id, event_type, payload, ts) VALUES(?,?,?,?)",
                   (int(task_id), etype, payload, db.now_iso()))
    return {"ok": True, "task": _row(get_task(task_id)), "changed": True}


def delete_task(task_id: int) -> dict:
    row = db.query_one("SELECT title FROM tasks WHERE id=?", (int(task_id),))
    if not row:
        return {"ok": False, "error": "任务不存在"}
    db.execute("DELETE FROM tasks WHERE id=?", (int(task_id),))
    return {"ok": True, "deleted": row["title"]}


def add_steps(task_id: int, steps: list) -> dict:
    """给任务加子步骤（建为子任务）"""
    parent = get_task(task_id)
    if not parent:
        return {"ok": False, "error": "父任务不存在"}
    created = []
    for s in steps[:8]:
        s = (s or "").strip()
        if not s:
            continue
        r = create_task(s, domain=parent["domain"], priority=parent["priority"],
                        due_at=parent["due_at"], parent_id=parent["id"], source="ai")
        if r.get("ok"):
            created.append(r["task"])
    return {"ok": True, "count": len(created), "steps": created}


def today_overview() -> dict:
    """今日概览：到期/逾期/进行中/提醒数 + 明细分组"""
    today = date.today().isoformat()
    all_open = list_tasks(include_done=False, limit=200)
    due_today = [t for t in all_open if t["due_at"] and t["due_at"][:10] == today]
    overdue = [t for t in all_open if t["due_at"] and t["due_at"][:10] < today]
    doing = [t for t in all_open if t["status"] == "doing"]

    reminders_today = db.query(
        "SELECT COUNT(*) AS c FROM reminders WHERE enabled=1 AND next_fire_at LIKE ?",
        (f"{today}%",))[0]["c"]

    upcoming = [t for t in all_open
                if t["due_at"] and today < t["due_at"][:10] <= (date.today() + timedelta(days=3)).isoformat()]

    counts = {d: {"total": 0, "done": 0} for d in DOMAIN_KEYS}
    for r in db.query("SELECT domain, status, COUNT(*) AS c FROM tasks GROUP BY domain, status"):
        d = r["domain"] if r["domain"] in counts else "life"
        counts[d]["total"] += r["c"]
        if r["status"] == "done":
            counts[d]["done"] += r["c"]

    return {
        "date": today,
        "due_today": len(due_today),
        "overdue": len(overdue),
        "doing": len(doing),
        "reminders_today": reminders_today,
        "due_today_list": due_today[:10],
        "overdue_list": overdue[:10],
        "upcoming_list": upcoming[:10],
        "counts": counts,
    }


def overview_text() -> str:
    """给人看的今日概览文本（注入 prompt / 显示在状态栏）"""
    ov = today_overview()
    parts = [f"今日({ov['date']})：到期 {ov['due_today']} · 逾期 {ov['overdue']} · "
             f"进行中 {ov['doing']} · 提醒 {ov['reminders_today']}"]
    if ov["overdue_list"]:
        parts.append("逾期：" + "、".join(
            f"[{t['id']}]{t['title']}" for t in ov["overdue_list"][:5]))
    if ov["due_today_list"]:
        parts.append("今天到期：" + "、".join(
            f"[{t['id']}]{t['title']}" for t in ov["due_today_list"][:5]))
    if not (ov["overdue"] or ov["due_today"] or ov["doing"]):
        parts.append("三线暂时都没有压着的任务。")
    return "\n".join(parts)


def health_check() -> bool:
    """任务表可读即视为健康"""
    try:
        db.query("SELECT COUNT(*) AS c FROM tasks")
        return True
    except Exception:
        return False
