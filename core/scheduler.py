"""
调度层 scheduler.py
- 提醒规则：once / daily / weekly
- 单一事实来源：reminders 表（APScheduler 只做"每 30 秒看一眼表"的心跳）
  这样进程重启后不需要恢复 jobstore，也不会出现"表里有、job 里没有"的双状态
- 启动补发：错过的提醒合并成一条补发，不刷屏
"""

import threading
from datetime import datetime, timedelta

from apscheduler.schedulers.background import BackgroundScheduler

from core import db

WEEK_TEXT = {1: "周一", 2: "周二", 3: "周三", 4: "周四", 5: "周五", 6: "周六", 7: "周日"}
WEEK_CN = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "日": 7, "天": 7, "七": 7}
TICK_SECONDS = 30
MISS_GRACE_MINUTES = 10      # 超过 10 分钟未触发算"错过"


def parse_week(token: str):
    """'1' / '周一' / '星期一' / '一' 都能解析成 1-7"""
    t = (token or "").strip()
    if t.isdigit():
        n = int(t)
        return n if 1 <= n <= 7 else None
    for prefix in ("星期", "周", "礼拜"):
        if t.startswith(prefix):
            t = t[len(prefix):]
    return WEEK_CN.get(t)


# ---------------- 规则计算 ----------------

def parse_expr(rule_type: str, rule_expr: str):
    """校验并解析规则，返回 (ok, 错误信息)"""
    rule_expr = (rule_expr or "").strip()
    if rule_type == "once":
        for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
            try:
                datetime.strptime(rule_expr, fmt)
                return True, None
            except ValueError:
                continue
        return False, "一次性提醒时间格式应为 YYYY-MM-DD HH:MM"
    if rule_type == "daily":
        try:
            datetime.strptime(rule_expr, "%H:%M")
            return True, None
        except ValueError:
            return False, "每日提醒时间格式应为 HH:MM"
    if rule_type == "weekly":
        parts = rule_expr.split()
        if len(parts) == 2 and parse_week(parts[0]):
            try:
                datetime.strptime(parts[1], "%H:%M")
                return True, None
            except ValueError:
                pass
        return False, "每周提醒格式应为 '周几 HH:MM'，如 '周一 09:00' 或 '1 09:00'"
    return False, f"未知规则类型 {rule_type}"


