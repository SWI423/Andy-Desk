"""
Andy（Baymax_pro） 配置中心
- 所有路径基于项目根目录计算，项目移动后无需改代码
- 密钥走 .env（不入库）
"""

import os
from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))

# === 智谱 AI ===
ZHIPU_API_KEY = os.getenv("ZHIPU_API_KEY", "")
ZHIPU_BASE_URL = os.getenv("ZHIPU_BASE_URL", "https://open.bigmodel.cn/api/paas/v4")
ZHIPU_CHAT_MODEL = os.getenv("ZHIPU_CHAT_MODEL", "glm-4-flash")
ZHIPU_EMBED_MODEL = os.getenv("ZHIPU_EMBED_MODEL", "embedding-3")

# === 路径（全部绝对化，基于 BASE_DIR）===
DATA_DIR = os.path.join(BASE_DIR, os.getenv("DATA_DIR", "data"))
LOG_DIR = os.path.join(BASE_DIR, os.getenv("LOG_DIR", "logs"))
DB_PATH = os.path.join(DATA_DIR, "baymax.db")

# === 日志 ===
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

# === 应用参数 ===
APP_NAME = "Andy"
SESSION_ID = os.getenv("SESSION_ID", "default")   # 单用户，先用固定会话
MAX_TOOL_ROUNDS = 4                               # 工具循环上限，防死循环
HISTORY_LIMIT = 6                                 # L1 工作记忆保留轮数
CONTEXT_TOKEN_BUDGET = 2500                       # 记忆注入部分 token 上限（估算）

# === 记忆检索（embedding + 关键词混合）===
EMBED_ENABLED = os.getenv("EMBED_ENABLED", "1").lower() not in ("0", "false", "off")
EMBED_MODEL = ZHIPU_EMBED_MODEL                   # embedding-3，实测输出 2048 维
EMBED_BATCH = int(os.getenv("EMBED_BATCH", "16"))         # 批量向量化条数
EMBED_TOPK = int(os.getenv("EMBED_TOPK", "10"))           # 向量侧候选条数
EMBED_MIN_SCORE = float(os.getenv("EMBED_MIN_SCORE", "0.10"))   # 中心化余弦下限（本地语料标定）
EMBED_MARGIN = float(os.getenv("EMBED_MARGIN", "0.06"))   # 只保留靠近最高分的候选
EMBED_BACKFILL_MAX = int(os.getenv("EMBED_BACKFILL_MAX", "400"))  # 单次后台补向量上限
RRF_K = int(os.getenv("RRF_K", "60"))                     # RRF 平滑常数
KEYWORD_TOPK = int(os.getenv("KEYWORD_TOPK", "10"))       # 关键词侧候选条数

# === 领域定义（三线）===
DOMAINS = {
    "study": "学习",
    "work": "工作",
    "life": "生活",
}
DOMAIN_KEYS = list(DOMAINS.keys())


def ensure_dirs():
    """确保运行目录存在"""
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(LOG_DIR, exist_ok=True)


def check_config() -> bool:
    """关键配置是否就绪"""
    return bool(ZHIPU_API_KEY)


if __name__ == "__main__":
    ensure_dirs()
    print("=== Andy配置检查 ===")
    print(f"项目根目录: {BASE_DIR}")
    print(f"智谱 API Key: {'已配置' if ZHIPU_API_KEY else '未配置'}")
    print(f"对话模型: {ZHIPU_CHAT_MODEL}")
    print(f"数据库: {DB_PATH}")
    print(f"日志目录: {LOG_DIR}")
    print(f"领域: {DOMAINS}")
