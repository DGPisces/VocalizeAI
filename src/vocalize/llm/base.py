"""LLM 服务接口契约：OpenAI-compatible 流式 chat completion + tool calling。

支持的供应商：DeepSeek（默认）、官方 OpenAI、Qwen DashScope、SenseNova、其他
OpenAI-compat。切换 provider 仅需改 ``.env`` 的 ``OPENAI_BASE_URL`` /
``OPENAI_API_KEY`` / ``OPENAI_MODEL``。

cancellation：
- 调用方应通过对返回的 AsyncIterator 调用 ``aclose()`` 中断流式生成。
- 实现需在底层 HTTP/SSE 连接关闭，避免 OpenAI server-side 继续生成（继续计费）。
  Phase 2 实测时务必验证：barge-in 触发后远端 token 计数停止增长。

实现类应额外提供 ``async health_check() -> bool`` 方法供 Phase 6 编排器监控。
"""
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Literal, Protocol, runtime_checkable


@dataclass
class ToolCall:
    """Re-assembled 完整工具调用（assistant 消息回传给模型用）。

    从 streaming 流的多个 ``ToolCallDelta`` 累积而成；orchestrator 在 finish_reason
    == "tool_calls" 时把累积结果包成 ``ToolCall`` 列表挂到 assistant ``ChatMessage``
    上回传，下一轮 LLM 才知道"上一轮我已经调用了哪个工具、要等 tool result"。
    No this field → DeepSeek/OpenAI loses tool dispatch context → infinite loop
    calling same tool.
    """

    id: str          # OpenAI 分配的 call id（如 ``call_abc``）；tool 结果消息以此回链
    name: str        # 函数名
    arguments: str   # 完整 JSON 字符串（保留模型原始输出，不重新序列化）


@dataclass
class ChatMessage:
    role: Literal["system", "user", "assistant", "tool"]
    content: str
    tool_call_id: str | None = None  # tool role 必填
    name: str | None = None
    # assistant 消息携带的工具调用列表（D-13 strict tool round-trip）。
    # 仅在 role="assistant" 且 finish_reason=="tool_calls" 的回传场景使用；
    # 序列化时见 ``openai_compat._chat_message_to_openai``：content 会变 None。
    tool_calls: list["ToolCall"] | None = None


@dataclass
class ToolDef:
    name: str
    description: str
    parameters: dict[str, Any]  # JSON schema


@dataclass
class TextDelta:
    """文本增量片段。"""

    text: str


@dataclass
class ToolCallDelta:
    """工具调用增量片段。

    OpenAI streaming 的 ``tool_calls`` 数组带 ``index`` 字段，多个并行 tool call 才能区分；
    早期 chunk 的 ``tool_call_id`` / ``name`` 可能尚未 resolved（None），后续 chunk 会
    填上 ``arguments_delta``。orchestrator 按 ``tool_call_index`` 累积同一 tool call 的
    所有 ``arguments_delta`` 拼出完整 JSON。
    """

    tool_call_index: int
    tool_call_id: str | None       # 早期 chunk 可能尚未 resolved
    name: str | None               # None for continuation chunks
    arguments_delta: str           # 增量 JSON 字符串


@dataclass
class FinishChunk:
    """流结束标记，携带原因和可选 usage。

    ``reason="tool_calls"`` 表示工具调用 args 已产完，orchestrator 可 reassemble
    并执行；``reason="length"`` 表示被 max_tokens 截断，调用方可决定是否重试。
    """

    reason: Literal["stop", "tool_calls", "length", "content_filter"]
    usage: dict[str, int] | None = None  # {"prompt_tokens": ..., "completion_tokens": ...}


LLMChunk = TextDelta | ToolCallDelta | FinishChunk


@runtime_checkable
class LLMService(Protocol):
    # 实现是 async generator（``async def`` + ``yield``）；Protocol 必须用
    # ``def -> AsyncIterator``，否则 mypy 当成返回 Coroutine 的函数。
    def stream_chat(
        self,
        messages: list[ChatMessage],
        tools: list[ToolDef] | None = None,
    ) -> AsyncIterator[LLMChunk]: ...
