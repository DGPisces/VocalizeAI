"""统一日志管理模块。

提供系统日志配置 (``setup_logging``) 和对话日志写入 (``DialogueLogger``)。
ReflectionLogger 已移除，将在 Phase 6 重新设计于 ``reflection/postcall.py``。
"""
import datetime
import logging
import os

from .config import get_config


def default_dialogue_log_path() -> str:
    """对话日志默认路径：<log_dir>/ai_generated_log.txt。

    用函数而非模块级常量，是为了配合 ``get_config()`` 的惰性单例：模块级常量会在
    import 时就把 ``log_dir`` 锁死，测试 monkeypatch env 后再 ``reset_config()``
    也无法影响该常量。
    """
    return os.path.join(get_config().log_dir, "ai_generated_log.txt")


class DialogueLogger:
    """对话日志管理器。

    向纯文本日志文件追加 ``==== 时间戳 ====\\n[角色]: 内容`` 段落，超过 max_lines
    自动清空（保留一条系统标记）。日志路径不依赖全局 config，调用方按需注入。

    线程/进程安全性：本 logger **不安全**。多通电话并发时，单一文件路径会出现写入
    交错或丢条目；正确做法是一通 call 一个 logger 实例，传入不同 ``log_path``
    （如 ``logs/call-{call_id}.log``）。Phase 6 重构时会切换到 ``aiofiles`` 的异步
    实现并加入按 call_id 路由。
    """

    def __init__(
        self,
        log_path: str | None = None,
        max_lines: int = 100,
    ) -> None:
        self.log_path = log_path or default_dialogue_log_path()
        self.max_lines = max_lines
        self._ensure_log_dir()

    def _ensure_log_dir(self) -> None:
        log_dir = os.path.dirname(self.log_path)
        if log_dir and not os.path.exists(log_dir):
            os.makedirs(log_dir, exist_ok=True)

    def _count_lines(self) -> int:
        try:
            if not os.path.exists(self.log_path):
                return 0
            with open(self.log_path, encoding="utf-8") as f:
                return sum(1 for _ in f)
        except Exception:
            return 0

    def _auto_cleanup(self) -> None:
        if self._count_lines() >= self.max_lines:
            try:
                with open(self.log_path, "w", encoding="utf-8") as f:
                    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    f.write(f"==== {timestamp} ====\n")
                    f.write("[系统]: 日志已自动清理\n")
            except Exception as e:
                logging.error(f"自动清理日志失败: {e}")

    def log_entry(self, speaker: str, content: str) -> None:
        """记录一条对话条目。"""
        self._auto_cleanup()
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(f"==== {timestamp} ====\n")
                f.write(f"[{speaker}]: {content.strip()}\n")
        except Exception as e:
            logging.error(f"写入对话日志失败: {e}")

    def clear_log(self) -> None:
        """清空对话日志。"""
        try:
            with open(self.log_path, "w", encoding="utf-8") as f:
                f.truncate(0)
        except Exception as e:
            logging.error(f"清空对话日志失败: {e}")

    def read_log(self) -> str:
        """读取完整的对话日志。"""
        try:
            with open(self.log_path, encoding="utf-8") as f:
                return f.read()
        except Exception as e:
            logging.error(f"读取对话日志失败: {e}")
            return ""


def setup_logging(level: int = logging.INFO, log_path: str | None = None) -> None:
    """初始化系统日志：默认写到 ``<log_dir>/system.log``，不输出控制台。

    ``log_path``: ``None`` 时使用 ``<config.log_dir>/system.log``；显式传入路径时
    优先（其父目录会被自动创建）。
    """
    cfg = get_config()
    cfg.ensure_log_dir()

    actual_path = log_path or os.path.join(cfg.log_dir, "system.log")
    parent = os.path.dirname(actual_path)
    if parent and not os.path.exists(parent):
        os.makedirs(parent, exist_ok=True)

    logging.getLogger().handlers.clear()
    logging.basicConfig(
        level=level,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        handlers=[
            logging.FileHandler(actual_path, encoding="utf-8"),
        ],
    )
