"""
工具层 tools.py
Function Calling 的工具契约（OpenAI 兼容格式）+ 注册表
新增能力 = 这里加一个 schema + 一个函数，不改对话核心 => 扩展性落点
"""

import json

from core import tasks, scheduler, memory

# ---------------- 工具说明书 ----------------

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "task_create",
            "description": "新建一条任务。用户说「帮我记一下/安排/要做……」时调用。必须判断属于学习、工作还是生活。",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "任务标题，简短明确"},
                    "domain": {"type": "string", "enum": ["study", "work", "life"],
                               "description": "领域：study=学习，work=工作，life=生活"},
                    "priority": {"type": "integer", "enum": [1, 2, 3],
                                 "description": "1=高 2=中 3=低，默认 2"},
                    "due_at": {"type": "string",
                               "description": "截止时间，格式 YYYY-MM-DD HH:MM 或 YYYY-MM-DD，没有就不填"},
                    "notes": {"type": "string", "description": "补充说明，可省略"},
                },
                "required": ["title", "domain"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "task_list",
            "description": "查询任务列表。用户问「我今天要做什么/学习上有什么/进度怎样」时调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "domain": {"type": "string", "enum": ["study", "work", "life", "all"],
                               "description": "领域，all 表示全部"},
                    "status": {"type": "string", "enum": ["todo", "doing", "done", "all"],
                               "description": "状态，all 表示全部"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "task_update",
            "description": ("更新任务状态或进度。用户说「做完了/开始了/推到 70%」时调用。"
                            "优先使用上下文里给出的任务编号；没有编号就传 title 让系统按标题匹配，"
                            "绝对不要编造 task_id。"),
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "integer",
                                "description": "任务编号（上下文「未完成任务」里有）；不知道就留空"},
                    "title": {"type": "string", "description": "任务标题，用于按标题定位"},
                    "status": {"type": "string", "enum": ["todo", "doing", "done"],
                               "description": "新状态"},
                    "progress": {"type": "integer", "description": "进度 0-100"},
                    "due_at": {"type": "string", "description": "新的截止时间，格式 YYYY-MM-DD HH:MM"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "task_breakdown",
            "description": "把一个任务拆成若干子步骤。用户说「帮我拆一下/分步骤」时调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "integer", "description": "要拆分的父任务 id"},
                    "steps": {"type": "array", "items": {"type": "string"},
                              "description": "3-6 个可执行的小步骤"},
                },
                "required": ["task_id", "steps"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "reminder_create",
            "description": "设置定时提醒。用户说「几点提醒我/每天叫我/别忘了」时调用。时间必须换算成绝对格式。",
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {"type": "string", "description": "提醒内容"},
                    "rule_type": {"type": "string", "enum": ["once", "daily", "weekly"],
                                  "description": "once=一次性 daily=每天 weekly=每周"},
                    "rule_expr": {"type": "string",
                                  "description": "once: 'YYYY-MM-DD HH:MM'；daily: 'HH:MM'；weekly: '周几 HH:MM'（周几用 1-7，1=周一）"},
                    "task_id": {"type": "integer", "description": "关联任务 id，可省略"},
                },
                "required": ["content", "rule_type", "rule_expr"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "reminder_list",
            "description": "查看当前启用的提醒。用户问「我设了哪些提醒」时调用。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "overview_today",
            "description": "今日三线概览：到期、逾期、进行中、提醒数。用户问「今天怎么样/有什么要紧的」时调用。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "memory_save",
            "description": "记住用户的稳定事实（习惯、偏好、长期安排、身体状况）。只记长期有效的信息，不记一次性闲聊。",
            "parameters": {
                "type": "object",
                "properties": {
                    "predicate": {"type": "string",
                                  "description": "事实类型，如 name / preference / habit / health / schedule_anchor"},
                    "object": {"type": "string", "description": "事实内容，一句话"},
                    "domain": {"type": "string", "enum": ["study", "work", "life", "general"]},
                },
                "required": ["predicate", "object"],
            },
        },
    },
]


# ---------------- 工具实现 ----------------

