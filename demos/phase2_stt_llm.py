"""Phase 2 demo：Mac 麦克风 → SenseVoice → DeepSeek 流式 → 终端 token-by-token。

用法（在 Mac 上）::

    # 配 .env：OPENAI_API_KEY、（可选）OPENAI_BASE_URL / OPENAI_MODEL、GPU_HOST
    python -m demos.phase2_stt_llm
    python -m demos.phase2_stt_llm --language zh
    python -m demos.phase2_stt_llm --no-mic   # 改用 stdin，无需 GPU 服务

切换 provider 仅改 ``.env`` 即可：

- DeepSeek（默认）：``OPENAI_BASE_URL=https://api.deepseek.com/v1``
  ``OPENAI_MODEL=deepseek-chat``
- 官方 OpenAI：       ``OPENAI_BASE_URL=https://api.openai.com/v1``
  ``OPENAI_MODEL=gpt-4o-mini``
- Qwen DashScope：     ``OPENAI_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1``
  ``OPENAI_MODEL=qwen-plus``

按 Ctrl-C 干净退出（mic / STT / LLM stream 均会关闭）。
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time

from vocalize.config import get_config
from vocalize.llm.base import ChatMessage, FinishChunk, TextDelta, ToolCallDelta
from vocalize.llm.openai_compat import LLMServiceError, OpenAICompatClient
from vocalize.stt.sensevoice import SenseVoiceClient, SenseVoiceError
from vocalize.transports.microphone import MicrophoneTransport

DEFAULT_SYSTEM_PROMPT = (
    "You are a bilingual (Chinese/English) restaurant-reservation assistant. "
    "Reply in the same language as the user. Keep replies short and ask one "
    "question at a time to collect: party size, date, time, name, phone."
)


async def _chat_turn(
    llm: OpenAICompatClient, messages: list[ChatMessage]
) -> str:
    """跑一轮 LLM 流；token-by-token 打印；返回完整 assistant 文本。"""
    t0 = time.monotonic()
    first_token_at: float | None = None
    pieces: list[str] = []

    # stream_chat 是 async generator，调用即拿迭代器，不需要 await
    stream = llm.stream_chat(messages)

    print("assistant: ", end="", flush=True)
    try:
        async for chunk in stream:
            if isinstance(chunk, TextDelta):
                if first_token_at is None:
                    first_token_at = time.monotonic()
                print(chunk.text, end="", flush=True)
                pieces.append(chunk.text)
            elif isinstance(chunk, ToolCallDelta):
                # Phase 2 demo 不接 tool；只是日志一下
                logging.debug(
                    "tool_call idx=%d id=%s name=%s args+=%r",
                    chunk.tool_call_index, chunk.tool_call_id,
                    chunk.name, chunk.arguments_delta,
                )
            elif isinstance(chunk, FinishChunk):
                print()  # newline after final
                if first_token_at is not None:
                    ttft = first_token_at - t0
                    total = time.monotonic() - t0
                    logging.info(
                        "[timing] ttft=%.3fs total=%.3fs reason=%s usage=%s",
                        ttft, total, chunk.reason, chunk.usage,
                    )
    except LLMServiceError as exc:
        print(f"\n[llm error mid-stream] {exc}", file=sys.stderr)

    return "".join(pieces)


async def _stdin_lines() -> "asyncio.Queue[str | None]":
    """把 stdin 行喂进一个 queue（None 代表 EOF）。"""
    queue: asyncio.Queue[str | None] = asyncio.Queue()

    def _reader() -> None:
        for line in sys.stdin:
            line = line.rstrip("\n")
            if line:
                asyncio.run_coroutine_threadsafe(queue.put(line), loop)
        asyncio.run_coroutine_threadsafe(queue.put(None), loop)

    loop = asyncio.get_running_loop()
    loop.run_in_executor(None, _reader)
    return queue


async def run_no_mic(args: argparse.Namespace) -> int:
    cfg = get_config()
    llm = OpenAICompatClient.from_app_config(cfg)
    print(
        f"[provider] base_url={cfg.openai_base_url} model={cfg.openai_model}",
        file=sys.stderr,
    )

    messages: list[ChatMessage] = [
        ChatMessage(role="system", content=args.system_prompt),
    ]
    queue = await _stdin_lines()
    print("type a message and press Enter (Ctrl-D to exit):", file=sys.stderr)
    while True:
        line = await queue.get()
        if line is None:
            return 0
        messages.append(ChatMessage(role="user", content=line))
        reply = await _chat_turn(llm, messages)
        if reply:
            messages.append(ChatMessage(role="assistant", content=reply))


async def run_with_mic(args: argparse.Namespace) -> int:
    cfg = get_config()
    missing = cfg.validate_for_phase("gpu")
    if missing:
        print(
            f"missing env vars: {', '.join(missing)} (set GPU_HOST in .env)",
            file=sys.stderr,
        )
        return 2

    llm = OpenAICompatClient.from_app_config(cfg)
    print(
        f"[provider] base_url={cfg.openai_base_url} model={cfg.openai_model}",
        file=sys.stderr,
    )

    mic = MicrophoneTransport(device=args.device)
    stt = SenseVoiceClient(
        host=cfg.gpu_host,
        port=cfg.sensevoice_ws_port,
        language_hint=args.language,
    )
    print(
        f"connecting to {stt.ws_url} ... (speak; Ctrl-C to stop)",
        file=sys.stderr,
    )

    messages: list[ChatMessage] = [
        ChatMessage(role="system", content=args.system_prompt),
    ]

    audio = mic.input_stream()
    try:
        async for t in stt.stream_transcribe(audio):
            if not t.is_final:
                if t.text:
                    print(f"\r[partial] {t.text}", end="", flush=True)
                continue
            print(f"\nuser: {t.text}", flush=True)
            messages.append(ChatMessage(role="user", content=t.text))
            reply = await _chat_turn(llm, messages)
            if reply:
                messages.append(ChatMessage(role="assistant", content=reply))
    except SenseVoiceError as exc:
        print(f"sensevoice error: {exc}", file=sys.stderr)
        return 1
    finally:
        await mic.close()
    return 0


def main() -> None:
    p = argparse.ArgumentParser(description="Phase 2 STT+LLM demo")
    p.add_argument(
        "--device", default=None,
        help="sounddevice input device name or index (default: system default)",
    )
    p.add_argument(
        "--language", default="auto",
        choices=["auto", "zh", "en", "yue", "ja", "ko"],
        help="STT language hint (default: auto)",
    )
    p.add_argument(
        "--system-prompt", default=DEFAULT_SYSTEM_PROMPT,
        help="LLM system prompt (default: bilingual restaurant assistant)",
    )
    p.add_argument(
        "--no-mic", action="store_true",
        help="read user input from stdin instead of microphone (smoke-test LLM)",
    )
    p.add_argument(
        "-v", "--verbose", action="store_true", help="verbose logging",
    )
    args = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    runner = run_no_mic if args.no_mic else run_with_mic
    try:
        rc = asyncio.run(runner(args))
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        rc = 130
    except LLMServiceError as exc:
        print(f"llm error: {exc}", file=sys.stderr)
        rc = 1
    sys.exit(rc)


if __name__ == "__main__":
    main()
