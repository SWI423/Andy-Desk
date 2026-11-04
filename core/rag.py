"""
知识库 RAG 模块 rag.py
负责：医疗知识库向量检索

后续任务 M6 实现具体逻辑
"""


def search_knowledge(query: str, top_k: int = 3) -> list:
    """
    检索知识库

    参数:
        query: 用户问题
        top_k: 返回几条结果

    返回:
        list: 相关知识条目列表
    """
    # TODO: M6 实现
    raise NotImplementedError("M6 任务：知识库 RAG")


def health_check() -> bool:
    """健康检查"""
    return True