def _tool_task_create(title, domain="life", priority=2, due_at=None, notes=""):
    r = tasks.create_task(title=title, domain=domain, priority=priority,
                          due_at=due_at, notes=notes, source="ai")
    if not r.get("ok"):
        return r
    t = r["task"]
    return {"ok": True, "id": t["id"], "title": t["title"], "domain": t["domain_text"],
            "due": t["due_at"], "priority": t["priority_text"],
            "reply_hint": f"已记入{t['domain_text']}：{t['title']}"}


def _tool_task_list(domain="all", status="all"):
    d = None if domain in (None, "", "all") else domain
    s = None if status in (None, "", "all") else status
    rows = tasks.list_tasks(domain=d, status=s, include_done=status in ("done", "all"))
    return {"ok": True, "count": len(rows),
            "tasks": [{"id": t["id"], "title": t["title"], "domain": t["domain_text"],
                       "status": t["status_text"], "progress": t["progress"],
                       "due": t["due_at"], "priority": t["priority_text"]} for t in rows[:30]]}


def _tool_task_update(task_id=None, title=None, status=None, progress=None, due_at=None):
    if task_id in (None, "", 0):
        if not title:
            return {"ok": False, "error": "需要任务编号或标题才能更新"}
        cands = tasks.find_by_title(title)
        if not cands:
            return {"ok": False, "error": f"没找到「{title}」对应的未完成任务"}
        if len(cands) > 1:
            return {"ok": False,
                    "error": "匹配到多条，请让用户确认编号",
                    "candidates": [{"id": c["id"], "title": c["title"]} for c in cands]}
        task_id = cands[0]["id"]
    r = tasks.update_task(int(task_id), status=status, progress=progress, due_at=due_at)
    if not r.get("ok"):
        return r
    t = r["task"]
    return {"ok": True, "id": t["id"], "title": t["title"], "status": t["status_text"],
            "progress": t["progress"], "due": t["due_at"]}


def _tool_task_breakdown(task_id, steps):
    if isinstance(steps, str):
        steps = [s for s in steps.replace("\n", "；").split("；") if s.strip()]
    return tasks.add_steps(int(task_id), steps)


def _tool_reminder_create(content, rule_type, rule_expr, task_id=None):
    return scheduler.create_reminder(content, rule_type, rule_expr,
                                     task_id=int(task_id) if task_id else None)


def _tool_reminder_list():
    rows = scheduler.list_reminders()
    return {"ok": True, "count": len(rows),
            "reminders": [{"id": r["id"], "content": r["content"], "rule": r["rule_desc"],
                           "next": r["next_fire_at"], "missed": r["missed_count"]} for r in rows]}


def _tool_overview_today():
    ov = tasks.today_overview()
    return {"ok": True, "date": ov["date"], "due_today": ov["due_today"],
            "overdue": ov["overdue"], "doing": ov["doing"],
            "reminders_today": ov["reminders_today"],
            "due_list": [{"id": t["id"], "title": t["title"]} for t in ov["due_today_list"]],
            "overdue_list": [{"id": t["id"], "title": t["title"]} for t in ov["overdue_list"]]}


def _tool_memory_save(predicate, obj, domain="general"):
    return memory.upsert_fact(predicate, obj, domain=domain)


TOOL_REGISTRY = {
    "task_create": _tool_task_create,
    "task_list": _tool_task_list,
    "task_update": _tool_task_update,
    "task_breakdown": _tool_task_breakdown,
    "reminder_create": _tool_reminder_create,
    "reminder_list": _tool_reminder_list,
    "overview_today": _tool_overview_today,
    "memory_save": _tool_memory_save,
}


def call_tool(name: str, args: dict) -> dict:
    """
    统一入口：任何工具异常都收敛成 {"ok": False, "error": ...} 回给模型，
    不让一次工具失败打断整轮对话
    """
    fn = TOOL_REGISTRY.get(name)
    if not fn:
        return {"ok": False, "error": f"未知工具 {name}"}
    try:
        result = fn(**(args or {}))
        return result if isinstance(result, dict) else {"ok": True, "result": result}
    except TypeError as e:
        return {"ok": False, "error": f"参数不匹配：{e}"}
    except Exception as e:
        return {"ok": False, "error": f"工具执行失败：{e}"}


def tools_json() -> str:
    return json.dumps(TOOLS, ensure_ascii=False)


def health_check() -> bool:
    return len(TOOL_REGISTRY) == len(TOOLS) and bool(TOOLS)
