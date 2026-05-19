"""音频传输层接口契约：抽象本地 mic/speaker 与 telephony Media Streams (v2) 的差异。

设计要点：
- sample_rate / channels / encoding 三元组完整描述字节流的二进制格式，
  让 pipeline 在不同 transport（PCM16 16kHz mono、μ-law 8kHz mono）之间转换时
  无需扩展契约。
- 实现类必须在 ``__init__`` 给 ``sample_rate`` / ``channels`` / ``encoding`` 三个数据
  属性赋值；否则 ``isinstance(svc, AudioTransport)`` 在 ``runtime_checkable`` 模式下
  对类（而非实例）调用时静态过、运行时却找不到属性，造成 Phase 1 接入时难以排查的
  错误。强烈建议在测试里覆盖每个 transport 实例的这三个字段。
- 实现类应额外提供 ``async health_check() -> bool`` 方法供 Phase 6 编排器监控；
  不在 Protocol 上强制声明，是为了让早期实现可逐步补齐。
"""
from collections.abc import AsyncIterator
from typing import Literal, Protocol, runtime_checkable

AudioEncoding = Literal["pcm_s16le", "pcm_s16be", "mulaw", "alaw", "opus"]


@runtime_checkable
class AudioTransport(Protocol):
    """双向音频流传输抽象。"""

    sample_rate: int          # e.g. 8000, 16000, 24000
    channels: int             # 1 = mono；电话场景永远 mono
    encoding: AudioEncoding

    # input_stream 是 async generator（``async def`` + ``yield``）；Protocol 上
    # 必须用 ``def -> AsyncIterator``，否则 mypy 把它当作返回 coroutine 的函数。
    def input_stream(self) -> AsyncIterator[bytes]: ...
    async def output_stream(self, audio: AsyncIterator[bytes]) -> None: ...
    async def close(self) -> None: ...

    # Phase 4 D-04（hold filler audio）：clarification.py 在调用 UserChannel
    # 之前 / 之后调这两个钩子，让"transport 是否暂停往外发音频"成为协议级语义。
    # MicrophoneTransport 实现里是 log-only no-op（共享扬声器没有 hold 概念）；
    # v2 telephony transport will hook hold-filler audio here. Declared on
    # the Protocol rather than behind hasattr-gate so clarification.py has
    # a clean type contract without try/except fallbacks.
    # （不需要 try/except hasattr 兜底。）
    async def pause_outbound(self) -> None: ...
    async def resume_outbound(self) -> None: ...
