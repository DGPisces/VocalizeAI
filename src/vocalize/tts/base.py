"""TTS 服务接口契约：流式合成 + 跨语 per-chunk 切换 + 句末边界标记。

输入：``AsyncIterator[TextChunk]``，每个 chunk 携带 ``language`` 让句内可切语言；
``is_final_segment=True`` 提示实现可 flush 当前句以触发自然韵律边界（比如句末降调、
合理停顿）。

输出：``AsyncIterator[bytes]`` 是按 ``output_sample_rate`` / ``output_encoding`` 编码的
连续音频流；具体格式由实现类暴露的属性决定，让下游 pipeline 在送入不同 transport
(e.g. telephony providers may need μ-law 8kHz) before resampling/transcoding.

实现类应额外提供 ``async health_check() -> bool`` 方法供 Phase 6 编排器监控。
"""
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from vocalize.transports.base import AudioEncoding


@dataclass
class TextChunk:
    """合成输入的文本片段。"""

    text: str
    language: str = "zh"             # 跨语场景由 dialogue/language.py 路由
    is_final_segment: bool = False   # True = 当前句末，可 flush


@runtime_checkable
class TTSService(Protocol):
    output_sample_rate: int          # 实现暴露的输出采样率（如 16000、24000）
    output_encoding: AudioEncoding   # 实现暴露的字节流编码（通常 "pcm_s16le"）

    # 实现是 async generator（``async def`` + ``yield``）；Protocol 必须用
    # ``def -> AsyncIterator``，否则 mypy 当成返回 Coroutine 的函数。
    def stream_synthesize(
        self, text_chunks: AsyncIterator[TextChunk]
    ) -> AsyncIterator[bytes]: ...
