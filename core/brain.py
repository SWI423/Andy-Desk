"""
对话核心 brain.py
流程：用户输入 → 时间硬拦截 → 记事件/抽事实 → 组装上下文 → 流式对话（含工具循环）→ 落库
"""

import json
import re
from datetime import datetime

from openai import OpenAI

from config import (ZHIPU_API_KEY, ZHIPU_BASE_URL, ZHIPU_CHAT_MODEL,
                    SESSION_ID, MAX_TOOL_ROUNDS, HISTORY_LIMIT)
from console import logger
from core import memory, tools

WEEK_TEXT = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]

_CLIENT = None


def _client() -> OpenAI:
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = OpenAI(api_key=ZHIPU_API_KEY, base_url=ZHIPU_BASE_URL, timeout=90)
    return _CLIENT


def current_time_text() -> str:
    now = datetime.now()
    return f"现在是 {now.year}年{now.month}月{now.day}日 {WEEK_TEXT[now.weekday()]} {now.strftime('%H:%M')}"


# 基本问答校准：日期/星期/时间这种“查表就能答”的问题，不交给模型猜
_WEEKDAY_SET = {"今天星期几", "今天周几", "今天星期几呀", "今天周几呀",
                "现在星期几", "现在是星期几", "当前星期几", "星期几",
                "今天是什么日子", "今天是星期几"}
_DATE_SET = {"今天几号", "今天多少号", "今天日期", "今天的日期",
             "今天几月几号", "现在几号", "现在是几号", "现在日期",
             "当前日期", "今天是几号"}
_TIME_SET = {"现在几点", "现在几点钟", "现在时间", "当前时间",
             "现在是什么时间", "几点", "几点了", "时间",
             "现在是几点", "现在是几点钟", "现在几点了", "现在是几点了"}
_BASIC_BLOCK = ("提醒", "叫我", "闹钟", "安排", "记下", "记住", "明天",
                "昨天", "后天", "下周", "上周", "周末")

_FULL_TIME = re.compile(
    r"(现在是|今天是|当前是)\s*(\d{4})年(\d{1,2})月(\d{1,2})日"
    r"\s*(星期[一二三四五六日天])(?:\s*(\d{1,2}:\d{2}))?")
_WEEK_ONLY = re.compile(r"今天(是)?(星期[一二三四五六日天])")
_CONDITIONAL = ("如果", "假如", "假设", "要是", "若", "不是", "并非", "除非")


def _is_conditional(text: str, start: int) -> bool:
    return any(k in text[max(0, start - 6):start] for k in _CONDITIONAL)


def answer_basic_query(text: str, now=None):
    """只处理短句里的日期/星期/时间；返回 None 表示交给正常对话"""
    norm = re.sub(r"[？?。！!，,、\s]", "", text or "")
    if not norm or len(norm) > 16:
        return None
    if any(k in norm for k in _BASIC_BLOCK):
        return None
    now = now or datetime.now()
    week = WEEK_TEXT[now.weekday()]
    date_text = f"{now.year}年{now.month}月{now.day}日"
    if norm in _WEEKDAY_SET:
        return f"今天是 {date_text}，{week}。"
    if norm in _DATE_SET:
        return f"今天是 {date_text}，{week}。"
    if norm in _TIME_SET:
        return f"现在是 {date_text} {week} {now.strftime('%H:%M')}。"
    return None


def calibrate_reply_time(reply: str, now=None) -> str:
    """模型偶尔会无视注入的时间；把带今天日期的错误星期/时间改回来"""
    text = reply or ""
    now = now or datetime.now()

    def full_repl(m):
        if _is_conditional(text, m.start()):
            return m.group(0)
        try:
            dt = datetime(int(m.group(2)), int(m.group(3)), int(m.group(4)))
        except ValueError:
            return m.group(0)
        week = WEEK_TEXT[dt.weekday()]
        tail_time = f" {now.strftime('%H:%M')}" if (
            m.group(6) and dt.date() == now.date()) else (
            f" {m.group(6)}" if m.group(6) else "")
        return (f"{m.group(1)} {dt.year}年{dt.month}月{dt.day}日 "
                f"{week}{tail_time}")

    def week_repl(m):
        if _is_conditional(text, m.start()):
            return m.group(0)
        return f"今天{m.group(1) or ''}{WEEK_TEXT[now.weekday()]}"

    new = _FULL_TIME.sub(full_repl, text)
    new = _WEEK_ONLY.sub(week_repl, new)
    if new != text:
        logger.info("[brain] 校准时间表述：%s -> %s", text[:60], new[:60])
    return new


