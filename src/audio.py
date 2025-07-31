"""
音频处理模块
处理音频播放、录制和语音生成相关功能
"""
import os
import logging
import tempfile
import contextlib
import wave
import threading
import time
from typing import Any, Optional
import numpy as np
import sounddevice as sd
import whisper

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


class AudioRecorder:
    """音频录制器，支持静音检测"""
    
    def __init__(self, silence_threshold: float = 0.01, silence_duration: float = 2.0):
        """
        初始化录音器
        Args:
            silence_threshold: 静音阈值，低于此值认为是静音
            silence_duration: 静音持续时间（秒），超过此时间停止录音
        """
        self.config = get_config()
        self.silence_threshold = silence_threshold
        self.silence_duration = silence_duration
        self.recording = False
        self.audio_data = []
        self.last_sound_time = time.time()
        self._lock = threading.Lock()
        
    def _audio_callback(self, indata, frames, time_info, status):
        """音频回调函数，处理录音数据和静音检测"""
        if status:
            logging.warning(f"录音状态警告: {status}")
        
        # 计算音量（RMS）
        volume_norm = np.sqrt(np.mean(indata**2))
        
        with self._lock:
            if self.recording:
                # 添加音频数据
                self.audio_data.append(indata.copy())
                
                # 检查是否有声音
                if volume_norm > self.silence_threshold:
                    self.last_sound_time = time.time()
                else:
                    # 检查静音时间是否超过阈值
                    if time.time() - self.last_sound_time > self.silence_duration:
                        logging.info("检测到静音，停止录音")
                        self.recording = False
    
    def start_recording(self):
        """开始录音"""
        with self._lock:
            self.recording = True
            self.audio_data = []
            self.last_sound_time = time.time()
        
        logging.info("开始录音...")
        
        # 开始录音流
        self.stream = sd.InputStream(
            callback=self._audio_callback,
            channels=self.config.audio_channels,
            samplerate=self.config.audio_rate,
            dtype=np.float32
        )
        self.stream.start()
    
    def stop_recording(self) -> Optional[np.ndarray]:
        """停止录音并返回音频数据"""
        with self._lock:
            self.recording = False
        
        if hasattr(self, 'stream'):
            self.stream.stop()
            self.stream.close()
        
        if self.audio_data:
            # 合并所有音频数据
            audio_array = np.concatenate(self.audio_data, axis=0)
            logging.info(f"录音完成，时长: {len(audio_array) / self.config.audio_rate:.2f}秒")
            return audio_array.flatten()
        
        return None
    
    def record_until_silence(self) -> Optional[np.ndarray]:
        """录音直到检测到静音"""
        self.start_recording()
        
        try:
            # 等待录音完成（通过静音检测自动停止）
            while True:
                with self._lock:
                    if not self.recording:
                        break
                time.sleep(0.1)
        except KeyboardInterrupt:
            logging.info("录音被用户中断")
        
        return self.stop_recording()


class SpeechToText:
    """语音转文字处理器"""
    
    def __init__(self, model_name: str = "turbo"):
        """
        初始化语音转文字处理器
        Args:
            model_name: whisper模型名称，默认为turbo
        """
        self.model_name = model_name
        self.model = None
        self.config = get_config()
        
    def _ensure_model_loaded(self):
        """确保whisper模型已加载"""
        if self.model is None:
            logging.info(f"加载Whisper模型: {self.model_name}")
            try:
                self.model = whisper.load_model(self.model_name)
                logging.info("Whisper模型加载成功")
            except Exception as e:
                logging.error(f"Whisper模型加载失败: {e}")
                raise
    
    def transcribe_audio_data(self, audio_data: np.ndarray) -> str:
        """
        将音频数据转换为文字
        Args:
            audio_data: 音频数据数组
        Returns:
            转换后的文字
        """
        self._ensure_model_loaded()
        
        try:
            # whisper期望的音频格式是float32，采样率16000
            # 如果采样率不同，需要重采样
            if self.config.audio_rate != 16000:
                import scipy.signal
                audio_data = scipy.signal.resample(
                    audio_data, 
                    int(len(audio_data) * 16000 / self.config.audio_rate)
                )
            
            # 确保音频数据格式正确
            audio_data = audio_data.astype(np.float32)
            
            # 使用whisper转录
            result = self.model.transcribe(audio_data)
            text = result["text"].strip()
            
            logging.info(f"语音转文字结果: {text}")
            return text
            
        except Exception as e:
            logging.error(f"语音转文字失败: {e}")
            return ""
    
    def transcribe_file(self, audio_file_path: str) -> str:
        """
        将音频文件转换为文字
        Args:
            audio_file_path: 音频文件路径
        Returns:
            转换后的文字
        """
        self._ensure_model_loaded()
        
        try:
            result = self.model.transcribe(audio_file_path)
            text = result["text"].strip()
            logging.info(f"文件语音转文字结果: {text}")
            return text
        except Exception as e:
            logging.error(f"文件语音转文字失败: {e}")
            return ""


class AudioPlayer:
    """音频播放器"""
    
    def __init__(self):
        self.config = get_config()
        self._initialized = False
    
    def _ensure_initialized(self):
        """确保pygame mixer已初始化"""
        if not self._initialized:
            try:
                pygame.mixer.init()
                self._initialized = True
                logging.info("音频播放器初始化成功")
            except Exception as e:
                logging.error(f"音频播放器初始化失败: {e}")
                raise
    
    def play_audio_blob(self, blob: Any):
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
_audio_recorder: AudioRecorder = None
_speech_to_text: SpeechToText = None


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


def get_audio_recorder() -> AudioRecorder:
    """获取音频录制器实例"""
    global _audio_recorder
    if _audio_recorder is None:
        _audio_recorder = AudioRecorder()
    return _audio_recorder


def get_speech_to_text() -> SpeechToText:
    """获取语音转文字实例"""
    global _speech_to_text
    if _speech_to_text is None:
        _speech_to_text = SpeechToText()
    return _speech_to_text


def generate_and_play_voice(text: str, voice_name: str = "Kore") -> None:
    """便捷函数：生成并播放语音"""
    get_voice_generator().generate_and_play(text, voice_name)


def record_and_transcribe() -> str:
    """便捷函数：录音并转换为文字"""
    recorder = get_audio_recorder()
    stt = get_speech_to_text()
    
    # 录音直到静音
    audio_data = recorder.record_until_silence()
    
    if audio_data is not None and len(audio_data) > 0:
        # 转换为文字
        return stt.transcribe_audio_data(audio_data)
    else:
        logging.warning("没有录制到音频数据")
        return "" 