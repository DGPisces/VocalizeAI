"""Phase 1 demo：Mac 麦克风 → SenseVoice (Windows GPU 节点) → 终端。

用法（在 Mac 上）::

    # 配 GPU_HOST、SENSEVOICE_WS_PORT 到 .env 或 export
    python -m demos.phase1_stt_mic
    python -m demos.phase1_stt_mic --device "MacBook Pro Microphone" -v
    python -m demos.phase1_stt_mic --language zh   # 强制中文 hint，关掉 auto

按 Ctrl-C 干净退出（取消 task → 客户端发 stop → 关 socket）。
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from vocalize.config import get_config
from vocalize.stt.sensevoice import SenseVoiceClient, SenseVoiceError
from vocalize.transports.microphone import MicrophoneTransport


def _format_transcript(text: str, lang: str | None, is_final: bool) -> str:
    tag = "final" if is_final else "partial"
    lang_str = lang or "??"
    return f"[{lang_str}, {tag}] {text}"


async def run(args: argparse.Namespace) -> int:
    cfg = get_config()
    missing = cfg.validate_for_phase("gpu")
    if missing:
        print(
            f"missing env vars: {', '.join(missing)} (set GPU_HOST in .env)",
            file=sys.stderr,
        )
        return 2

    mic = MicrophoneTransport(device=args.device)
    client = SenseVoiceClient(
        host=cfg.gpu_host,
        port=cfg.sensevoice_ws_port,
        language_hint=args.language,
    )
    print(f"connecting to {client.ws_url} ... (Ctrl-C to stop)", file=sys.stderr)

    audio = mic.input_stream()
    try:
        async for t in client.stream_transcribe(audio):
            if not t.text and not t.is_final:
                continue
            print(_format_transcript(t.text, t.language, t.is_final), flush=True)
    except SenseVoiceError as exc:
        print(f"sensevoice error: {exc}", file=sys.stderr)
        return 1
    finally:
        await mic.close()
    return 0


def main() -> None:
    p = argparse.ArgumentParser(description="Phase 1 STT mic demo")
    p.add_argument(
        "--device", default=None,
        help="sounddevice input device name or index (default: system default)",
    )
    p.add_argument(
        "--language", default="auto",
        choices=["auto", "zh", "en", "yue", "ja", "ko"],
        help="language hint sent to SenseVoice (default: auto)",
    )
    p.add_argument(
        "-v", "--verbose", action="store_true", help="verbose logging",
    )
    args = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    try:
        rc = asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        rc = 130
    sys.exit(rc)


if __name__ == "__main__":
    main()
