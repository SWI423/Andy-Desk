"""
对话核心 brain.py
负责：接收用户输入 → 拼装 prompt → 调模型 → 工具循环 → 返回回复

后续任务 M4 实现具体逻辑
"""


def ask_baymax(user_input: str, on_chunk=None) -> dict:
    """
    大白对话主入口

    参数:
        user_input: 用户输入的文字
        on_chunk: 流式回调函数，每收到一段就调一次

    返回:
        dict: {
            "reply": "完整回复文本",
            "tools_called": ["调用过的工具名列表"],
        }
    """
    # TODO: M4 实现
    raise NotImplementedError("M4 任务：对话核心")


def health_check() -> bool:
    """健康检查：模块是否能正常加载"""
    return True
