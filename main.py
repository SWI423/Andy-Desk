"""
大白 Baymax_pro 主入口
用法: python main.py
"""

import sys

from config import check_config
from console import logger


def main():
    """主函数"""
    logger.info("=" * 40)
    logger.info("大白 Baymax_pro 启动中...")
    logger.info("=" * 40)

    # 配置检查
    if not check_config():
        logger.warning("配置未完整，部分功能可能不可用")

    # 模块健康检查
    logger.info("--- 模块健康检查 ---")
    try:
        from core.brain import health_check as brain_ok
        logger.info(f"对话核心: {'✓' if brain_ok() else '✗'}")
    except Exception as e:
        logger.info(f"对话核心: ✗ ({e})")

    try:
        from core.memory import health_check as mem_ok
        logger.info(f"记忆模块: {'✓' if mem_ok() else '✗'}")
    except Exception as e:
        logger.info(f"记忆模块: ✗ ({e})")

    try:
        from core.rag import health_check as rag_ok
        logger.info(f"知识库: {'✓' if rag_ok() else '✗'}")
    except Exception as e:
        logger.info(f"知识库: ✗ ({e})")

    try:
        from core.tools import health_check as tools_ok
        logger.info(f"工具系统: {'✓' if tools_ok() else '✗'}")
    except Exception as e:
        logger.info(f"工具系统: ✗ ({e})")

    logger.info("--- 骨架就绪，等待功能开发 ---")

    # TODO: M9 启动桌宠窗口
    # from PyQt6.QtWidgets import QApplication
    # from desktop.main_window import BaymaxWindow
    # app = QApplication(sys.argv)
    # window = BaymaxWindow()
    # window.show()
    # sys.exit(app.exec())


if __name__ == "__main__":
    main()
