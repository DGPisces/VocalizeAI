# Release Evidence Template

Use this template in the release notes or attach it as the approved release artifact. It makes D-13 operational: release notes link to the filled Layer-4 checklist or artifact, and the filled checklist links back to the release tag.

## Release Record

| Field | Value |
| --- | --- |
| Release tag | |
| Commit SHA | |
| CI workflow run URL | |
| AI-merchant artifact URL | |
| Filled Layer-4 checklist path or URL | |
| Release notes URL | |
| Release operator | |
| Release reviewer | |
| Date / time | |

## Release-Audio Command

```bash
. .venv/bin/activate && pytest tests/integration/test_ai_merchant.py --release-audio --ai-provider-required
```

Confirmation:

- TEST-04 release-audio coverage is represented by both real-audio scenarios and all three merchant behavior seeds.
- `--release-audio` selected only `gate: release_audio` cases.
- `run_release_audio_case(...)` executed for each case.
- The text-bypass runner did not execute for this release-audio evidence.
- D-05 release-only boundary was preserved; these cases were not ordinary PR blockers.
- D-08 real-audio bridge coverage was exercised for one zh merchant scenario and one en merchant scenario.

## Scenario Results

| Scenario | Seed | Result | Case evidence directory or artifact URL |
| --- | --- | --- | --- |
| `release_audio_zh_bridge` | `direct` | Pass / Fail | |
| `release_audio_zh_bridge` | `impatient` | Pass / Fail | |
| `release_audio_zh_bridge` | `follow_up` | Pass / Fail | |
| `release_audio_en_bridge` | `direct` | Pass / Fail | |
| `release_audio_en_bridge` | `impatient` | Pass / Fail | |
| `release_audio_en_bridge` | `follow_up` | Pass / Fail | |

## Per-Case Evidence

### `release_audio_zh_bridge` / `direct`

| Evidence | Link or path | Pass / Fail | Notes |
| --- | --- | --- | --- |
| `metadata.json` | | | Release tag, commit SHA, backend URL, browser/device fields |
| `frame_log.json` | | | WebSocket frames, transcript/review events, and errors |
| `stt_transcript.json` | | | Backend STT final merchant transcript |
| `tts_events.json` | | | Backend TTS `ai_to_merchant` output |
| `browser_speaker.json` | | | BrowserAudioBridge speaker playback observation |
| `raw_capture_summary.json` | | | Current-run STT/TTS/browser-speaker evidence counts |
| `judge.json` | | | DeepSeek-V4-Pro verdict and per-check rationales |

### `release_audio_zh_bridge` / `impatient`

| Evidence | Link or path | Pass / Fail | Notes |
| --- | --- | --- | --- |
| `metadata.json` | | | Release tag, commit SHA, backend URL, browser/device fields |
| `frame_log.json` | | | WebSocket frames, transcript/review events, and errors |
| `stt_transcript.json` | | | Backend STT final merchant transcript |
| `tts_events.json` | | | Backend TTS `ai_to_merchant` output |
| `browser_speaker.json` | | | BrowserAudioBridge speaker playback observation |
| `raw_capture_summary.json` | | | Current-run STT/TTS/browser-speaker evidence counts |
| `judge.json` | | | DeepSeek-V4-Pro verdict and per-check rationales |

### `release_audio_zh_bridge` / `follow_up`

| Evidence | Link or path | Pass / Fail | Notes |
| --- | --- | --- | --- |
| `metadata.json` | | | Release tag, commit SHA, backend URL, browser/device fields |
| `frame_log.json` | | | WebSocket frames, transcript/review events, and errors |
| `stt_transcript.json` | | | Backend STT final merchant transcript |
| `tts_events.json` | | | Backend TTS `ai_to_merchant` output |
| `browser_speaker.json` | | | BrowserAudioBridge speaker playback observation |
| `raw_capture_summary.json` | | | Current-run STT/TTS/browser-speaker evidence counts |
| `judge.json` | | | DeepSeek-V4-Pro verdict and per-check rationales |

### `release_audio_en_bridge` / `direct`

| Evidence | Link or path | Pass / Fail | Notes |
| --- | --- | --- | --- |
| `metadata.json` | | | Release tag, commit SHA, backend URL, browser/device fields |
| `frame_log.json` | | | WebSocket frames, transcript/review events, and errors |
| `stt_transcript.json` | | | Backend STT final merchant transcript |
| `tts_events.json` | | | Backend TTS `ai_to_merchant` output |
| `browser_speaker.json` | | | BrowserAudioBridge speaker playback observation |
| `raw_capture_summary.json` | | | Current-run STT/TTS/browser-speaker evidence counts |
| `judge.json` | | | DeepSeek-V4-Pro verdict and per-check rationales |

### `release_audio_en_bridge` / `impatient`

| Evidence | Link or path | Pass / Fail | Notes |
| --- | --- | --- | --- |
| `metadata.json` | | | Release tag, commit SHA, backend URL, browser/device fields |
| `frame_log.json` | | | WebSocket frames, transcript/review events, and errors |
| `stt_transcript.json` | | | Backend STT final merchant transcript |
| `tts_events.json` | | | Backend TTS `ai_to_merchant` output |
| `browser_speaker.json` | | | BrowserAudioBridge speaker playback observation |
| `raw_capture_summary.json` | | | Current-run STT/TTS/browser-speaker evidence counts |
| `judge.json` | | | DeepSeek-V4-Pro verdict and per-check rationales |

### `release_audio_en_bridge` / `follow_up`

| Evidence | Link or path | Pass / Fail | Notes |
| --- | --- | --- | --- |
| `metadata.json` | | | Release tag, commit SHA, backend URL, browser/device fields |
| `frame_log.json` | | | WebSocket frames, transcript/review events, and errors |
| `stt_transcript.json` | | | Backend STT final merchant transcript |
| `tts_events.json` | | | Backend TTS `ai_to_merchant` output |
| `browser_speaker.json` | | | BrowserAudioBridge speaker playback observation |
| `raw_capture_summary.json` | | | Current-run STT/TTS/browser-speaker evidence counts |
| `judge.json` | | | DeepSeek-V4-Pro verdict and per-check rationales |

## Known Gaps

| Gap | Impact | Owner | Follow-up |
| --- | --- | --- | --- |
| | | | |

## Release Notes Linkage

- Release notes URL:
- Filled checklist or artifact URL:
- Checklist backlink to release tag:
- CI workflow run URL:
- AI-merchant artifact URL:

Only link checklist or artifact locations approved for release evidence. Do not use private working notes as release evidence.
