"""配置管理模块。

统一加载 .env / 环境变量到一个 dataclass，供 pipeline、各 service client 注入。
全局单例 ``get_config()`` 是惰性的：首次调用时才从 env 加载；测试可调
``reset_config()`` 在 monkeypatch env 之后重读。
"""
import logging
import os
from dataclasses import dataclass
from typing import Literal

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None  # type: ignore[assignment]


def _int_env(name: str, default: int) -> int:
    """读取整数环境变量；空字符串或非法值时回退到 default。"""
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        logging.warning(
            "环境变量 %s=%r 不是合法整数，使用默认值 %d", name, raw, default
        )
        return default


@dataclass
class Config:
    """应用配置类。"""

    # LLM (OpenAI-compatible) 配置
    openai_api_key: str | None = None
    openai_base_url: str = "https://api.deepseek.com/v1"
    openai_model: str = "deepseek-chat"

    # GPU 推理节点（Tailscale 内网地址）。空串=未配置；`localhost` 是 Phase 0.5
    # 同机部署的合法值，validate_for_phase("gpu") 不会把它判为缺失。
    gpu_host: str = ""
    sensevoice_ws_port: int = 8000
    cosyvoice_ws_port: int = 8001

    # Pi 生产服务（Phase 4.5）
    orchestrator_listen_port: int = 8080

    # 默认语言策略（用户首句无法判断时的兜底）
    default_language: str = "zh"

    # 日志配置
    log_dir: str = "logs"

    @classmethod
    def from_env(cls) -> "Config":
        """从环境变量和 .env 文件加载配置。"""
        if load_dotenv is not None:
            load_dotenv()

        return cls(
            openai_api_key=os.getenv("OPENAI_API_KEY"),
            openai_base_url=os.getenv("OPENAI_BASE_URL", cls.openai_base_url),
            openai_model=os.getenv("OPENAI_MODEL", cls.openai_model),
            gpu_host=os.getenv("GPU_HOST", cls.gpu_host),
            sensevoice_ws_port=_int_env("SENSEVOICE_WS_PORT", cls.sensevoice_ws_port),
            cosyvoice_ws_port=_int_env("COSYVOICE_WS_PORT", cls.cosyvoice_ws_port),
            orchestrator_listen_port=_int_env(
                "ORCHESTRATOR_LISTEN_PORT", cls.orchestrator_listen_port
            ),
            default_language=os.getenv("DEFAULT_LANGUAGE", cls.default_language),
            log_dir=os.getenv("LOG_DIR", cls.log_dir),
        )

    def validate_for_phase(
        self, phase: Literal["llm", "gpu"]
    ) -> list[str]:
        """返回该 phase 缺失的环境变量名列表；空列表代表 OK。

        用法：在 Phase N 启动入口调用 ``cfg.validate_for_phase("llm")``，缺啥就
        提示啥；早期 phase 不应该被晚期 phase 的缺项报错噪声打扰。
        """
        missing: list[str] = []
        if phase == "llm":
            if not self.openai_api_key:
                missing.append("OPENAI_API_KEY")
        elif phase == "gpu":
            if not self.gpu_host:
                missing.append("GPU_HOST")
        return missing

    def get_missing_configs(self) -> list[str]:
        """返回缺失的必填配置项名称（向后兼容；等价于 Phase 0 的 LLM 校验）。"""
        return self.validate_for_phase("llm")

    def ensure_log_dir(self) -> None:
        """确保日志目录存在。"""
        if not os.path.exists(self.log_dir):
            os.makedirs(self.log_dir, exist_ok=True)


# 全局配置实例（惰性）
_config: Config | None = None


def get_config() -> Config:
    """惰性单例：首次调用时从 env 加载；测试可调 ``reset_config()`` 重置。"""
    global _config
    if _config is None:
        _config = Config.from_env()
    return _config


def reset_config() -> None:
    """测试用：清掉缓存，下次 ``get_config()`` 重新读 env。"""
    global _config
    _config = None


def validate_config() -> bool:
    """验证配置并输出警告信息（Phase 0 默认按 LLM 阶段校验）。"""
    missing = get_config().validate_for_phase("llm")
    if missing:
        logging.warning("缺少以下环境变量: %s", ", ".join(missing))
        return False
    return True
