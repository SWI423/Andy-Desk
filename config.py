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
EMBED_MIN_CORPUS = int(os.getenv("EMBED_MIN_CORPUS", "8"))      # 少于这么多条时不做去均值
EMBED_SMALL_SCORE = float(os.getenv("EMBED_SMALL_SCORE", "0.45"))  # 小语料下未中心化的余弦下限
RECENCY_HALF_LIFE_DAYS = float(os.getenv("RECENCY_HALF_LIFE_DAYS", "120"))  # 记忆衰减半衰期
RECENCY_MIN_WEIGHT = float(os.getenv("RECENCY_MIN_WEIGHT", "0.35"))         # 衰减下限，避免老事彻底消失
RECALL_SIM_GATE = float(os.getenv("RECALL_SIM_GATE", "0.80"))               # 只保留接近最高语义分的候选
RECALL_INTENT_BOOST = float(os.getenv("RECALL_INTENT_BOOST", "0.08"))       # 问句与内容同主题时的小幅加权

# === MCP 扩展（外部工具）===
MCP_ENABLED = os.getenv("MCP_ENABLED", "1").lower() not in ("0", "false", "off")
MCP_CONFIG_PATH = os.path.join(BASE_DIR, os.getenv("MCP_CONFIG_PATH", "mcp_servers.json"))
MCP_TIMEOUT = float(os.getenv("MCP_TIMEOUT", "20"))        # 单次请求超时（秒）
MCP_MAX_TOOLS = int(os.getenv("MCP_MAX_TOOLS", "24"))     # 最多暴露给模型的 MCP 工具数
MCP_ALLOW_WRITE = os.getenv("MCP_ALLOW_WRITE", "0").lower() in ("1", "true", "on")

# === 语音输入（ASR，离线）===
# Vosk 中文小模型：models/vosk-model-small-cn-0.22（约 42MB，需单独下载）
ASR_ENABLED = os.getenv("ASR_ENABLED", "1").lower() not in ("0", "false", "off")
ASR_MODEL_DIR = os.path.join(BASE_DIR, os.getenv(
    "ASR_MODEL_DIR", os.path.join("models", "vosk-model-small-cn-0.22")))
ASR_SAMPLE_RATE = int(os.getenv("ASR_SAMPLE_RATE", "16000"))
ASR_BLOCK_SIZE = int(os.getenv("ASR_BLOCK_SIZE", "4000"))
ASR_MAX_SECONDS = int(os.getenv("ASR_MAX_SECONDS", "30"))

# === 语音播报（TTS）===
TTS_ENABLED = os.getenv("TTS_ENABLED", "0").lower() in ("1", "true", "on")
# TTS_VOICE 留空 = 按文本语言自动选（中文 ZH / 英文 EN）；填了则强制使用该音色
TTS_VOICE = os.getenv("TTS_VOICE", "").strip()
TTS_VOICE_ZH = os.getenv("TTS_VOICE_ZH", "zh-CN-YunxiNeural")   # 中文男声
TTS_VOICE_ZH_TW = os.getenv("TTS_VOICE_ZH_TW", "zh-TW-HsiaoChenNeural")
TTS_VOICE_EN = os.getenv("TTS_VOICE_EN", "en-US-BrianMultilingualNeural")  # 自动模式英文音色
TTS_VOICE_EN_GB = os.getenv("TTS_VOICE_EN_GB", "en-GB-RyanNeural")
TTS_VOICE_EN_CA = os.getenv("TTS_VOICE_EN_CA", "en-CA-LiamNeural")
TTS_VOICE_EN_HK = os.getenv("TTS_VOICE_EN_HK", "en-HK-SamNeural")
TTS_VOICE_EN_US = os.getenv("TTS_VOICE_EN_US", "en-US-BrianMultilingualNeural")
TTS_RATE = os.getenv("TTS_RATE", "+0%")                         # 强制音色时的语速
TTS_RATE_ZH = os.getenv("TTS_RATE_ZH", "+0%")
TTS_RATE_EN = os.getenv("TTS_RATE_EN", "+0%")
TTS_PITCH = os.getenv("TTS_PITCH", "+0Hz")                      # 强制音色时的音调
TTS_PITCH_ZH = os.getenv("TTS_PITCH_ZH", "+0Hz")
TTS_PITCH_EN = os.getenv("TTS_PITCH_EN", "+0Hz")
TTS_VOLUME = os.getenv("TTS_VOLUME", "+0%")
TTS_MAX_CHARS = int(os.getenv("TTS_MAX_CHARS", "240"))          # 单次播报上限，避免长回答念太久
TTS_CHUNK_CHARS = int(os.getenv("TTS_CHUNK_CHARS", "80"))       # 兼容旧配置；当前整段合成不再分句
TTS_SAPI_RATE = int(os.getenv("TTS_SAPI_RATE", "175"))          # pyttsx3 兜底语速
TTS_SAPI_VOLUME = float(os.getenv("TTS_SAPI_VOLUME", "0.9"))

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
