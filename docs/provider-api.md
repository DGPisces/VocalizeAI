# Vocalize Provider API

VocalizeAI talks to speech services through a local Provider API. The default
`v0.1.0` provider is the macOS native speech helper, but the backend only
depends on this contract.

The provider is expected to run on loopback by default. Public deployments
should not expose these endpoints to the internet.

## Versioning

Current API version: `v1`.

Every response or event that includes `provider_api_version` must use `"1.0"`.
Future incompatible changes will use a new URL prefix.

## Capabilities

```http
GET /v1/capabilities
```

Response:

```json
{
  "provider_api_version": "1.0",
  "provider": "macos-native",
  "realtime": true,
  "stt": {
    "realtime": true,
    "input_encoding": "pcm_s16le",
    "input_sample_rate": 16000,
    "languages": ["zh-CN", "en-US"]
  },
  "tts": {
    "realtime": true,
    "output_encoding": "pcm_s16le",
    "output_sample_rate": 24000,
    "languages": ["zh-CN", "en-US"]
  },
  "permissions": {
    "speech_recognition": "authorized",
    "microphone": "authorized",
    "tts_voices_available": 12
  }
}
```

`vocalize setup` and `vocalize doctor` use this endpoint to derive provider
settings. Ordinary users should not need to hand-edit STT/TTS parameters.

## STT Streaming

```http
GET /v1/stt/stream
Upgrade: websocket
```

Client-to-provider frames:

```json
{"type":"start","provider_api_version":"1.0","language":"auto","session_id":"optional"}
```

Binary frames are raw audio bytes matching the provider's advertised input
format. The default realtime profile is mono `pcm_s16le` at 16 kHz.

```json
{"type":"end_of_utterance"}
{"type":"stop"}
```

Provider-to-client transcript event:

```json
{
  "type": "transcript",
  "text": "book a table",
  "is_final": false,
  "confidence": 0.82,
  "start_time": 0.0,
  "end_time": 1.1,
  "utterance_id": 0,
  "language": "en",
  "segments": [
    {"text":"book a table","language":"en","start_time":0.0,"end_time":1.1}
  ]
}
```

`partial` and `final` transcripts for the same utterance must share
`utterance_id`. `language` may be `null` on partials.

## TTS Streaming

```http
GET /v1/tts/stream
Upgrade: websocket
```

Client-to-provider frames:

```json
{"type":"start","provider_api_version":"1.0","language":"zh","session_id":"optional"}
{"type":"text","text":"您好。","language":"zh","is_final_segment":true}
{"type":"stop"}
```

Provider-to-client events:

```json
{"type":"audio_start","sample_rate":24000,"encoding":"pcm_s16le","channels":1}
```

Binary frames contain audio bytes matching the `audio_start` metadata.

```json
{"type":"audio_end"}
```

## Errors

Providers should return structured errors over the active WebSocket:

```json
{"type":"error","code":"permission_denied","message":"Microphone permission is missing","fatal":true}
```

`fatal=true` ends the current stream. Non-fatal errors may be logged and ignored
by the backend when the stream can continue safely.

## Cancellation

Clients cancel by sending `{"type":"stop"}` and closing the WebSocket. Providers
must stop recognition/synthesis promptly and release native resources.

## Production Readiness

The `realtime` profile is required for a production-ready VocalizeAI deployment.
Batch-only speech providers may be useful for diagnostics, but they do not
satisfy the public `v0.1.0` Mac-first product path.
