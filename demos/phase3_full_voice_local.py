"""Phase 3 demo：本地完整语音 agent — Mac mic → STT → LLM → TTS → 扬声器。

Acceptance target (verify via live demo)（双语，e2e < 2.5s）：
    Say 你好 → AI replies in zh; say Hello there → AI replies in en;
    say 再见 → AI replies in zh; e2e < 2.5s.

用法（在 Mac 上）::

    # 配 .env：OPENAI_API_KEY、GPU_HOST（指向跑 sensevoice + cosyvoice 的节点）
    python -m demos.phase3_full_voice_local
    python -m demos.phase3_full_voice_local --language zh
    python -m demos.phase3_full_voice_local --no-mic   # 仅文本回退（不需要 GPU）

按 Ctrl-C 干净退出（mic / STT / LLM / TTS / 扬声器都会关掉）。
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time

from vocalize.config import get_config
from vocalize.llm.base import ChatMessage, FinishChunk, TextDelta
from vocalize.llm.openai_compat import LLMServiceError, OpenAICompatClient
from vocalize.pipeline import VoicePipeline
from vocalize.stt.sensevoice import SenseVoiceClient
from vocalize.transports.microphone import MicrophoneTransport
from vocalize.tts.cosyvoice import CosyVoiceClient, CosyVoiceError

DEFAULT_SYSTEM_PROMPT = (
    "You are a bilingual (Chinese/English) restaurant-reservation assistant. "
    "Reply in the same language as the user. Keep replies short and ask one "
    "question at a time to collect: party size, date, time, name, phone."
)


async def run_with_mic(args: argparse.Namespace) -> int:
    cfg = get_config()
    missing_gpu = cfg.validate_for_phase("gpu")
    if missing_gpu:
        print(
            f"missing env vars: {', '.join(missing_gpu)} (set GPU_HOST in .env)",
            file=sys.stderr,
        )
        return 2
    missing_llm = cfg.validate_for_phase("llm")
    if missing_llm:
        print(
            f"missing env vars: {', '.join(missing_llm)} (set OPENAI_API_KEY)",
            file=sys.stderr,
        )
        return 2

    transport = MicrophoneTransport(
        device=args.device, output_device=args.output_device,
    )
    stt = SenseVoiceClient(
        host=cfg.gpu_host,
        port=cfg.sensevoice_ws_port,
        language_hint=args.language,
    )
    llm = OpenAICompatClient.from_app_config(cfg)
    tts = CosyVoiceClient.from_app_config(cfg)

    print(
        f"[stt] {stt.ws_url} (lang_hint={args.language})\n"
        f"[llm] {cfg.openai_base_url} model={cfg.openai_model}\n"
        f"[tts] {tts.ws_url} default_lang={tts.default_language}\n"
        f"[audio] mic={transport.sample_rate}Hz mono → speaker="
        f"{transport.output_sample_rate}Hz mono\n"
        f"speak (Ctrl-C to stop) ...",
        file=sys.stderr,
    )

    pipeline = VoicePipeline(
        transport=transport,
        stt=stt,
        llm=llm,
        tts=tts,
        system_prompt=args.system_prompt,
        default_language=cfg.default_language,
    )
    try:
        await pipeline.run()
    except (CosyVoiceError, LLMServiceError) as exc:
        print(f"\n[fatal] {exc}", file=sys.stderr)
        return 1
    return 0


async def run_no_mic(args: argparse.Namespace) -> int:
    """文本回退路径：不需要 GPU 服务，单测 LLM 链路。"""
    cfg = get_config()
    missing = cfg.validate_for_phase("llm")
    if missing:
        print(
            f"missing env vars: {', '.join(missing)}", file=sys.stderr,
        )
        return 2
    llm = OpenAICompatClient.from_app_config(cfg)
    print(
        f"[llm] {cfg.openai_base_url} model={cfg.openai_model}\n"
        f"type a message and press Enter (Ctrl-D to exit):",
        file=sys.stderr,
    )

    messages: list[ChatMessage] = [
        ChatMessage(role="system", content=args.system_prompt),
    ]
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[str | None] = asyncio.Queue()

    def _reader() -> None:
        for line in sys.stdin:
            line = line.rstrip("\n")
            if line:
                asyncio.run_coroutine_threadsafe(queue.put(line), loop)
        asyncio.run_coroutine_threadsafe(queue.put(None), loop)

    loop.run_in_executor(None, _reader)

    while True:
        line = await queue.get()
        if line is None:
            return 0
        messages.append(ChatMessage(role="user", content=line))
        t0 = time.monotonic()
        first_token: float | None = None
        pieces: list[str] = []
        # ``stream_chat`` is an async generator; failures surface from the
        # ``async for`` below, not at construction time.
        stream = llm.stream_chat(messages)
        print("assistant: ", end="", flush=True)
        try:
            async for chunk in stream:
                if isinstance(chunk, TextDelta):
                    if first_token is None:
                        first_token = time.monotonic()
                    print(chunk.text, end="", flush=True)
                    pieces.append(chunk.text)
                elif isinstance(chunk, FinishChunk):
                    print()
                    if first_token is not None:
                        ttft = first_token - t0
                        total = time.monotonic() - t0
                        print(
                            f"[timing] ttft_llm={ttft:.3f}s total={total:.3f}s",
                            file=sys.stderr,
                        )
        except LLMServiceError as exc:
            print(f"\n[llm error mid-stream] {exc}", file=sys.stderr)
            continue
        reply = "".join(pieces).strip()
        if reply:
            messages.append(ChatMessage(role="assistant", content=reply))


def main() -> None:
    p = argparse.ArgumentParser(description="Phase 3 full local voice agent")
    p.add_argument(
        "--device", default=None,
        help="sounddevice input device name or index (default: system default)",
    )
    p.add_argument(
        "--output-device", default=None,
        help="sounddevice output device name or index (default: system default)",
    )
    p.add_argument(
        "--language", default="auto",
        choices=["auto", "zh", "en", "yue", "ja", "ko"],
        help="STT language hint (default: auto — let SenseVoice detect)",
    )
    p.add_argument(
        "--system-prompt", default=DEFAULT_SYSTEM_PROMPT,
        help="LLM system prompt (default: bilingual restaurant assistant)",
    )
    p.add_argument(
        "--no-mic", action="store_true",
        help="text input fallback (smoke-test LLM without GPU services)",
    )
    p.add_argument(
        "-v", "--verbose", action="store_true", help="verbose logging",
    )
    args = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    runner = run_no_mic if args.no_mic else run_with_mic
    try:
        rc = asyncio.run(runner(args))
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        rc = 130
    sys.exit(rc)


if __name__ == "__main__":
    main()
