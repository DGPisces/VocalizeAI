"""
统一日志管理模块
提供对话日志、AI反思日志和系统日志的统一管理
"""
import logging
import os
import datetime
from typing import Optional, List
from pathlib import Path

from .config import get_config


class DialogueLogger:
    """对话日志管理器"""
    
    def __init__(self, log_path: Optional[str] = None):
        self.config = get_config()
        self.log_path = log_path or self.config.ai_generated_log
        self._ensure_log_dir()
    
    def _ensure_log_dir(self) -> None:
        """确保日志目录存在"""
        self.config.ensure_log_dir()
    
    def log_entry(self, speaker: str, content: str) -> None:
        """记录对话条目"""
        timestamp = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        try:
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(f"==== {timestamp} ====\n")
                f.write(f"[{speaker}]: {content.strip()}\n")
        except Exception as e:
            logging.error(f"写入对话日志失败: {e}")
    
    def clear_log(self) -> None:
        """清空对话日志"""
        try:
            with open(self.log_path, "w", encoding="utf-8") as f:
                f.truncate(0)
        except Exception as e:
            logging.error(f"清空对话日志失败: {e}")
    
    def read_log(self) -> str:
        """读取完整的对话日志"""
        try:
            with open(self.log_path, "r", encoding="utf-8") as f:
                return f.read()
        except Exception as e:
            logging.error(f"读取对话日志失败: {e}")
            return ""


class ReflectionLogger:
    """AI反思日志管理器"""
    
    def __init__(self, log_path: Optional[str] = None):
        self.config = get_config()
        self.log_path = log_path or self.config.reflection_log
        self._ensure_log_dir()
    
    def _ensure_log_dir(self) -> None:
        """确保日志目录存在"""
        self.config.ensure_log_dir()
    
    def add_reflection(self, reflection: str, is_refined: bool = False) -> None:
        """添加反思记录"""
        timestamp = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        suffix = " (精炼)" if is_refined else ""
        
        try:
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(f"\n==== {timestamp} ===={suffix}\n")
                f.write(reflection.strip() + "\n")
        except Exception as e:
            logging.error(f"写入反思日志失败: {e}")
    
    def get_latest_reflection(self) -> str:
        """获取最新的反思记录"""
        try:
            with open(self.log_path, "r", encoding="utf-8") as f:
                lines = f.readlines()
            
            idxs = [i for i, line in enumerate(lines) if line.startswith("====")]
            if not idxs:
                return ""
                
            last_idx = idxs[-1]
            next_idx = next((i for i in idxs if i > last_idx), len(lines))
            reflection = "".join(lines[last_idx+1:next_idx]).strip()
            return reflection
        except Exception as e:
            logging.error(f"读取反思日志失败: {e}")
            return ""
    
    def get_all_reflections(self) -> List[str]:
        """获取所有反思记录"""
        try:
            with open(self.log_path, "r", encoding="utf-8") as f:
                lines = f.readlines()
            
            idxs = [i for i, line in enumerate(lines) if line.startswith("====")]
            if not idxs:
                return []
            
            reflections = []
            for i in range(len(idxs)):
                start = idxs[i] + 1
                end = idxs[i+1] if i+1 < len(idxs) else len(lines)
                reflections.append("".join(lines[start:end]).strip())
            
            return reflections
        except Exception as e:
            logging.error(f"读取所有反思记录失败: {e}")
            return []
    
    def refine_reflections(self, refined_content: str) -> None:
        """用精炼后的内容覆盖原文件"""
        try:
            with open(self.log_path, "w", encoding="utf-8") as f:
                timestamp = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                f.write(f"\n==== {timestamp} ==== (精炼)\n")
                f.write(refined_content.strip() + "\n")
        except Exception as e:
            logging.error(f"精炼反思日志失败: {e}")


def setup_logging(level: int = logging.INFO) -> None:
    """设置系统日志"""
    logging.basicConfig(
        level=level,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler('logs/system.log', encoding='utf-8')
        ]
    )


# 全局日志实例
dialogue_logger = DialogueLogger()
reflection_logger = ReflectionLogger()


def get_dialogue_logger() -> DialogueLogger:
    """获取对话日志实例"""
    return dialogue_logger


def get_reflection_logger() -> ReflectionLogger:
    """获取反思日志实例"""
    return reflection_logger 