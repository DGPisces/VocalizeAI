"""OpenAICompatClient 协议层测试。

策略：在 ``openai.AsyncOpenAI`` SDK 边界 mock 掉 ``chat.completions.create``，
不下到 httpx 层，避免依赖网络/真实 endpoint。覆盖：

- 纯文本流的 TextDelta + FinishChunk(reason="stop") + usage 提取
- 单个 tool call 的多 chunk 重组（首 chunk 带 id+name，后续仅 args delta）
- 并行 tool calls 用 ``tool_call_index`` 区分
- ``aclose()`` 触发底层 stream.close()
- 网络错误重试 + 4xx 立即抛 + 401 wrapped + finish_reason="length"
- ChatMessage → OpenAI dict 翻译
- health_check OK / fail
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import openai
import pytest

from vocalize.llm.base import (
    ChatMessage,
    FinishChunk,
    TextDelta,
    ToolCall,
    ToolCallDelta,
    ToolDef,
)
from vocalize.llm.openai_compat import (
    LLMServiceError,
    OpenAICompatClient,
    OpenAICompatConfig,
    _chat_message_to_openai,
    _tool_def_to_openai,
)


# ---------------------------------------------------------------------------
# Fakes / helpers
# ---------------------------------------------------------------------------
class FakeStream:
    """模拟 ``openai.AsyncStream``：持有一串 chunk，记录 close 调用次数。"""

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


def _delta_text(text: str, finish: str | None = None, usage: Any = None) -> Any:
    """构造一个 SDK 风格的 ChatCompletionChunk 对象（用 SimpleNamespace mock）。"""
    delta = SimpleNamespace(content=text, tool_calls=None)
    choice = SimpleNamespace(delta=delta, finish_reason=finish, index=0)
    return SimpleNamespace(choices=[choice], usage=usage)


def _delta_tool_call(
    index: int,
    tc_id: str | None,
    name: str | None,
    args: str,
    finish: str | None = None,
) -> Any:
    fn = SimpleNamespace(name=name, arguments=args)
    tc = SimpleNamespace(index=index, id=tc_id, function=fn, type="function")
    delta = SimpleNamespace(content=None, tool_calls=[tc])
    choice = SimpleNamespace(delta=delta, finish_reason=finish, index=0)
    return SimpleNamespace(choices=[choice], usage=None)


def _delta_tool_calls_multi(tcs: list[Any], finish: str | None = None) -> Any:
    delta = SimpleNamespace(content=None, tool_calls=tcs)
    choice = SimpleNamespace(delta=delta, finish_reason=finish, index=0)
    return SimpleNamespace(choices=[choice], usage=None)


def _make_client() -> OpenAICompatClient:
    return OpenAICompatClient(
        OpenAICompatConfig(
            api_key="sk-test",
            base_url="https://api.deepseek.com/v1",
            model="deepseek-chat",
            request_timeout=5.0,
            max_retries=2,
        )
    )


def _patch_create(client: OpenAICompatClient, mock: Any) -> Any:
    """把 client._client.chat.completions.create 替换成给定的 AsyncMock。"""
    return patch.object(
        client._client.chat.completions, "create", new=mock,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
async def test_stream_text_only() -> None:
    client = _make_client()
    usage = SimpleNamespace(prompt_tokens=3, completion_tokens=5, total_tokens=8)
    stream = FakeStream([
        _delta_text("Hello "),
        _delta_text("world"),
        _delta_text("", finish="stop", usage=usage),
    ])
    create = AsyncMock(return_value=stream)
    with _patch_create(client, create):
        it = client.stream_chat([ChatMessage(role="user", content="hi")])
        out = [c async for c in it]

    assert out == [
        TextDelta(text="Hello "),
        TextDelta(text="world"),
        FinishChunk(
            reason="stop",
            usage={"prompt_tokens": 3, "completion_tokens": 5, "total_tokens": 8},
        ),
    ]
    assert stream.close_calls == 1
    create.assert_awaited_once()
    kwargs = create.await_args.kwargs
    assert kwargs["model"] == "deepseek-chat"
    assert kwargs["stream"] is True


async def test_stream_tool_calls_single() -> None:
    client = _make_client()
    stream = FakeStream([
        _delta_tool_call(0, "call_1", "book_table", ""),
        _delta_tool_call(0, None, None, '{"party":'),
        _delta_tool_call(0, None, None, ' 4}', finish="tool_calls"),
    ])
    create = AsyncMock(return_value=stream)
    with _patch_create(client, create):
        it = client.stream_chat([ChatMessage(role="user", content="book")])
        out = [c async for c in it]

    assert len(out) == 4
    assert out[0] == ToolCallDelta(
        tool_call_index=0, tool_call_id="call_1",
        name="book_table", arguments_delta="",
    )
    assert out[1] == ToolCallDelta(
        tool_call_index=0, tool_call_id=None,
        name=None, arguments_delta='{"party":',
    )
    assert out[2] == ToolCallDelta(
        tool_call_index=0, tool_call_id=None,
        name=None, arguments_delta=' 4}',
    )
    assert out[3] == FinishChunk(reason="tool_calls", usage=None)


async def test_stream_tool_calls_parallel() -> None:
    client = _make_client()
    # interleaved: chunk1 has tc index=0 head, chunk2 has tc index=1 head,
    # chunk3 has both indices' arg deltas
    fn0_head = SimpleNamespace(name="book_table", arguments="")
    tc0_head = SimpleNamespace(index=0, id="call_a", function=fn0_head, type="function")
    fn1_head = SimpleNamespace(name="cancel_table", arguments="")
    tc1_head = SimpleNamespace(index=1, id="call_b", function=fn1_head, type="function")

    fn0_arg = SimpleNamespace(name=None, arguments='{"party":2}')
    tc0_arg = SimpleNamespace(index=0, id=None, function=fn0_arg, type="function")
    fn1_arg = SimpleNamespace(name=None, arguments='{"id":"x"}')
    tc1_arg = SimpleNamespace(index=1, id=None, function=fn1_arg, type="function")

    stream = FakeStream([
        _delta_tool_calls_multi([tc0_head]),
        _delta_tool_calls_multi([tc1_head]),
        _delta_tool_calls_multi([tc0_arg, tc1_arg], finish="tool_calls"),
    ])
    create = AsyncMock(return_value=stream)
    with _patch_create(client, create):
        it = client.stream_chat([ChatMessage(role="user", content="x")])
        out = [c async for c in it]

    tool_chunks = [c for c in out if isinstance(c, ToolCallDelta)]
    assert [c.tool_call_index for c in tool_chunks] == [0, 1, 0, 1]
    assert tool_chunks[0].name == "book_table" and tool_chunks[0].tool_call_id == "call_a"
    assert tool_chunks[1].name == "cancel_table" and tool_chunks[1].tool_call_id == "call_b"
    assert tool_chunks[2].arguments_delta == '{"party":2}'
    assert tool_chunks[3].arguments_delta == '{"id":"x"}'
    assert isinstance(out[-1], FinishChunk)
    assert out[-1].reason == "tool_calls"


async def test_aclose_propagates_to_stream() -> None:
    client = _make_client()
    stream = FakeStream([
        _delta_text("partial "),
        _delta_text("more "),
        _delta_text("more"),
        _delta_text("", finish="stop"),
    ])
    create = AsyncMock(return_value=stream)
    with _patch_create(client, create):
        it = client.stream_chat([ChatMessage(role="user", content="hi")])
        # consume only one chunk, then aclose
        first = await it.__anext__()
        assert isinstance(first, TextDelta)
        await it.aclose()

    assert stream.close_calls == 1


async def test_retry_on_connection_error() -> None:
    client = _make_client()
    good_stream = FakeStream([_delta_text("ok", finish="stop")])
    err = openai.APIConnectionError(request=httpx.Request("POST", "http://x"))
    create = AsyncMock(side_effect=[err, good_stream])
    with _patch_create(client, create), patch(
        "vocalize.llm.openai_compat.asyncio.sleep", new=AsyncMock(),
    ):
        it = client.stream_chat([ChatMessage(role="user", content="hi")])
        out = [c async for c in it]

    assert create.await_count == 2
    assert out == [TextDelta(text="ok"), FinishChunk(reason="stop", usage=None)]


async def test_no_retry_on_4xx() -> None:
    client = _make_client()
    response = httpx.Response(
        401,
        request=httpx.Request("POST", "http://x"),
        json={"error": {"message": "bad key"}},
    )
    err = openai.AuthenticationError(
        message="bad key", response=response, body=None,
    )
    create = AsyncMock(side_effect=err)
    with _patch_create(client, create):
        with pytest.raises(LLMServiceError) as ei:
            it = client.stream_chat([ChatMessage(role="user", content="hi")])
            async for _ in it:
                pass

    assert ei.value.upstream_status == 401
    assert "OPENAI_API_KEY" in str(ei.value)
    assert create.await_count == 1


async def test_no_retry_on_other_4xx() -> None:
    """非 401/429 的 4xx（如 400）也应立即失败而不是无限重试。"""
    client = _make_client()
    response = httpx.Response(
        400,
        request=httpx.Request("POST", "http://x"),
        json={"error": {"message": "bad request"}},
    )
    err = openai.BadRequestError(
        message="bad request", response=response, body=None,
    )
    create = AsyncMock(side_effect=err)
    with _patch_create(client, create):
        with pytest.raises(LLMServiceError) as ei:
            it = client.stream_chat([ChatMessage(role="user", content="hi")])
            async for _ in it:
                pass

    assert ei.value.upstream_status == 400
    assert create.await_count == 1


async def test_finish_reason_length() -> None:
    client = _make_client()
    stream = FakeStream([
        _delta_text("partial answer cut "),
        _delta_text("off", finish="length"),
    ])
    create = AsyncMock(return_value=stream)
    with _patch_create(client, create):
        it = client.stream_chat([ChatMessage(role="user", content="hi")])
        out = [c async for c in it]

    finish = out[-1]
    assert isinstance(finish, FinishChunk)
    assert finish.reason == "length"


def test_chat_message_to_openai_translation() -> None:
    tool_msg = ChatMessage(
        role="tool", content="result-json", tool_call_id="abc",
    )
    assert _chat_message_to_openai(tool_msg) == {
        "role": "tool", "content": "result-json", "tool_call_id": "abc",
    }

    asst_msg = ChatMessage(
        role="assistant", content="hi there", name="book_table",
    )
    assert _chat_message_to_openai(asst_msg) == {
        "role": "assistant", "content": "hi there", "name": "book_table",
    }

    user_msg = ChatMessage(role="user", content="plain")
    assert _chat_message_to_openai(user_msg) == {
        "role": "user", "content": "plain",
    }


def test_tool_def_to_openai_translation() -> None:
    td = ToolDef(
        name="book_table",
        description="reserve a table",
        parameters={"type": "object", "properties": {"party": {"type": "integer"}}},
    )
    assert _tool_def_to_openai(td) == {
        "type": "function",
        "function": {
            "name": "book_table",
            "description": "reserve a table",
            "parameters": {
                "type": "object",
                "properties": {"party": {"type": "integer"}},
            },
        },
    }


async def test_health_check_ok() -> None:
    client = _make_client()
    create = AsyncMock(return_value=MagicMock())
    with _patch_create(client, create):
        ok = await client.health_check()
    assert ok is True
    kwargs = create.await_args.kwargs
    assert kwargs["stream"] is False
    assert kwargs["max_tokens"] == 1


async def test_health_check_transient_failure_returns_false() -> None:
    client = _make_client()
    create = AsyncMock(
        side_effect=openai.APIConnectionError(
            request=httpx.Request("POST", "http://x"),
        )
    )
    with _patch_create(client, create):
        ok = await client.health_check()
    assert ok is False


async def test_health_check_auth_error_propagates() -> None:
    """永久误配（401）必须上抛，让 Phase 6 监控区分 misconfig vs downtime。"""
    client = _make_client()
    response = httpx.Response(
        401,
        request=httpx.Request("POST", "http://x"),
        json={"error": {"message": "bad key"}},
    )
    err = openai.AuthenticationError(
        message="bad key", response=response, body=None,
    )
    create = AsyncMock(side_effect=err)
    with _patch_create(client, create):
        with pytest.raises(openai.AuthenticationError):
            await client.health_check()


async def test_extra_body_thinking_disabled_mode_for_stream() -> None:
    """非 thinking 模式下，流式请求附加 ``thinking:{type:disabled}``。"""
    client = OpenAICompatClient(
        OpenAICompatConfig(
            api_key="sk-test",
            base_url="https://api.deepseek.com",
            model="test-model",
            thinking_mode="disabled",
        )
    )
    create = AsyncMock(return_value=FakeStream([]))
    with _patch_create(client, create):
        async for _ in client.stream_chat([ChatMessage(role="user", content="hi")]):
            pass
    kwargs = create.await_args.kwargs
    assert kwargs["extra_body"] == {"thinking": {"type": "disabled"}}


async def test_extra_body_thinking_disabled_mode_for_health_check() -> None:
    """health_check 在非 thinking 模式下也带 disable thinking flag。"""
    client = OpenAICompatClient(
        OpenAICompatConfig(
            api_key="sk-test",
            base_url="https://api.deepseek.com",
            model="test-model",
            thinking_mode="disabled",
        )
    )
    create = AsyncMock(return_value=MagicMock())
    with _patch_create(client, create):
        await client.health_check()
    kwargs = create.await_args.kwargs
    assert kwargs["extra_body"] == {"thinking": {"type": "disabled"}}


async def test_no_extra_body_when_thinking_enabled() -> None:
    """thinking enabled 表示不发送关闭 thinking 的额外字段。"""
    client = OpenAICompatClient(
        OpenAICompatConfig(
            api_key="sk-test",
            base_url="https://api.minimaxi.com/v1",
            model="MiniMax-M2.7",
            thinking_mode="enabled",
        )
    )
    create = AsyncMock(return_value=FakeStream([]))
    with _patch_create(client, create):
        async for _ in client.stream_chat([ChatMessage(role="user", content="hi")]):
            pass
    kwargs = create.await_args.kwargs
    assert "extra_body" not in kwargs


async def test_enabled_mode_omits_extra_body_for_deepseek_v4() -> None:
    """即使是 DeepSeek V4，用户选择 enabled 时也不强行关闭 thinking。"""
    client = OpenAICompatClient(
        OpenAICompatConfig(
            api_key="sk-test",
            base_url="https://api.deepseek.com/v1",
            model="deepseek-v4-flash",
            thinking_mode="enabled",
        )
    )
    create = AsyncMock(return_value=FakeStream([]))
    with _patch_create(client, create):
        async for _ in client.stream_chat([ChatMessage(role="user", content="hi")]):
            pass
    kwargs = create.await_args.kwargs
    assert "extra_body" not in kwargs


async def test_from_app_config_missing_key() -> None:
    from vocalize.config import Config

    cfg = Config(openai_api_key=None)
    with pytest.raises(LLMServiceError, match="OPENAI_API_KEY"):
        OpenAICompatClient.from_app_config(cfg)


async def test_from_app_config_ok() -> None:
    from vocalize.config import Config

    cfg = Config(
        openai_api_key="sk-x",
        openai_base_url="https://example.test/v1",
        openai_model="m",
    )
    client = OpenAICompatClient.from_app_config(cfg)
    assert client._config.api_key == "sk-x"
    assert client._config.base_url == "https://example.test/v1"
    assert client._config.model == "m"
    assert client._config.thinking_mode == "disabled"


async def test_tools_passed_to_create() -> None:
    client = _make_client()
    stream = FakeStream([_delta_text("ok", finish="stop")])
    create = AsyncMock(return_value=stream)
    tool = ToolDef(name="t", description="d", parameters={"type": "object"})
    with _patch_create(client, create):
        it = client.stream_chat(
            [ChatMessage(role="user", content="x")], tools=[tool],
        )
        async for _ in it:
            pass

    kwargs = create.await_args.kwargs
    assert kwargs["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "t", "description": "d", "parameters": {"type": "object"},
            },
        }
    ]


async def test_retry_exhausted_raises() -> None:
    """连续网络错误超过 max_retries 应抛 LLMServiceError。"""
    client = _make_client()  # max_retries=2 → 3 attempts total
    err = openai.APIConnectionError(request=httpx.Request("POST", "http://x"))
    create = AsyncMock(side_effect=[err, err, err])
    with _patch_create(client, create), patch(
        "vocalize.llm.openai_compat.asyncio.sleep", new=AsyncMock(),
    ):
        with pytest.raises(LLMServiceError, match="3 attempts"):
            it = client.stream_chat([ChatMessage(role="user", content="x")])
            async for _ in it:
                pass
    assert create.await_count == 3


def test_negative_max_retries_rejected() -> None:
    """max_retries 必须 >= 0，否则 attempts 循环会出现 raw AssertionError。"""
    with pytest.raises(LLMServiceError, match="max_retries must be >= 0"):
        OpenAICompatConfig(
            api_key="sk-x",
            base_url="https://x/v1",
            model="m",
            max_retries=-1,
        )


# ---------------------------------------------------------------------------
# Phase 4 Plan 02 — assistant tool_calls round-trip serialization (D-13)
# ---------------------------------------------------------------------------
def test_chat_message_to_openai_assistant_with_tool_calls() -> None:
    """assistant 消息携带 tool_calls 时：content 必须是 None（非 ""），且 tool_calls
    数组按 OpenAI/DeepSeek function-call 形状输出。
    """
    msg = ChatMessage(
        role="assistant",
        content="",
        tool_calls=[
            ToolCall(
                id="call_abc",
                name="update_booking_field",
                arguments='{"slot":"date","value":"2026-05-10"}',
            )
        ],
    )
    out = _chat_message_to_openai(msg)
    assert out["role"] == "assistant"
    assert out["content"] is None  # 关键：OpenAI 规范要求 null，不是 ""
    assert out["tool_calls"] == [
        {
            "id": "call_abc",
            "type": "function",
            "function": {
                "name": "update_booking_field",
                "arguments": '{"slot":"date","value":"2026-05-10"}',
            },
        }
    ]
    assert "tool_call_id" not in out


def test_chat_message_to_openai_tool_role_preserved() -> None:
    """role=tool 仍走老路径：content 透传字符串、tool_call_id 必填、不带 tool_calls。"""
    msg = ChatMessage(
        role="tool",
        content='{"ok":true}',
        tool_call_id="call_abc",
    )
    out = _chat_message_to_openai(msg)
    assert out["tool_call_id"] == "call_abc"
    assert out["content"] == '{"ok":true}'
    assert "tool_calls" not in out


def test_chat_message_to_openai_user_no_tool_keys() -> None:
    """普通 user 消息不应混入 tool_calls / tool_call_id 键。"""
    msg = ChatMessage(role="user", content="hi")
    out = _chat_message_to_openai(msg)
    assert "tool_calls" not in out
    assert "tool_call_id" not in out
    assert out["content"] == "hi"
    assert out["role"] == "user"


def test_chat_message_to_openai_multiple_tool_calls_preserve_order() -> None:
    """多个 ToolCall 必须按构造顺序原样进入 tool_calls 数组。"""
    msg = ChatMessage(
        role="assistant",
        content="",
        tool_calls=[
            ToolCall(id="call_a", name="update_booking_field", arguments='{"slot":"date"}'),
            ToolCall(id="call_b", name="assess_readiness_to_dial", arguments="{}"),
        ],
    )
    out = _chat_message_to_openai(msg)
    assert len(out["tool_calls"]) == 2
    assert [tc["id"] for tc in out["tool_calls"]] == ["call_a", "call_b"]
    assert out["tool_calls"][0]["function"]["name"] == "update_booking_field"
    assert out["tool_calls"][1]["function"]["name"] == "assess_readiness_to_dial"


async def test_stream_chat_break_triggers_close() -> None:
    """caller 用 ``break`` 提前结束迭代时，stream.close() 必须被调用一次。

    Phase 5 barge-in 依赖此行为：用户打断时，async generator 的 finally
    跑 ``stream.close()``，让远端停止生成。这里用 ``contextlib.aclosing``
    保证确定性触发（async-for 自动 GC 在测试里时机不可靠）。
    """
    from contextlib import aclosing

    client = _make_client()
    stream = FakeStream([
        _delta_text("a"),
        _delta_text("b"),
        _delta_text("c"),
        _delta_text("d"),
        _delta_text("e", finish="stop"),
    ])
    create = AsyncMock(return_value=stream)
    with _patch_create(client, create):
        async with aclosing(
            client.stream_chat([ChatMessage(role="user", content="hi")])
        ) as it:
            async for chunk in it:
                assert isinstance(chunk, TextDelta)
                break  # 拿到第一个 chunk 就提前退出

    assert stream.close_calls == 1
