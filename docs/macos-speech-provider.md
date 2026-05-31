# macOS Speech Provider Helper

`macos/VocalizeSpeechProvider` is the default `v0.1.0` speech provider for the
Mac-first public release. It exposes the Vocalize Provider API on loopback and
uses macOS native speech components:

- STT: Apple Speech (`SFSpeechRecognizer`) with streamed `pcm_s16le` audio.
- TTS: macOS `say` plus AVFoundation conversion to mono `pcm_s16le` at 24 kHz.

## Build

```bash
swift build --package-path macos/VocalizeSpeechProvider
```

## Run

```bash
VOCALIZE_SPEECH_PROVIDER_PORT=8765 \
  macos/VocalizeSpeechProvider/.build/debug/VocalizeSpeechProvider
```

Then check:

```bash
curl -s http://127.0.0.1:8765/v1/capabilities
```

## Backend Auto-Start

The backend only starts the helper when explicitly configured:

```bash
VOCALIZE_SPEECH_PROVIDER_AUTO_START=1
VOCALIZE_SPEECH_PROVIDER_COMMAND=/absolute/path/to/VocalizeSpeechProvider
VOCALIZE_STT_PROVIDER_URL=http://127.0.0.1:8765
VOCALIZE_TTS_PROVIDER_URL=http://127.0.0.1:8765
```

The installer will write these values for packaged builds. Source-tree dev runs
may leave auto-start disabled and start the helper manually.

## Permissions

`/v1/capabilities` reports:

- `permissions.speech_recognition`
- `permissions.microphone`
- `permissions.tts_voices_available`

`vocalize doctor` treats missing Speech Recognition permission or missing TTS
voices as deployment blockers. On first use, macOS may prompt the user to grant
Speech Recognition access.
