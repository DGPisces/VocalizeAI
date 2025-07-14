"""
配置管理模块
统一管理所有API密钥和配置项
"""
import os
import logging
from typing import Optional, Dict, Any
from dataclasses import dataclass


@dataclass
class Config:
    """应用配置类"""
    # OpenAI配置
    openai_api_key: Optional[str] = None
    openai_base_url: str = "https://api.sensenova.cn/compatible-mode/v1/"
    openai_model: str = "DeepSeek-V3"
    
    # SenseNova配置
    sensenova_access_key_id: Optional[str] = None
    sensenova_secret_access_key: Optional[str] = None
    
    # Google配置
    google_api_key: Optional[str] = None
    google_model_id: str = "gemini-2.5-flash-preview-tts"
    
    # 日志配置
    log_dir: str = "logs"
    ai_generated_log: str = "logs/ai_generated_log.txt"
    reflection_log: str = "logs/chatbot_reflection_log.txt"
    max_reflection_entries: int = 5
    
    # 音频配置
    audio_channels: int = 1
    audio_rate: int = 24000
    audio_sample_width: int = 2
    
    @classmethod
    def from_env(cls) -> 'Config':
        """从环境变量加载配置"""
        return cls(
            openai_api_key=os.getenv("OPENAI_API_KEY"),
            openai_base_url=os.getenv("OPENAI_BASE_URL", cls.openai_base_url),
            openai_model=os.getenv("OPENAI_MODEL", cls.openai_model),
            sensenova_access_key_id=os.getenv("SENSENOVA_ACCESS_KEY_ID"),
            sensenova_secret_access_key=os.getenv("SENSENOVA_SECRET_ACCESS_KEY"),
            google_api_key=os.getenv("GOOGLE_API_KEY"),
            google_model_id=os.getenv("GOOGLE_MODEL_ID", cls.google_model_id)
        )
    
    def validate(self) -> Dict[str, bool]:
        """验证配置的有效性"""
        validation_results = {}
        
        # 检查必需的API密钥
        validation_results['openai_api_key'] = bool(self.openai_api_key)
        validation_results['google_api_key'] = bool(self.google_api_key)
        validation_results['sensenova_keys'] = bool(
            self.sensenova_access_key_id and self.sensenova_secret_access_key
        )
        
        return validation_results
    
    def get_missing_configs(self) -> list[str]:
        """获取缺失的配置项"""
        validation_results = self.validate()
        missing = []
        
        if not validation_results['openai_api_key']:
            missing.append('OPENAI_API_KEY')
        if not validation_results['google_api_key']:
            missing.append('GOOGLE_API_KEY')
        if not validation_results['sensenova_keys']:
            missing.extend(['SENSENOVA_ACCESS_KEY_ID', 'SENSENOVA_SECRET_ACCESS_KEY'])
            
        return missing
    
    def ensure_log_dir(self) -> None:
        """确保日志目录存在"""
        if not os.path.exists(self.log_dir):
            os.makedirs(self.log_dir, exist_ok=True)


# 全局配置实例
config = Config.from_env()


def get_config() -> Config:
    """获取全局配置实例"""
    return config


def validate_config() -> bool:
    """验证配置并输出警告信息"""
    missing = config.get_missing_configs()
    if missing:
        logging.warning(f"缺少以下环境变量: {', '.join(missing)}")
        return False
    return True 