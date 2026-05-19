"""OpenAI-compatible 流式 LLM 客户端 (Phase 2)。

通过官方 ``openai`` SDK 的 ``AsyncOpenAI`` 连任意 OpenAI-compat endpoint
（DeepSeek 默认 / 官方 OpenAI / Qwen DashScope / SenseNova / MiniMax / ...），
把 SSE 流式 chat completion 转成 ``LLMService`` Protocol 定义的 ``LLMChunk`` 序列。

设计取舍：
- **重试**：SDK 自带的 ``max_retries`` 行为不透明（不区分 4xx vs 网络错误，
  也不向 caller 暴露重试次数）。这里把 SDK ``max_retries=0``，自己做显式重试：
  仅对 ``APIConnectionError`` / ``APITimeoutError`` / ``RateLimitError`` 重试，
  指数退避 0.5s * 2^attempt；其他 4xx 立即抛 ``LLMServiceError``。
- **cancellation**：返回的 ``AsyncIterator`` 在 ``aclose()`` 时会触发底层
  ``AsyncStream.close()``，关闭 SSE 连接，让远端停止生成（也停止计费）。
  这是 Phase 5 barge-in 正确性的前置条件。
- **assistant tool-call round-trip**：``ChatMessage`` dataclass 当前没有
  ``tool_calls`` 字段，这里仅 passthrough role/content/name/tool_call_id。
  TODO(phase-4)：orchestrator 需要把上一轮 assistant 的 tool_calls 喂回 model
  以延续上下文，届时 ``ChatMessage`` 会扩字段，本模块再补 translation。
- **thinking-chain 剥离**：部分 OpenAI-compat 模型（MiniMax-M 系、DeepSeek-R1 等）
  把内部推理（chain-of-thought）作为普通 ``delta.content`` 文本流出，并以
  ``<think>...</think>`` 显式包裹。下游 pipeline 会按句末标点切段送 TTS，
  导致用户听到 AI 朗读自己的英文推理。这里在 client 出口做无副作用的状态机
  剥离：没有 ``<think>`` 标签时 0 字符变更；有标签时按字面匹配吞掉标签
  及其内部内容。详见 ``_ThinkingStripper``。
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Literal, cast

import openai
from openai import AsyncOpenAI, AsyncStream
from openai.types.chat import ChatCompletionChunk

from vocalize.config import Config
from vocalize.llm.base import (
    ChatMessage,
    FinishChunk,
    LLMChunk,
    TextDelta,
    ToolCallDelta,
    ToolDef,
)

log = logging.getLogger(__name__)

_KNOWN_FINISH_REASONS: set[str] = {"stop", "tool_calls", "length", "content_filter"}

# ``<think>`` / ``</think>`` 标签字面值。MiniMax-M2.7 实测的 SSE 流以这两个
# 7-/8-字符序列开/闭推理段落（见 .planning/debug/minimax-thinking-tts-leak.md
# Evidence）。匹配是字面 + 大小写敏感的，不使用正则——避免误吞用户文本里
# 合法的 ``<think>`` 字串（罕见但不为零）。
_THINK_OPEN = "<think>"
_THINK_CLOSE = "</think>"

# 模型前缀 → 在请求体附加 ``thinking: {type: "disabled"}``。
# DeepSeek-V4 系列文档 (api-docs.deepseek.com/api/create-chat-completion)
# 明确写：``"thinking": {"type": "disabled"}`` 表示 use non-thinking model；
# 老别名 ``deepseek-chat`` 直接路由到 v4-flash 非思考模式，无需此字段，但加上
# 也无副作用（OpenAI-compat 服务端忽略未知字段）。
# MiniMax-M 系按文档此字段会被忽略（M2.7 server 永远会推理），故不加——
# client-side ``_ThinkingStripper`` 兜底剥离 <think>...</think>。
_DISABLE_THINKING_PREFIXES: tuple[str, ...] = (
    "deepseek-v4-",
    "deepseek-reasoner",
)


def _server_disable_thinking(model: str) -> bool:
    """返回 True 表示该模型支持 server-side ``thinking:{type:disabled}`` 关闭。"""
    return any(model.startswith(p) for p in _DISABLE_THINKING_PREFIXES)


def _potential_prefix_len(tail: str, target: str) -> int:
    """返回 tail 末尾作为 target 真前缀（不含完整匹配）的最大长度。

    用于跨 chunk 边界的部分标签检测：例如 tail="foo<thi", target="<think>"
    返回 4（``<thi`` 是 ``<think>`` 的真前缀），意味着这 4 个字符不能立刻
    输出，要 hold 住等下一个 delta 拼接。完整匹配（tail 末尾恰好是整个
    target）由主循环的 ``str.find`` 在下次 feed 时处理，所以这里上限是
    ``len(target)-1``。
    """
    n = min(len(tail), len(target) - 1)
    for k in range(n, 0, -1):
        if tail.endswith(target[:k]):
            return k
    return 0


class _ThinkingStripper:
    """流式过滤器：吞掉 ``<think>...</think>`` 块（含标签自身）。

    状态机：
    - ``in_thinking=False`` → 输出文本，遇到 ``<think>`` 切换到 thinking
    - ``in_thinking=True``  → 丢弃文本，遇到 ``</think>`` 切换回普通

    跨 chunk 边界处理（关键正确性 + 零回归）：
    - 假如标签被拆在两个 delta（``<thi`` + ``nk>`` / ``</thi`` + ``nk>``），
      朴素扫描会漏匹配。这里仅 hold 住末尾**确实是潜在标签前缀**的少量字符
      （由 ``_potential_prefix_len`` 决定），其余整段透传。这意味着 99% 的
      delta（不以 ``<`` 结尾的）零字符 hold、零 reshape——保持现有 LLM
      pipeline 的逐 token byte-for-byte 行为。
    - ``flush()`` 在 LLM 流结束时调用，把残留的非 thinking buffer 一次性吐出。

    幂等性：feed 不含 ``<think>`` 的字符串时，输出 == 输入（且不改变 chunk
    边界）。这意味着对 DeepSeek / OpenAI / Qwen 等不发 ``<think>`` 的模型零
    影响；唯一开销是每个 delta 的常数级 endswith 扫描。
    """

    def __init__(self) -> None:
        self._buf: str = ""
        self._in_thinking: bool = False

    def feed(self, text: str) -> str:
        """喂入新增 delta，返回应该向下游 yield 的纯净文本。

        返回 ``""`` 表示本次 delta 没有可输出文本（全在 thinking 内 / 全是
        部分标签前缀被 hold 在 buffer 里）。
        """
        if not text:
            return ""
        # 把上次未决的 buffer 与新 delta 拼接；buffer 在两种情况非空：
        # (a) 在 in_thinking=False 状态下，buffer 是上次 feed 末尾可能是
        #     部分 ``<think>`` 前缀的尾巴；
        # (b) 在 in_thinking=True 状态下，buffer 是部分 ``</think>`` 前缀。
        data = self._buf + text
        out_parts: list[str] = []
        i = 0
        while i < len(data):
            if self._in_thinking:
                close_at = data.find(_THINK_CLOSE, i)
                if close_at >= 0:
                    # 跳过 thinking 段 + close 标签
                    i = close_at + len(_THINK_CLOSE)
                    self._in_thinking = False
                    continue
                # 没找到 close：检查末尾是否是部分 ``</think>`` 前缀
                tail = data[i:]
                hold = _potential_prefix_len(tail, _THINK_CLOSE)
                # 前面的全部 drop（thinking 内容），仅 hold 住部分前缀
                self._buf = tail[len(tail) - hold:] if hold > 0 else ""
                return "".join(out_parts)
            else:
                open_at = data.find(_THINK_OPEN, i)
                if open_at >= 0:
                    # open 之前的内容可以输出
                    if open_at > i:
                        out_parts.append(data[i:open_at])
                    i = open_at + len(_THINK_OPEN)
                    self._in_thinking = True
                    continue
                # 没找到 open：检查末尾是否是部分 ``<think>`` 前缀
                tail = data[i:]
                hold = _potential_prefix_len(tail, _THINK_OPEN)
                if hold > 0:
                    out_parts.append(tail[: len(tail) - hold])
                    self._buf = tail[len(tail) - hold:]
                else:
                    out_parts.append(tail)
                    self._buf = ""
                return "".join(out_parts)
        # i 走到末尾 → 没有未决 buffer
        self._buf = ""
        return "".join(out_parts)

    def flush(self) -> str:
        """流结束时调用：吐出残留 buffer（仅在非 thinking 状态时有效）。

        如果流在 thinking 模式中结束（即缺失 ``</think>``），残留 buffer
        被丢弃——优先保护 TTS 不被污染，宁可漏掉模型未闭合的尾段。
        非 thinking 状态下的残留 buffer 是部分 ``<think>`` 前缀（如 ``<th``），
        既不像标签也不一定是用户可见文本，但保险起见照样吐出。
        """
        if self._in_thinking:
            self._buf = ""
            return ""
        out = self._buf
        self._buf = ""
        return out


class LLMServiceError(RuntimeError):
    """LLM 调用失败（终态）。``upstream_status`` 为 HTTP 状态码（如有）。"""

    def __init__(self, message: str, upstream_status: int | None = None) -> None:
        super().__init__(message)
        self.upstream_status: int | None = upstream_status


@dataclass
class OpenAICompatConfig:
    """OpenAICompatClient 的配置。"""

    api_key: str
    base_url: str
    model: str
    request_timeout: float = 30.0
    max_retries: int = 2

    def __post_init__(self) -> None:
        if self.max_retries < 0:
            raise LLMServiceError(
                f"max_retries must be >= 0, got {self.max_retries}"
            )


class OpenAICompatClient:
    """OpenAI-compatible 流式 chat 客户端，实现 ``LLMService`` Protocol。"""

    def __init__(self, config: OpenAICompatConfig) -> None:
        self._config = config
        # SDK 自带 retry 关掉，我们自己控制重试策略（区分 4xx vs 网络错误）
        self._client = AsyncOpenAI(
            api_key=config.api_key,
            base_url=config.base_url,
            timeout=config.request_timeout,
            max_retries=0,
        )
        log.info(
            "OpenAICompatClient ready: base_url=%s model=%s",
            config.base_url, config.model,
        )

    @classmethod
    def from_app_config(cls, cfg: Config) -> "OpenAICompatClient":
        """从全局 ``Config`` 构造；缺 ``OPENAI_API_KEY`` 时报错。"""
        missing = cfg.validate_for_phase("llm")
        if missing:
            raise LLMServiceError(
                f"missing required env vars: {', '.join(missing)}"
            )
        assert cfg.openai_api_key is not None  # validate_for_phase 已保证
        return cls(
            OpenAICompatConfig(
                api_key=cfg.openai_api_key,
                base_url=cfg.openai_base_url,
                model=cfg.openai_model,
            )
        )

    async def stream_chat(
        self,
        messages: list[ChatMessage],
        tools: list[ToolDef] | None = None,
    ) -> AsyncIterator[LLMChunk]:
        """流式 chat completion；按需 yield TextDelta / ToolCallDelta / FinishChunk。

        本身是 async generator：caller 的 ``async for`` 作用域结束（含 ``break`` /
        异常 / ``aclose()``）会触发本函数的 ``finally``，进而 ``await stream.close()``
        显式关闭 SSE，让远端停止生成。这是 Phase 5 barge-in 的硬性前置。

        thinking-chain 剥离：见模块 docstring 与 ``_ThinkingStripper``。每次
        ``stream_chat`` 调用使用独立的 stripper 实例，状态不跨 turn 泄漏。
        """
        oai_messages = [_chat_message_to_openai(m) for m in messages]
        oai_tools = (
            [_tool_def_to_openai(t) for t in tools] if tools else None
        )
        stream = await self._create_stream_with_retry(oai_messages, oai_tools)
        stripper = _ThinkingStripper()
        try:
            async for chunk in stream:
                if not chunk.choices:
                    continue
                choice = chunk.choices[0]
                delta = choice.delta

                if delta is not None:
                    content = getattr(delta, "content", None)
                    if content:
                        clean = stripper.feed(content)
                        if clean:
                            log.debug("text delta: %r", clean)
                            yield TextDelta(text=clean)

                    tool_calls = getattr(delta, "tool_calls", None)
                    if tool_calls:
                        for tc in tool_calls:
                            fn = getattr(tc, "function", None)
                            name = getattr(fn, "name", None) if fn is not None else None
                            args = getattr(fn, "arguments", None) if fn is not None else None
                            yield ToolCallDelta(
                                tool_call_index=tc.index,
                                tool_call_id=tc.id,
                                name=name,
                                arguments_delta=args or "",
                            )

                if choice.finish_reason is not None:
                    # 流结束：把 stripper 残留 buffer 吐给下游。注意必须
                    # 在 FinishChunk 之前发，否则 pipeline 会把 tail 文本
                    # 当成下一轮的开头。
                    tail = stripper.flush()
                    if tail:
                        log.debug("text delta (flush): %r", tail)
                        yield TextDelta(text=tail)
                    reason = _normalize_finish_reason(choice.finish_reason)
                    usage = _extract_usage(chunk)
                    yield FinishChunk(reason=reason, usage=usage)
        finally:
            try:
                await stream.close()
            except Exception:
                log.debug("error closing LLM stream", exc_info=True)

    async def health_check(self) -> bool:
        """非流式 ping；Phase 6 监控用。

        - 短暂故障（连接/超时/限流/上游 5xx）→ 返回 ``False``，表示 transient downtime。
        - ``AuthenticationError`` 等永久误配 → 直接抛，让监控/告警区分对待。
        """
        try:
            kwargs: dict[str, Any] = {
                "model": self._config.model,
                "messages": [{"role": "user", "content": "ping"}],
                "max_tokens": 1,
                "stream": False,
            }
            if _server_disable_thinking(self._config.model):
                kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
            await self._client.chat.completions.create(**kwargs)
            return True
        except openai.AuthenticationError:
            # 永久误配（API key 错）→ 上抛，让监控区分 misconfig vs downtime
            raise
        except (
            openai.APIConnectionError,
            openai.APITimeoutError,
            openai.RateLimitError,
            openai.APIStatusError,
        ) as exc:
            log.warning("health_check transient failure: %s", exc)
            return False

    async def _create_stream_with_retry(
        self,
        oai_messages: list[dict[str, Any]],
        oai_tools: list[dict[str, Any]] | None,
    ) -> AsyncStream[ChatCompletionChunk]:
        """建立 streaming chat completion；只对网络/限流错误重试。"""
        attempts = self._config.max_retries + 1
        last_exc: Exception | None = None
        for attempt in range(attempts):
            try:
                kwargs: dict[str, Any] = {
                    "model": self._config.model,
                    "messages": oai_messages,
                    "stream": True,
                }
                if oai_tools is not None:
                    kwargs["tools"] = oai_tools
                if _server_disable_thinking(self._config.model):
                    kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
                stream = await self._client.chat.completions.create(**kwargs)
                return cast(AsyncStream[ChatCompletionChunk], stream)
            except openai.AuthenticationError as exc:
                log.error("LLM auth failed (status=%s): %s", exc.status_code, exc)
                raise LLMServiceError(
                    f"authentication failed (check OPENAI_API_KEY): {exc}",
                    upstream_status=exc.status_code,
                ) from exc
            except openai.RateLimitError as exc:
                last_exc = exc
                if attempt >= attempts - 1:
                    break
                delay = _retry_after_seconds(exc) or 1.0
                log.warning(
                    "LLM rate-limited; retry %d/%d in %.2fs",
                    attempt + 1, attempts - 1, delay,
                )
                await asyncio.sleep(delay)
            except (openai.APIConnectionError, openai.APITimeoutError) as exc:
                last_exc = exc
                if attempt >= attempts - 1:
                    break
                delay = 0.5 * (2 ** attempt)
                log.warning(
                    "LLM transient network error; retry %d/%d in %.2fs: %s",
                    attempt + 1, attempts - 1, delay, exc,
                )
                await asyncio.sleep(delay)
            except openai.APIStatusError as exc:
                # 其他非 401/429 的 4xx/5xx：不重试
                log.error(
                    "LLM upstream error status=%s: %s", exc.status_code, exc,
                )
                raise LLMServiceError(
                    f"upstream error: {exc}", upstream_status=exc.status_code,
                ) from exc

        assert last_exc is not None
        log.error("LLM call failed after %d attempts: %s", attempts, last_exc)
        status = getattr(last_exc, "status_code", None)
        raise LLMServiceError(
            f"LLM call failed after {attempts} attempts: {last_exc}",
            upstream_status=status,
        ) from last_exc


def _chat_message_to_openai(m: ChatMessage) -> dict[str, Any]:
    """``ChatMessage`` → OpenAI ``messages[]`` 字典。

    D-13 strict tool round-trip：assistant 消息若携带 ``tool_calls``，必须发
    ``content: null``（不是 ``""``）+ ``tool_calls`` 数组。OpenAI/DeepSeek 服务端
    对此不宽容——空字符串会被认为是空文本回复，丢失 tool 调用上下文。
    """
    out: dict[str, Any] = {"role": m.role, "content": m.content or None}
    if m.role == "tool":
        # OpenAI 要求 tool 角色携带 tool_call_id
        if m.tool_call_id is None:
            raise LLMServiceError("tool message requires tool_call_id")
        out["tool_call_id"] = m.tool_call_id
    if m.name is not None:
        out["name"] = m.name
    if m.tool_calls:
        out["tool_calls"] = [
            {
                "id": tc.id,
                "type": "function",
                "function": {"name": tc.name, "arguments": tc.arguments},
            }
            for tc in m.tool_calls
        ]
    return out


def _tool_def_to_openai(t: ToolDef) -> dict[str, Any]:
    """``ToolDef`` → OpenAI ``tools[]`` 字典。"""
    return {
        "type": "function",
        "function": {
            "name": t.name,
            "description": t.description,
            "parameters": t.parameters,
        },
    }


def _normalize_finish_reason(
    raw: str,
) -> Literal["stop", "tool_calls", "length", "content_filter"]:
    """把 SDK 的 finish_reason 字符串归一化到 ``FinishChunk.reason`` 的 Literal。"""
    if raw in _KNOWN_FINISH_REASONS:
        return cast(
            Literal["stop", "tool_calls", "length", "content_filter"], raw,
        )
    # 未知值（如 "function_call" 老格式）→ 当 stop 兜底
    log.warning("unknown finish_reason %r, mapping to 'stop'", raw)
    return "stop"


def _extract_usage(chunk: ChatCompletionChunk) -> dict[str, int] | None:
    """从最终 chunk 提取 usage（DeepSeek streaming 可能不带）。"""
    usage = getattr(chunk, "usage", None)
    if usage is None:
        return None
    out: dict[str, int] = {}
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        val = getattr(usage, key, None)
        if isinstance(val, int):
            out[key] = val
    return out or None


def _retry_after_seconds(exc: openai.RateLimitError) -> float | None:
    """从 429 响应解析 ``Retry-After``；非法/缺失返回 None。"""
    resp = getattr(exc, "response", None)
    if resp is None:
        return None
    headers = getattr(resp, "headers", None) or {}
    raw = headers.get("Retry-After") or headers.get("retry-after")
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None