# ---------- 时间硬拦截 ----------

_REMINDER_KW = ["提醒我", "叫我", "别忘了", "定时"]
# 只认"钟点"，不认日期：光说"明天"没法定提醒，必须追问几点
_CLOCK_PAT = re.compile(
    r"(\d{1,2}\s*[:：]\s*\d{2}|\d{1,2}\s*[点時时]|[一二三四五六七八九十两]\s*[点時时]|"
    r"半\s*[个小]?时|一刻|整点)")


def needs_time_clarify(text: str) -> bool:
    """
    说了要提醒但没给钟点 -> 只问一句，不调模型
    注意：只给"明天/下周"这类日期也算没给时间，模型很容易自己编个 09:00
    """
    return any(k in text for k in _REMINDER_KW) and not _CLOCK_PAT.search(text)


# ---------- 动作意图与"假报喜"拦截 ----------

# 这些说法说明模型在宣称已经落库，必须真有成功的写工具，否则就是骗用户
_CLAIM_PAT = re.compile(
    r"已(经)?(设置|设好|记下|记好|记住|记录|创建|新建|添加|加入|标记|更新|改成|完成|安排|删除|取消|存|办|弄|搞)"
    r"|(设置|记下|记好|记住|记录|创建|新建|添加|加入|标记|更新|改|完成|安排|删除|取消|存|办|弄|搞定)了")
# 未来式承诺：没命中“已…”也可能是在说“我会每天叫你”，同样要求真落库
_PROMISE_PAT = re.compile(
    r"(会|每天|以后|到时|到时候|明早|明天)[^。！？]{0,12}(叫你|喊你|提醒你|通知你|设好闹钟|定好闹钟)"
    r"|(叫你|喊你)[^。！？]{0,6}(起床|起来)")
_NEGATIVE_PAT = re.compile(r"不会|没法|不能|做不到|没记|没设|失败")
_WRITE_TOOLS = {"task_create", "task_update", "task_breakdown",
                "memory_save", "reminder_create"}


def force_tool_for(text: str):
    """意图很明确时直接锁定工具名，避免模型自作主张换成闲聊"""
    t = text or ""
    if any(k in t for k in _REMINDER_KW) and _CLOCK_PAT.search(t):
        return "reminder_create"
    if any(k in t for k in ("做完了", "完成了", "开始了", "做到一半", "进度", "取消")):
        return "task_update"
    if any(k in t for k in ("怎么样了", "进展", "到哪了", "完成了没", "做完了没")):
        return "task_list"
    # 建任务不做强制：模型自己排 due/domain 更稳，出错由"假报喜"兜底
    if any(k in t for k in ("过敏", "喜欢", "讨厌", "习惯", "偏好", "我叫", "我的名字")):
        return None
    return None


def looks_like_write(text: str) -> bool:
    """用户这句话是不是要求落库（决定首轮是否强制模型调工具）"""
    return force_tool_for(text) is not None


def claims_done(reply: str) -> bool:
    reply = reply or ""
    if reply.strip().endswith(("？", "?")) or _NEGATIVE_PAT.search(reply):
        return False
    return bool(_CLAIM_PAT.search(reply) or _PROMISE_PAT.search(reply))


def _has_write_success(results: list) -> bool:
    return any(r["name"] in _WRITE_TOOLS and r["result"].get("ok") is True
               for r in results)


# ---------- 模型调用 ----------

def _stream_once(messages: list, on_chunk=None, tool_choice="auto"):
    """
    一次流式调用
    返回 (文本, 工具调用列表[{id,name,arguments}])
    tool_choice="required" 用于动作意图明确的场景，避免模型只说不做
    """
    stream = _client().chat.completions.create(
        model=ZHIPU_CHAT_MODEL,
        messages=messages,
        tools=tools.get_tools(),
        tool_choice=tool_choice,
        stream=True,
        temperature=0.5,
    )
    content_parts, calls = [], {}
    for chunk in stream:
        if not chunk.choices:
            continue
        delta = chunk.choices[0].delta
        if delta is None:
            continue
        if getattr(delta, "content", None):
            content_parts.append(delta.content)
            if on_chunk:
                try:
                    on_chunk(delta.content)
                except Exception:
                    pass
        for tc in (getattr(delta, "tool_calls", None) or []):
            entry = calls.setdefault(tc.index, {"id": "", "name": "", "arguments": ""})
            if tc.id:
                entry["id"] = tc.id
            fn = getattr(tc, "function", None)
            if fn is not None:
                if fn.name:
                    entry["name"] = fn.name          # 名称不拼接，避免重复
                if fn.arguments:
                    entry["arguments"] += fn.arguments
    return "".join(content_parts), [calls[i] for i in sorted(calls)]


