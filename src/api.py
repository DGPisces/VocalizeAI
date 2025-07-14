"""
API配置文件（已废弃）
请使用 src.config 模块替代此文件
"""
import warnings

warnings.warn(
    "src.api 模块已废弃，请使用 src.config 模块",
    DeprecationWarning,
    stacklevel=2
)

# 为了向后兼容，保留原有的变量定义
import os

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

# 新增的模型和URL配置
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.sensenova.cn/compatible-mode/v1/")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "DeepSeek-V3")

GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
GOOGLE_MODEL_ID = os.getenv("GOOGLE_MODEL_ID", "gemini-2.5-flash-preview-tts")

