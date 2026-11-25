"""
用户档案 core/profile.py
- 单用户模式下的"第一位用户"建档
- 记录：名字、启用日期、助手名、备注；同时写入语义事实，供对话层注入
"""

from datetime import date

from config import APP_NAME
from core import memory

DEFAULT_USER_NAME = "Wen"


def ensure_first_user(name: str = DEFAULT_USER_NAME) -> dict:
    """
    幂等建档：已有 user_name 就只补齐助手名，不覆盖用户自己改过的资料
    返回 {"created": bool, "name": str}
    """
    name = (name or "").strip() or DEFAULT_USER_NAME
    existing = memory.get_profile("user_name")

    if existing:
        memory.set_profile("assistant_name", APP_NAME)
        return {"created": False, "name": existing}

    memory.set_profile("user_name", name)
    memory.set_profile("user_since", date.today().isoformat())
    memory.set_profile("assistant_name", APP_NAME)
    memory.set_profile("user_note", "首位用户")
    memory.upsert_fact("name", name, domain="general", confidence=0.95)
    memory.log_event("system", f"[档案] 首位用户 {name} 已建立，助手 {APP_NAME}", importance=0.8)
    return {"created": True, "name": name}


def get_user_name() -> str:
    return memory.get_profile("user_name") or ""


def get_profile_summary() -> dict:
    return {
        "user_name": get_user_name(),
        "user_since": memory.get_profile("user_since") or "",
        "assistant_name": memory.get_profile("assistant_name") or APP_NAME,
        "user_note": memory.get_profile("user_note") or "",
    }


def health_check() -> bool:
    return True
