"""``<think>...</think>`` 剥离回归测试。

背景见 .planning/debug/minimax-thinking-tts-leak.md：MiniMax-M2.7 把推理链
通过 ``delta.content`` 以 ``<think>...</think>`` 包裹流出，未过滤会被 pipeline
切段送 TTS，用户听到 AI 朗读自己的内部推理。

测试覆盖：
1. ``_ThinkingStripper`` 单元逻辑（同 chunk / 跨 chunk 边界 / 多段）
2. ``OpenAICompatClient.stream_chat`` 集成：用真实抓取的 SSE 形态喂入，
   断言下游收到的 ``TextDelta`` 不含 ``<think>`` / 推理英文，仅含中文回答。
3. 无 ``<think>`` 标签的纯文本流不受影响（DeepSeek / OpenAI 兼容）。
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

from vocalize.llm.base import ChatMessage, FinishChunk, TextDelta
from vocalize.llm.openai_compat import (
    OpenAICompatClient,
    OpenAICompatConfig,
    _ThinkingStripper,
)


# ---------------------------------------------------------------------------
# 复用 test_llm_openai_compat.py 风格的 fakes（独立小拷贝，避免跨文件 import）
# ---------------------------------------------------------------------------
class FakeStream:
    def __init__(self, chunks: list[Any]) -> None:
        self._chunks = list(chunks)
        self.close_calls = 0

    def __aiter__(self) -> "FakeStream":
        return self

    async def __anext__(self) -> Any:
        if not self._chunks:
            raise StopAsyncIteration
        return self._chunks.pop(0)

    async def close(self) -> None:
        self.close_calls += 1


def _delta_text(text: str, finish: str | None = None) -> Any:
    delta = SimpleNamespace(content=text, tool_calls=None)
    choice = SimpleNamespace(delta=delta, finish_reason=finish, index=0)
    return SimpleNamespace(choices=[choice], usage=None)


def _make_client() -> OpenAICompatClient:
    return OpenAICompatClient(
        OpenAICompatConfig(
            api_key="sk-test",
            base_url="https://api.minimaxi.com/v1",
            model="MiniMax-M2.7",
            request_timeout=5.0,
            max_retries=0,
        )
    )


# ---------------------------------------------------------------------------
# _ThinkingStripper 单元测试
# ---------------------------------------------------------------------------
def test_stripper_no_think_passthrough() -> None:
    """无 ``<think>`` 标签时输出 == 输入（除最后可能 hold 住部分前缀的尾巴）。"""
    s = _ThinkingStripper()
    out1 = s.feed("Hello, world!")
    out2 = s.feed(" Goodbye.")
    out3 = s.flush()
    assert out1 + out2 + out3 == "Hello, world! Goodbye."


def test_stripper_single_delta_with_think_block() -> None:
    """同一 delta 内完整的 ``<think>...</think>`` 块被吞，外部文本保留。"""
    s = _ThinkingStripper()
    out = s.feed("hi <think>internal reasoning</think> bye")
    out += s.flush()
    assert out == "hi  bye"


def test_stripper_think_at_chunk_boundaries() -> None:
    """``<think>`` 与 ``</think>`` 标签拆在多个 delta 之间仍能正确识别。"""
    s = _ThinkingStripper()
    parts = [
        "answer ",
        "<thi",          # 标签前半
        "nk>secret reasoning continues",
        " more reasoning",
        "</thi",         # close 标签前半
        "nk>real reply",
    ]
    out = "".join(s.feed(p) for p in parts)
    out += s.flush()
    assert out == "answer real reply"


def test_stripper_minimax_real_sse_shape() -> None:
    """复刻 MiniMax-M2.7 实测 SSE：标签独占 chunk，推理英文在中间，回答中文在后。

    抓自 2026-05-03 实际 curl 流（见 debug session Evidence）。
    """
    s = _ThinkingStripper()
    minimax_deltas = [
        "<think>",
        "The user is asking about making a reservation. ",
        "I should ask for clarification about what type of booking they need,",
        " and gather the necessary details.\n",
        "</think>",
        "\n\n您好！很乐意帮您处理预订，但请提供以下信息：\n",
        "1. 预订类型\n2. 人数",
    ]
    out = "".join(s.feed(d) for d in minimax_deltas)
    out += s.flush()
    assert "<think>" not in out
    assert "</think>" not in out
    assert "reasoning" not in out.lower()
    assert "user is asking" not in out.lower()
    assert "您好" in out
    assert "预订类型" in out


def test_stripper_unterminated_think_drops_tail() -> None:
    """流以 ``<think>`` 中途结束（缺 ``</think>``）→ 残留 buffer 必须丢弃。

    比"漏掉模型未闭合的尾段"更糟糕的是把推理泄漏到 TTS。这里选择前者。
    """
    s = _ThinkingStripper()
    out = s.feed("greeting <think>partial reasoning never closed")
    out += s.flush()
    assert out == "greeting "


def test_stripper_multiple_think_blocks() -> None:
    """一个 turn 内出现多个 ``<think>...</think>`` 块都要被吞。"""
    s = _ThinkingStripper()
    out = s.feed("a <think>r1</think> b <think>r2</think> c")
    out += s.flush()
    assert out == "a  b  c"


# ---------------------------------------------------------------------------
# OpenAICompatClient 集成测试：从 client 出口看是否还有 thinking 泄漏
# ---------------------------------------------------------------------------
async def test_stream_chat_strips_minimax_thinking() -> None:
    """喂入真实 MiniMax SSE 形态，断言 TextDelta 文本不含 ``<think>`` 与推理英文。"""
    client = _make_client()
    # 与 _stripper_minimax_real_sse_shape 同一份录像，加上结束哨兵
    stream = FakeStream([
        _delta_text("<think>"),
        _delta_text("The user is asking about making a reservation. "),
        _delta_text("I should ask for clarification about what type of booking,"),
        _delta_text(" and gather the necessary details.\n"),
        _delta_text("</think>"),
        _delta_text("\n\n您好！很乐意帮您处理预订，但请提供以下信息：\n"),
        _delta_text("1. 预订类型\n2. 人数", finish="stop"),
    ])
    create = AsyncMock(return_value=stream)
    with patch.object(client._client.chat.completions, "create", new=create):
        it = client.stream_chat([ChatMessage(role="user", content="预订")])
        out = [c async for c in it]

    text_chunks = [c for c in out if isinstance(c, TextDelta)]
    finish_chunks = [c for c in out if isinstance(c, FinishChunk)]
    full = "".join(c.text for c in text_chunks)

    assert finish_chunks and finish_chunks[0].reason == "stop"
    assert "<think>" not in full
    assert "</think>" not in full
    assert "reasoning" not in full.lower()
    assert "user is asking" not in full.lower()
    assert "您好" in full
    assert "预订类型" in full


async def test_stream_chat_no_think_unchanged() -> None:
    """普通模型（无 ``<think>``）的流必须 byte-for-byte 透传，回归保护。"""
    client = _make_client()
    stream = FakeStream([
        _delta_text("Hello "),
        _delta_text("world"),
        _delta_text("!", finish="stop"),
    ])
    create = AsyncMock(return_value=stream)
    with patch.object(client._client.chat.completions, "create", new=create):
        it = client.stream_chat([ChatMessage(role="user", content="hi")])
        out = [c async for c in it]

    text_chunks = [c for c in out if isinstance(c, TextDelta)]
    full = "".join(c.text for c in text_chunks)
    assert full == "Hello world!"


async def test_stream_chat_strip_across_chunk_boundary() -> None:
    """``<think>`` 标签在两个 delta 之间被拆开，client 层仍正确剥离。"""
    client = _make_client()
    stream = FakeStream([
        _delta_text("answer "),
        _delta_text("<thi"),               # tag 前半
        _delta_text("nk>private thought</thi"),
        _delta_text("nk>real reply.", finish="stop"),
    ])
    create = AsyncMock(return_value=stream)
    with patch.object(client._client.chat.completions, "create", new=create):
        it = client.stream_chat([ChatMessage(role="user", content="x")])
        out = [c async for c in it]

    full = "".join(c.text for c in out if isinstance(c, TextDelta))
    assert "<think>" not in full
    assert "private thought" not in full
    assert full == "answer real reply."
