"""STT 服务接口契约：流式转写 + 多语言检测 + utterance 边界。

跨语场景的 language 字段语义：
- 顶层 ``Transcript.language`` 表示该 utterance 的"主语言"——即该句话整体上更接近哪种
  语言；用于下游 dialogue 层的语言路由（决定 LLM system prompt 与 TTS 声纹）。
- 中英混说时若需要 per-segment 的语言标注（例如"明天 7 点 reservation 4 people"），
  使用可选的 ``segments`` 字段。
- partial transcript 在语言识别尚未稳定时返回 ``language=None``；调用方应等到 final
  transcript 出现后再做语言路由决策，避免 partial 抖动导致来回切换 voice profile。

utterance 边界语义：
- 同一句话产生的所有 partial（``is_final=False``）和最终的 final（``is_final=True``）
  共享同一个 ``utterance_id``。
- ``utterance_id`` 在单次 ``stream_transcribe()`` 调用范围内单调递增；不同次调用之间
  不保证连续。

cancellation：
- 调用方通过对返回的 AsyncIterator 调用 ``aclose()`` 触发 STT 服务停止识别。
- 实现是否真的关闭上游 WebSocket / 释放 provider session 由实现类负责；建议在
  ``aclose`` 路径上发送 finalize 信号、然后关闭 socket，避免上游会话继续占用。

实现类应额外提供 ``async health_check() -> bool`` 方法供 Phase 6 编排器监控。
"""
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass
class TranscriptSegment:
    """跨语 utterance 中的一段，标注其所属语言。

    ``start_time`` / ``end_time`` 单位为秒，相对于该 utterance 的开始（不是相对于
    整次对话），便于实现侧从底层模型的字级时间戳直接构造。
    """

    text: str
    language: str
    start_time: float  # 自该 utterance 开始的秒数
    end_time: float


@dataclass
class Transcript:
    """单条转写结果。

    ``is_final=False`` 表示中间 partial；同一 utterance 的所有 partial+final 共享
    ``utterance_id``。
    """

    text: str
    is_final: bool
    confidence: float
    start_time: float                                # 自对话开始的秒数
    end_time: float                                  # 自对话开始的秒数
    utterance_id: int                                # 同一句话所有 partial+final 共享
    language: str | None = None                      # None 表示尚未检测出
    segments: list[TranscriptSegment] | None = None  # 跨语 utterance 的可选拆解


@runtime_checkable
class STTService(Protocol):
    # 实现是 async generator（``async def`` + ``yield``）；Protocol 必须用
    # ``def -> AsyncIterator``，否则 mypy 当成返回 Coroutine 的函数，调用方
    # ``async for`` 会被推断成对 Coroutine 迭代而报错。
    def stream_transcribe(
        self, audio_chunks: AsyncIterator[bytes]
    ) -> AsyncIterator[Transcript]: ...