def compute_next(rule_type: str, rule_expr: str, after: datetime = None):
    """计算下一次触发时间（严格晚于 after）"""
    now = after or datetime.now()
    if rule_type == "once":
        try:
            dt = datetime.strptime(rule_expr, "%Y-%m-%d %H:%M")
        except ValueError:
            try:
                dt = datetime.strptime(rule_expr, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                return None
        return dt if dt > now else None

    if rule_type == "daily":
        hh, mm = [int(x) for x in rule_expr.split(":")]
        cand = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        return cand if cand > now else cand + timedelta(days=1)

    if rule_type == "weekly":
        parts = rule_expr.split()
        target = parse_week(parts[0])
        if not target:
            return None
        hh, mm = [int(x) for x in parts[1].split(":")]
        cand = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        delta = (target - now.isoweekday()) % 7
        if delta == 0 and cand <= now:
            delta = 7
        return cand + timedelta(days=delta)

    return None


def rule_text(rule_type: str, rule_expr: str) -> str:
    if rule_type == "once":
        return f"{rule_expr} 一次"
    if rule_type == "daily":
        return f"每天 {rule_expr}"
    if rule_type == "weekly":
        p = rule_expr.split()
        n = parse_week(p[0]) if p else None
        return f"每{WEEK_TEXT.get(n, p[0] if p else rule_expr)} {p[1] if len(p) > 1 else ''}".strip()
    return rule_expr


# ---------------- CRUD ----------------

def create_reminder(content: str, rule_type: str, rule_expr: str,
                    task_id: int = None) -> dict:
    content = (content or "").strip()
    if not content:
        return {"ok": False, "error": "提醒内容不能为空"}
    ok, err = parse_expr(rule_type, rule_expr)
    if not ok:
        return {"ok": False, "error": err}

    nxt = compute_next(rule_type, rule_expr)
    if nxt is None:
        return {"ok": False, "error": "这个时间已经过去了，请给一个未来时间"}

    rid = db.execute(
        """INSERT INTO reminders(content, rule_type, rule_expr, next_fire_at, enabled,
                                 task_id, created_at)
           VALUES(?,?,?,?,1,?,?)""",
        (content, rule_type, rule_expr.strip(), nxt.strftime("%Y-%m-%d %H:%M:%S"),
         task_id, db.now_iso()))
    return {"ok": True, "id": rid, "next_fire_at": nxt.strftime("%Y-%m-%d %H:%M"),
            "text": rule_text(rule_type, rule_expr)}


def list_reminders(enabled_only: bool = True) -> list:
    sql = "SELECT * FROM reminders"
    if enabled_only:
        sql += " WHERE enabled=1"
    sql += " ORDER BY COALESCE(next_fire_at,'9999') ASC"
    rows = db.query(sql)
    for r in rows:
        r["rule_desc"] = rule_text(r["rule_type"], r["rule_expr"])
    return rows


def set_enabled(reminder_id: int, enabled: bool) -> dict:
    if enabled:
        row = db.query_one("SELECT * FROM reminders WHERE id=?", (int(reminder_id),))
        if not row:
            return {"ok": False, "error": "提醒不存在"}
        nxt = compute_next(row["rule_type"], row["rule_expr"])
        if nxt is None:
            return {"ok": False, "error": "规则时间已过期，无法启用"}
        db.execute("UPDATE reminders SET enabled=1, next_fire_at=? WHERE id=?",
                   (nxt.strftime("%Y-%m-%d %H:%M:%S"), int(reminder_id)))
    else:
        db.execute("UPDATE reminders SET enabled=0 WHERE id=?", (int(reminder_id),))
    return {"ok": True, "id": int(reminder_id), "enabled": enabled}


def delete_reminder(reminder_id: int) -> dict:
    row = db.query_one("SELECT content FROM reminders WHERE id=?", (int(reminder_id),))
    if not row:
        return {"ok": False, "error": "提醒不存在"}
    db.execute("DELETE FROM reminders WHERE id=?", (int(reminder_id),))
    return {"ok": True, "deleted": row["content"]}


def reminders_today() -> list:
    today = datetime.now().strftime("%Y-%m-%d")
    return [r for r in list_reminders() if (r["next_fire_at"] or "").startswith(today)]


# ---------------- 调度器 ----------------

class Scheduler:
    """心跳式调度器：每 TICK_SECONDS 查一次 reminders 表"""

    def __init__(self, on_fire=None):
        self.on_fire = on_fire or (lambda payload: None)
        self._sched = BackgroundScheduler(daemon=True)
        self._lock = threading.Lock()
        self.started = False

    def start(self, catch_up: bool = True):
        if self.started:
            return
        self._sched.add_job(self._tick, "interval", seconds=TICK_SECONDS,
                            id="reminder_tick", max_instances=1, coalesce=True)
        self._sched.start()
        self.started = True
        if catch_up:
            self._catch_up()

    def shutdown(self):
        if self.started:
            self._sched.shutdown(wait=False)
            self.started = False

    # -- 启动补发 --
    def _catch_up(self):
        now = datetime.now()
        overdue = db.query(
            "SELECT * FROM reminders WHERE enabled=1 AND next_fire_at<? ORDER BY next_fire_at",
            (now.strftime("%Y-%m-%d %H:%M:%S"),))
        if not overdue:
            return
        missed, late = [], []
        for r in overdue:
            last = datetime.strptime(r["next_fire_at"], "%Y-%m-%d %H:%M:%S")
            gap_min = (now - last).total_seconds() / 60
            if gap_min > MISS_GRACE_MINUTES:
                self._bump_missed(r, last, now)
                missed.append(r["content"])
            else:
                late.append(r["content"])          # 10 分钟内也补发，不静默吞掉
            self._advance(r, now)
        if missed or late:
            parts = []
            if missed:
                parts.append("错过：" + "；".join(missed[:5]))
            if late:
                parts.append("刚到点：" + "；".join(late[:5]))
            text = "启动时补发提醒（" + " / ".join(parts) + "）"
            if len(missed) > 5 or len(late) > 5:
                text += f" 共 {len(missed) + len(late)} 条"
            self.on_fire({"type": "catch_up", "content": text,
                          "items": missed + late})

    def _bump_missed(self, reminder: dict, last_fire: datetime, now: datetime):
        if reminder["rule_type"] == "daily":
            n = max((now.date() - last_fire.date()).days - 1, 0)
        elif reminder["rule_type"] == "weekly":
            n = max(((now.date() - last_fire.date()).days - 1) // 7, 0)
        else:
            n = 1
        db.execute("UPDATE reminders SET missed_count=missed_count+? WHERE id=?",
                   (n, reminder["id"]))

    def _advance(self, reminder: dict, now: datetime):
        nxt = compute_next(reminder["rule_type"], reminder["rule_expr"], after=now)
        if nxt is None:
            db.execute("UPDATE reminders SET enabled=0, last_fired_at=?, next_fire_at=NULL WHERE id=?",
                       (db.now_iso(), reminder["id"]))
        else:
            db.execute(
                "UPDATE reminders SET last_fired_at=?, next_fire_at=? WHERE id=?",
                (db.now_iso(), nxt.strftime("%Y-%m-%d %H:%M:%S"), reminder["id"]))

    # -- 心跳 --
    def _tick(self):
        if not self._lock.acquire(blocking=False):
            return
        try:
            now = datetime.now()
            due = db.query(
                "SELECT * FROM reminders WHERE enabled=1 AND next_fire_at<=? ORDER BY next_fire_at",
                (now.strftime("%Y-%m-%d %H:%M:%S"),))
            for r in due:
                self._advance(r, now)
                self.on_fire({
                    "type": "reminder",
                    "id": r["id"],
                    "content": r["content"],
                    "rule": rule_text(r["rule_type"], r["rule_expr"]),
                    "task_id": r["task_id"],
                })
        except Exception as e:  # 心跳不允许因单条异常整体停摆
            print(f"[scheduler] tick error: {e}")
        finally:
            self._lock.release()

    def fire_now(self, reminder_id: int):
        """立即触发一次（GUI「测试提醒」按钮用）"""
        r = db.query_one("SELECT * FROM reminders WHERE id=?", (int(reminder_id),))
        if r:
            self.on_fire({"type": "reminder", "id": r["id"], "content": r["content"],
                          "rule": rule_text(r["rule_type"], r["rule_expr"]),
                          "task_id": r["task_id"]})


def health_check() -> bool:
    try:
        ok_daily = parse_expr("daily", "07:30")[0]
        db.query("SELECT COUNT(*) AS c FROM reminders")
        return ok_daily
    except Exception:
        return False


if __name__ == "__main__":
    from core import db as _db
    _db.init_db()
    print("提醒规则自检：")
    for rt, re_ in [("once", "2026-12-01 09:00"), ("daily", "07:30"), ("weekly", "1 09:00")]:
        print(f"  {rt:6s} {re_:16s} -> {compute_next(rt, re_)}")
