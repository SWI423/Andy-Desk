"""
控制台日志模块 console.py
双写日志：终端 + 文件，按天轮转
"""

import logging
import os
from logging.handlers import TimedRotatingFileHandler

from config import LOG_DIR, LOG_LEVEL


def setup_logger(name: str = "baymax") -> logging.Logger:
    """
    初始化日志系统

    返回:
        Logger: 配置好的 logger
    """
    logger = logging.getLogger(name)
    logger.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))

    # 避免重复添加 handler
    if logger.handlers:
        return logger

    # 确保日志目录存在
    os.makedirs(LOG_DIR, exist_ok=True)

    # 终端输出
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S"
    ))

    # 文件输出（按天轮转）
    file_handler = TimedRotatingFileHandler(
        os.path.join(LOG_DIR, "baymax.log"),
        when="midnight",
        backupCount=7,
        encoding="utf-8"
    )
    file_handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    ))

    logger.addHandler(console_handler)
    logger.addHandler(file_handler)

    return logger


# 全局 logger
logger = setup_logger()


def health_check() -> bool:
    """健康检查"""
    return True
