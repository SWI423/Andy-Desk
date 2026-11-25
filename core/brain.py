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


# ---------- 时间硬拦截 ----------

_REMINDER_KW = ["提醒我", "叫我", "别忘了", "定时"]
_TIME_PAT = re.compile(
    r"(\d{1,2}[:：]\d{2}|\d{1,2}\s*[点時时]|今天|明天|后天|大后天|每天|每周|每星期|"
    r"周[一二三四五六日天]|下周|下个月|\d{4}-\d{1,2}-\d{1,2}|\d{1,2}月\d{1,2}[日号])"
)


def needs_time_clarify(text: str) -> bool:
    """说了要提醒但没给任何时间 -> 只问一句，不调模型"""
    return any(k in text for k in _REMINDER_KW) and not _TIME_PAT.search(text)


# ---------- 模型调用 ----------

def _stream_once(messages: list, on_chunk=None):
    """
    一次流式调用
    返回 (文本, 工具调用列表[{id,name,arguments}])
    """
    stream = _client().chat.completions.create(
        model=ZHIPU_CHAT_MODEL,
        messages=messages,
        tools=tools.TOOLS,
        tool_choice="auto",
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

    # 2. 先取历史（避免把本轮输入重复注入），再落用户事件 + 规则抽事实
    history = memory.recent_events(limit=HISTORY_LIMIT * 2, session_id=session_id)
    ctx = memory.build_context(text)
    uid = memory.log_event("user", text)
    try:
        memory.extract_facts(text, source_event_id=uid)
    except Exception as e:
        logger.warning("[brain] 事实抽取失败: %s", e)

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
    try:
        for rnd in range(MAX_TOOL_ROUNDS):
            content, calls = _stream_once(messages, on_chunk=on_chunk)
            if not calls:
                reply = content
                break

            messages.append(_assistant_tool_msg(content, calls))
            for c in calls:
                name = c["name"]
                if not name:
                    continue
                args = _parse_args(c["arguments"])
                result = tools.call_tool(name, args)
                called.append(name)
                results.append({"name": name, "args": args, "result": result})
                logger.info("[tool] %s %s -> %s", name, args, result.get("ok"))
                messages.append({
                    "role": "tool",
                    "tool_call_id": c["id"] or f"call_{len(called)}",
                    "content": json.dumps(result, ensure_ascii=False),
                })
        else:
            # 循环用尽仍有工具调用
            reply = "我先按你说的办了几步，剩下的我们下一句继续。"

    except Exception as e:
        logger.error("[brain] 模型调用失败: %s", e)
        err = str(e)
        reply = f"我这边连模型出了点问题（{type(e).__name__}）。本地任务和提醒功能还能用，稍后再试一次对话。"

    reply = (reply or "").strip() or "嗯，我在。"
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
