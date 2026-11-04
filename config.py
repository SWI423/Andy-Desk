"""
大白 Baymax_pro 配置模块
集中管理所有配置项，从环境变量和 .env 文件加载
"""

import os
from dotenv import load_dotenv

# 加载 .env 文件
load_dotenv()


# === 智谱 AI 配置 ===
ZHIPU_API_KEY = os.getenv("ZHIPU_API_KEY", "")
ZHIPU_BASE_URL = os.getenv("ZHIPU_BASE_URL", "https://open.bigmodel.cn/api/paas/v4")
ZHIPU_CHAT_MODEL = os.getenv("ZHIPU_CHAT_MODEL", "glm-4-flash")
ZHIPU_EMBED_MODEL = os.getenv("ZHIPU_EMBED_MODEL", "embedding-3")

# === 数据库 ===
DB_PATH = os.getenv("DB_PATH", "data/baymax.db")

# === 日志 ===
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
LOG_DIR = os.getenv("LOG_DIR", "logs")


def check_config() -> bool:
    """检查关键配置是否就绪"""
    if not ZHIPU_API_KEY:
        print("[警告] 未配置 ZHIPU_API_KEY，请复制 .env.example 为 .env 并填入")
        return False
    return True


if __name__ == "__main__":
    # 健康检查：打印配置状态
    print("=== 大白配置检查 ===")
    print(f"智谱 API Key: {'已配置' if ZHIPU_API_KEY else '未配置'}")
    print(f"对话模型: {ZHIPU_CHAT_MODEL}")
    print(f"向量模型: {ZHIPU_EMBED_MODEL}")
    print(f"数据库路径: {DB_PATH}")
    print(f"日志级别: {LOG_LEVEL}")
    print(f"配置检查: {'通过' if check_config() else '未通过'}")
