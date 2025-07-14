"""
Vocalize AI 餐厅预订聊天机器人包
提供语音交互、AI对话和自我反思功能的模块化聊天机器人系统
"""

__version__ = "1.0.0"
__author__ = "DGPisces"

from .config import get_config, validate_config
from .logger import setup_logging
from .chatbot import main

__all__ = [
    "get_config",
    "validate_config", 
    "setup_logging",
    "main"
] 