"""
骨架健康测试：验证所有模块能正常导入
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_config_import():
    """配置模块能导入"""
    import config
    assert config.ZHIPU_CHAT_MODEL is not None


def test_console_import():
    """日志模块能导入"""
    import console
    assert console.logger is not None


def test_brain_import():
    """对话核心能导入"""
    from core import brain
    assert callable(brain.ask_baymax)


def test_memory_import():
    """记忆模块能导入"""
    from core import memory
    assert callable(memory.save_health_event)


def test_rag_import():
    """知识库模块能导入"""
    from core import rag
    assert callable(rag.search_knowledge)


def test_tools_import():
    """工具模块能导入"""
    from core import tools
    assert isinstance(tools.TOOLS, list)


def test_prompts_import():
    """提示词能导入"""
    import prompts
    assert prompts.SYSTEM_PROMPT is not None
