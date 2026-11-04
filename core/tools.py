"""
工具注册模块 tools.py
负责：function calling 工具定义和执行

后续任务 M7 实现具体逻辑
"""


# 工具 JSON Schema 列表（给模型看的说明书）
TOOLS = [
    # TODO: M7 定义 5 个工具
    # - save_memory
    # - set_reminder
    # - search_knowledge
    # - log_health_event
    # - get_health_trend
]


# 工具注册表（函数名 → 实际函数）
TOOL_REGISTRY = {}


def call_tool(tool_name: str, args: dict) -> str:
    """执行一个工具调用"""
    # TODO: M7 实现
    raise NotImplementedError("M7 任务：工具调用循环")


def health_check() -> bool:
    """健康检查"""
    return True
