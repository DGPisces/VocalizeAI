from collections.abc import AsyncIterator

import pytest

from vocalize.dialogue.relay import (
    RelayResult,
    merchant_text_to_user_lang,
    user_to_merchant,
)
from vocalize.llm.base import ChatMessage, LLMChunk, TextDelta


class _FakeLLM:
    def __init__(
        self,
        chunks: list[LLMChunk] | None = None,
        raise_exc: Exception | None = None,
    ) -> None:
        self._chunks = list(chunks or [])
        self._raise = raise_exc
        self.call_count = 0
        self.messages: list[ChatMessage] | None = None

    async def stream_chat(
        self,
        *,
        messages: list[ChatMessage],
    ) -> AsyncIterator[LLMChunk]:
        self.call_count += 1
        self.messages = messages
        if self._raise is not None:
            raise self._raise
        for chunk in self._chunks:
            yield chunk


@pytest.mark.asyncio
async def test_relay_skips_when_same_lang() -> None:
    llm = _FakeLLM()

    out = await merchant_text_to_user_lang("hello", src="en", dst="en", llm=llm)

    assert isinstance(out, RelayResult)
    assert out.translated == "hello"
    assert out.skipped is True
    assert out.failed is False
    assert llm.call_count == 0


@pytest.mark.asyncio
async def test_relay_calls_llm_when_cross_lingual() -> None:
    llm = _FakeLLM(chunks=[TextDelta("你"), TextDelta("好")])

    out = await merchant_text_to_user_lang("hello", src="en", dst="zh", llm=llm)

    assert out.translated == "你好"
    assert out.skipped is False
    assert out.failed is False
    assert llm.call_count == 1
    assert llm.messages is not None
    assert llm.messages[0].role == "system"
    assert llm.messages[1].content == "hello"


@pytest.mark.asyncio
async def test_relay_returns_failed_result_on_llm_exception() -> None:
    llm = _FakeLLM(raise_exc=RuntimeError("LLM unavailable"))

    out = await merchant_text_to_user_lang("hello", src="en", dst="zh", llm=llm)

    assert out.failed is True
    assert out.translated is None
    assert out.skipped is False


@pytest.mark.asyncio
async def test_user_to_merchant_happy_cross_lingual() -> None:
    llm = _FakeLLM(chunks=[TextDelta("你好"), TextDelta("世界")])

    out = await user_to_merchant("hello world", src="en", dst="zh", llm=llm)

    assert out.translated == "你好世界"
    assert out.skipped is False
    assert out.failed is False
    assert llm.call_count == 1
    assert llm.messages is not None
    assert llm.messages[0].role == "system"
    assert llm.messages[1].content == "hello world"


@pytest.mark.asyncio
async def test_user_to_merchant_same_lang_short_circuit() -> None:
    llm = _FakeLLM()

    out = await user_to_merchant("你好", src="zh", dst="zh", llm=llm)

    assert out.translated == "你好"
    assert out.skipped is True
    assert out.failed is False
    assert llm.call_count == 0


@pytest.mark.asyncio
async def test_user_to_merchant_failure_returns_failed() -> None:
    llm = _FakeLLM(raise_exc=RuntimeError("LLM unavailable"))

    out = await user_to_merchant("hello", src="en", dst="zh", llm=llm)

    assert out.failed is True
    assert out.translated is None
    assert out.skipped is False