def _assistant_tool_msg(content: str, calls: list) -> dict:
    return {
        "role": "assistant",
        "content": content or None,
        "tool_calls": [
            {"id": c["id"] or f"call_{i}", "type": "function",
             "function": {"name": c["name"], "arguments": c["arguments"] or "{}"}}
            for i, c in enumerate(calls)
        ],
    }


def _parse_args(raw: str) -> dict:
    if not raw or not raw.strip():
        return {}
    try:
        v = json.loads(raw)
        return v if isinstance(v, dict) else {}
    except json.JSONDecodeError:
        logger.warning("[brain] 工具参数不是合法 JSON: %s", raw[:200])
        return {}


# ---------- 主入口 ----------

def ask_baymax(user_input: str, on_chunk=None, session_id: str = SESSION_ID) -> dict:
    """
    Andy对话主入口
    返回 {"reply", "tools_called", "tool_results", "context_meta", "error"}
    """
    text = (user_input or "").strip()
    if not text:
        return {"reply": "我在，想让我帮你安排点什么？", "tools_called": [], "error": None}

    # 1. 时间硬拦截（省一次模型调用，也避免模型瞎猜时间）
    if needs_time_clarify(text):
        reply = "几点提醒你？给我一个具体时间，比如「明早 8 点」或「11月28日 19:00」。"
        memory.log_event("user", text)
        memory.log_event("assistant", reply)
        return {"reply": reply, "tools_called": [],
                "context_meta": {"intercepted": True}, "error": None}

    # 1.1 基本问答硬拦截：日期/星期/几点直接查本机时钟，模型经常答错
    basic = answer_basic_query(text)
    if basic:
        memory.log_event("user", text)
        memory.log_event("assistant", basic)
        return {"reply": basic, "tools_called": [],
                "context_meta": {"intercepted": "basic_time"}, "error": None}

    # 2. 先取历史（避免把本轮输入重复注入），再落用户事件 + 规则抽事实
    history = memory.recent_events(limit=HISTORY_LIMIT * 2, session_id=session_id)
    ctx = memory.build_context(text)
    uid = memory.log_event("user", text)
    fact_written = False
    try:
        fact_written = bool(memory.extract_facts(text, source_event_id=uid))
    except Exception as e:
        logger.warning("[brain] 事实抽取失败: %s", e)
    # 后台给老事件补向量，不阻塞本轮对话
    memory.kick_backfill()

    # 3. 组装消息：稳定前缀 → 动态上下文 → 最近原文 → 当前输入
    dynamic = "\n\n".join(p for p in [current_time_text(), ctx["text"]] if p)

    from prompts import build_system_prompt
    messages = [
        {"role": "system", "content": build_system_prompt()},
        {"role": "system", "content": dynamic},
    ]
    for ev in history:
        if ev["content"].strip():
            messages.append({"role": ev["role"], "content": ev["content"]})
    messages.append({"role": "user", "content": text})

    # 4. 工具循环
    called, results, reply, err = [], [], "", None
    executed = {}                 # 同一轮里重复调用同工具同参数，直接复用结果
    force_name = force_tool_for(text)
    write_intent = force_name is not None
    # 只有本轮要求写入时，才检查"声称已办好"；查询类回复里出现"已经设置"不算
    expect_write = write_intent or any(k in text for k in ("记住", "记一下", "记下", "帮我记"))
    # 动作意图明确时首轮锁定工具；同时不流式输出，避免"先报喜后纠正"
    stream_cb = None if write_intent else on_chunk

    def first_choice():
        if force_name:
            return {"type": "function", "function": {"name": force_name}}
        return "auto"

    def run_calls(calls):
        """执行一批工具调用，返回 (是否全是重复调用)"""
        dup = 0
        for idx, c in enumerate(calls):
            name = c["name"]
            if not name:
                continue
            args = _parse_args(c["arguments"])
            sig = (name, json.dumps(args, ensure_ascii=False, sort_keys=True))
            if sig in executed:
                # 模型陷入"再调一次"的循环：不重复执行写操作，把上次结果回给它
                dup += 1
                result = dict(executed[sig], note="这步刚才已经执行过，结果同上")
                messages.append({
                    "role": "tool",
                    "tool_call_id": c["id"] or f"call_{idx}",
                    "content": json.dumps(result, ensure_ascii=False),
                })
                logger.info("[tool] %s 重复调用已跳过", name)
                continue
            result = tools.call_tool(name, args)
            if result.get("ok", True) is not False:
                executed[sig] = result      # 只缓存成功结果；失败允许重试
            called.append(name)
            results.append({"name": name, "args": args, "result": result})
            logger.info("[tool] %s %s -> %s", name, args, result.get("ok"))
            messages.append({
                "role": "tool",
                "tool_call_id": c["id"] or f"call_{idx}",
                "content": json.dumps(result, ensure_ascii=False),
            })
        return dup > 0 and dup == len([c for c in calls if c["name"]])

    try:
        for rnd in range(MAX_TOOL_ROUNDS):
            choice = first_choice() if rnd == 0 else "auto"
            content, calls = _stream_once(messages, on_chunk=stream_cb, tool_choice=choice)
            logger.info("[brain] round=%s choice=%s calls=%s content_len=%s",
                        rnd, choice, [c["name"] for c in calls], len(content or ""))
            if not calls:
                reply = content
                break

            messages.append(_assistant_tool_msg(content, calls))
            all_dup = run_calls(calls)
            if all_dup:
                reply = "这步我刚才已经做过了，结果在上面；还有别的要安排的吗？"
                break
        else:
            # 循环用尽仍有工具调用
            reply = "我先按你说的办了几步，剩下的我们下一句继续。"

        # 4.1 假报喜拦截：回复说"已办好"但没有任何成功的写操作 -> 丢历史重试
        # 提醒意图被锁了工具却零调用，即使回复只是"每天8:30叫你"这种承诺，也算没落地
        forced_no_call = (force_name == "reminder_create" and not results
                          and not (reply or "").strip().endswith(("？", "?")))
        if (expect_write and (claims_done(reply) or forced_no_call)
                and not (_has_write_success(results) or fact_written)):
            logger.warning("[brain] 检测到未落库就宣称完成，去历史重试：%s", reply[:80])
            # 多轮历史会把模型带偏；只留稳定前缀 + 当前时间/上下文 + 这一句
            messages = [m for m in messages[:2]] + [{
                "role": "system",
                "content": ("这一轮你还没有调用任何工具。必须真实调用合适的工具把事办成；"
                            "缺少信息就说明缺什么并提问，不准再说“已设置/已记下/已完成”。"),
            }, {"role": "user", "content": text}]
            for i in range(2):
                content, calls = _stream_once(
                    messages, on_chunk=None,
                    tool_choice=(first_choice() if i == 0 else "auto")
                    if force_name else "required")
                if not calls:
                    reply = content
                    break
                messages.append(_assistant_tool_msg(content, calls))
                if run_calls(calls):
                    reply = "这一步刚才已经办过了，结果在上一条里。"
                    break
            if expect_write and claims_done(reply) and not (_has_write_success(results) or fact_written):
                reply = ("抱歉，刚才那条我只是嘴上说办好了，实际没写进系统。"
                         "你再说一次，我马上用工具真正办。")

    except Exception as e:
        logger.error("[brain] 模型调用失败: %s", e)
        err = str(e)
        reply = f"我这边连模型出了点问题（{type(e).__name__}）。本地任务和提醒功能还能用，稍后再试一次对话。"

    reply = (reply or "").strip() or "嗯，我在。"
    reply = calibrate_reply_time(reply)
    memory.log_event("assistant", reply)
    return {
        "reply": reply,
        "tools_called": called,
        "tool_results": results,
        "context_meta": ctx["stats"],
        "error": err,
    }


# 兼容旧名字
ask = ask_baymax


def health_check() -> bool:
    return bool(ZHIPU_API_KEY) and bool(tools.TOOLS)
