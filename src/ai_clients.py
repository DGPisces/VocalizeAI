"""
AI客户端管理模块
统一管理OpenAI和Google AI客户端，提供单例模式避免重复创建连接
"""
import logging
from typing import Optional, List, Dict, Any
from openai import OpenAI
from google import genai
from google.genai import types
import sensenova

from .config import get_config, validate_config


class AIClientManager:
    """AI客户端管理器"""
    
    def __init__(self):
        self.config = get_config()
        self._openai_client: Optional[OpenAI] = None
        self._google_client: Optional[genai.Client] = None
        self._initialized = False
    
    def initialize(self) -> bool:
        """初始化所有AI客户端"""
        if self._initialized:
            return True
        
        try:
            # 验证配置
            if not validate_config():
                return False
            
            # 初始化SenseNova
            if self.config.sensenova_access_key_id:
                sensenova.access_key_id = self.config.sensenova_access_key_id
            
            # 初始化OpenAI客户端
            if self.config.openai_api_key:
                self._openai_client = OpenAI(
                    api_key=self.config.openai_api_key,
                    base_url=self.config.openai_base_url
                )
                logging.info("OpenAI客户端初始化成功")
            
            # 初始化Google客户端
            if self.config.google_api_key:
                self._google_client = genai.Client(api_key=self.config.google_api_key)
                logging.info("Google AI客户端初始化成功")
            
            self._initialized = True
            return True
            
        except Exception as e:
            logging.error(f"AI客户端初始化失败: {e}")
            return False
    
    @property
    def openai_client(self) -> Optional[OpenAI]:
        """获取OpenAI客户端"""
        if not self._initialized:
            self.initialize()
        return self._openai_client
    
    @property
    def google_client(self) -> Optional[genai.Client]:
        """获取Google AI客户端"""
        if not self._initialized:
            self.initialize()
        return self._google_client
    
    def ask_gpt(self, messages: List[Dict[str, str]]) -> str:
        """调用GPT模型进行对话"""
        if not self.openai_client:
            raise RuntimeError("OpenAI客户端未初始化")
        
        try:
            response = self.openai_client.chat.completions.create(
                model=self.config.openai_model,
                messages=messages
            )
            return response.choices[0].message.content
        except Exception as e:
            logging.error(f"GPT调用失败: {e}")
            raise
    
    def generate_voice_google(self, text: str, voice_name: str = "Kore") -> Any:
        """使用Google AI生成语音"""
        if not self.google_client:
            raise RuntimeError("Google AI客户端未初始化")
        
        try:
            response = self.google_client.models.generate_content(
                model=self.config.google_model_id,
                contents=[text],
                config=types.GenerateContentConfig(
                    response_modalities=["AUDIO"],
                    speech_config=types.SpeechConfig(
                        voice_config=types.VoiceConfig(
                            prebuilt_voice_config=types.PrebuiltVoiceConfig(
                                voice_name=voice_name
                            )
                        )
                    )
                )
            )
            return response
        except Exception as e:
            logging.error(f"Google语音生成失败: {e}")
            raise


# 全局AI客户端管理器实例
_ai_manager: Optional[AIClientManager] = None


def get_ai_manager() -> AIClientManager:
    """获取AI客户端管理器单例"""
    global _ai_manager
    if _ai_manager is None:
        _ai_manager = AIClientManager()
        _ai_manager.initialize()
    return _ai_manager


def ask_gpt(messages: List[Dict[str, str]]) -> str:
    """便捷函数：调用GPT模型"""
    return get_ai_manager().ask_gpt(messages)


def generate_voice_google(text: str, voice_name: str = "Kore") -> Any:
    """便捷函数：生成Google语音"""
    return get_ai_manager().generate_voice_google(text, voice_name) 