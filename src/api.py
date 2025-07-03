import os

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
SENSENOVA_ACCESS_KEY_ID = os.getenv("SENSENOVA_ACCESS_KEY_ID")
SENSENOVA_SECRET_ACCESS_KEY = os.getenv("SENSENOVA_SECRET_ACCESS_KEY")

# 新增的模型和URL配置
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.sensenova.cn/compatible-mode/v1/")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "DeepSeek-V3")