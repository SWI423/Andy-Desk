"""
记忆模块 memory.py
负责：用户画像、健康记录、提醒事项的数据库操作

后续任务 M3/M5 实现具体逻辑
"""


def init_db():
    """初始化数据库表结构"""
    # TODO: M3 实现建表
    raise NotImplementedError("M3 任务：数据库 Schema")


def save_health_event(event_type: str, detail: str):
    """记录一条健康事件"""
    # TODO: M5 实现
    raise NotImplementedError("M5 任务：记忆模块")


def get_user_profile() -> dict:
    """获取用户画像"""
    # TODO: M5 实现
    raise NotImplementedError("M5 任务：记忆模块")


def add_reminder(content: str, remind_time: str):
    """添加定时提醒"""
    # TODO: M5 实现
    raise NotImplementedError("M5 任务：记忆模块")


def health_check() -> bool:
    """健康检查"""
    return True
