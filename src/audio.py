"""
音频处理模块
处理音频播放和语音生成相关功能
"""
import os
import logging
import tempfile
import contextlib
import wave
from typing import Any

import pygame

from .config import get_config
from .ai_clients import generate_voice_google


@contextlib.contextmanager
def wave_file(filename: str, channels: int = 1, rate: int = 24000, sample_width: int = 2):
    """创建WAV文件的上下文管理器"""
    with wave.open(filename, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(sample_width)
        wf.setframerate(rate)
        yield wf


class AudioPlayer:
    """音频播放器"""
    
    def __init__(self):
        self.config = get_config()
        self._initialized = False
    
    def _ensure_initialized(self) -> None:
        """确保pygame mixer已初始化"""
        if not self._initialized:
            try:
                pygame.mixer.init()
                self._initialized = True
                logging.info("音频播放器初始化成功")
            except Exception as e:
                logging.error(f"音频播放器初始化失败: {e}")
                raise
    
    def play_audio_blob(self, blob: Any) -> None:
        """播放音频数据块"""
        self._ensure_initialized()
        
        # 创建临时文件路径
        with tempfile.NamedTemporaryFile(suffix='.wav', delete=False) as temp_file:
            temp_file_path = temp_file.name
        
        try:
            # 使用wave模块创建标准的WAV文件
            with wave_file(
                temp_file_path, 
                channels=self.config.audio_channels,
                rate=self.config.audio_rate,
                sample_width=self.config.audio_sample_width
            ) as wav:
                wav.writeframes(blob.data)
            
            # 播放音频
            pygame.mixer.music.load(temp_file_path)
            pygame.mixer.music.play()
            
            # 等待播放完成
            while pygame.mixer.music.get_busy():
                pygame.time.wait(100)
                
        except Exception as e:
            logging.error(f"音频播放失败: {e}")
        finally:
            # 清理临时文件
            try:
                os.unlink(temp_file_path)
            except Exception as e:
                logging.warning(f"清理临时音频文件失败: {e}")
    
    def play_audio_response(self, response: Any) -> None:
        """播放AI语音响应"""
        try:
            if hasattr(response, 'candidates') and response.candidates:
                candidate = response.candidates[0]
                if hasattr(candidate, 'content') and hasattr(candidate.content, 'parts'):
                    parts = candidate.content.parts
                    if parts and hasattr(parts[0], 'inline_data'):
                        self.play_audio_blob(parts[0].inline_data)
                    else:
                        logging.warning("响应中没有找到音频数据")
                else:
                    logging.warning("响应格式不正确")
            else:
                logging.warning("响应中没有candidates")
        except Exception as e:
            logging.error(f"播放AI响应失败: {e}")


class VoiceGenerator:
    """语音生成器"""
    
    def __init__(self):
        self.audio_player = AudioPlayer()
    
    def generate_and_play(self, text: str, voice_name: str = "Kore") -> None:
        """生成语音并播放"""
        try:
            logging.info(f"生成语音: {text[:50]}...")
            response = generate_voice_google(text, voice_name)
            self.audio_player.play_audio_response(response)
        except Exception as e:
            logging.error(f"语音生成和播放失败: {e}")


# 全局实例
_audio_player: AudioPlayer = None
_voice_generator: VoiceGenerator = None


def get_audio_player() -> AudioPlayer:
    """获取音频播放器实例"""
    global _audio_player
    if _audio_player is None:
        _audio_player = AudioPlayer()
    return _audio_player


def get_voice_generator() -> VoiceGenerator:
    """获取语音生成器实例"""
    global _voice_generator
    if _voice_generator is None:
        _voice_generator = VoiceGenerator()
    return _voice_generator


def generate_and_play_voice(text: str, voice_name: str = "Kore") -> None:
    """便捷函数：生成并播放语音"""
    get_voice_generator().generate_and_play(text, voice_name) 