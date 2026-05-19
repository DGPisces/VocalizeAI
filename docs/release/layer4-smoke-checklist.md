# Layer-4 Release Smoke Checklist

Use this checklist before every release tag to prove the physical laptop plus iPhone speakerphone path that ordinary PR CI cannot cover.

## Scope

- Requirements: TEST-04 release-audio scenario coverage and TEST-06 release smoke gate.
- Decisions: D-05 keeps real-audio validation release-only, D-08 defines the two real-audio bridge scenarios, and D-13 requires release notes to link the filled checklist or approved artifact.
- Release-only scenarios from `tests/integration/scenarios.yaml`:
  - `release_audio_zh_bridge` (`zh` user, `zh` merchant)
  - `release_audio_en_bridge` (`en` user, `en` merchant)
- Coverage: `2 scenarios x 3 seeds` = 6 release-audio cases (`direct`, `impatient`, `follow_up` for each scenario).
- These real-audio scenarios are not ordinary PR blockers and must stay outside default PR CI.

## Operator Record

| Field | Value |
| --- | --- |
| Release tag | |
| Commit SHA | |
| Operator | |
| Date / time | |
| Backend URL | |
| Browser and version | |
| Laptop microphone input label | |
| Laptop speaker output label | |
| iPhone / speakerphone device | |
| STT service health | Pass / Fail |
| TTS service health | Pass / Fail |
| LLM / DeepSeek judge health | Pass / Fail |
| CI workflow run URL | |
| Release notes URL | |

## Preconditions

- The backend points at the release candidate commit and real STT/TTS/LLM services.
- The browser uses the release-audio Playwright config with no fake-media flags.
- The laptop microphone is positioned to capture the iPhone speakerphone output.
- Browser speaker output is routed so the merchant can hear backend TTS over the phone.
- `VOCALIZE_RELEASE_AUDIO_BACKEND_URL` points to the backend under test.
- `VOCALIZE_RELEASE_AUDIO_INPUT_LABEL` identifies the browser microphone used for merchant capture.
- `VOCALIZE_RELEASE_AUDIO_PLAY_CMD` can play the scripted merchant audio into the speakerphone path.
- The release notes draft has a place to link this filled checklist or the approved release artifact.

## Command

Run from the repository root:

```bash
. .venv/bin/activate && pytest tests/integration/test_ai_merchant.py --release-audio --ai-provider-required
```

This command selects only `gate: release_audio` scenarios and invokes `run_release_audio_case(...)`, not the text-bypass runner. It must produce evidence under `tests/integration/evidence/<scenario_id>/<seed>/`.

## Smoke Checks

For each scenario and seed, mark Pass only when all checks have evidence:

- Speakerphone bridge: merchant audio reaches the laptop microphone through the physical phone speaker path.
- Language routing: `release_audio_zh_bridge` stays zh/zh and `release_audio_en_bridge` stays en/en.
- Browser microphone capture: the browser records merchant audio from the selected input.
- Backend STT capture: `stt_transcript.json` includes final merchant transcript evidence.
- Backend TTS output: `tts_events.json` includes `ai_to_merchant` output.
- BrowserAudioBridge speaker playback: `browser_speaker.json` records playback observation for the backend TTS audio.
- DeepSeek-V4-Pro judge verdict: `judge.json` exists and passes all must-pass checks.
- Raw capture summary: `raw_capture_summary.json` records the current run's STT/TTS/browser-speaker evidence counts.
- Transcript/review evidence: `frame_log.json` and `metadata.json` identify the scenario, seed, release tag, and result.

## Case Results

| Scenario | Seed | Result | Evidence directory | Notes |
| --- | --- | --- | --- | --- |
| `release_audio_zh_bridge` | `direct` | Pass / Fail | `tests/integration/evidence/release_audio_zh_bridge/direct/` | |
| `release_audio_zh_bridge` | `impatient` | Pass / Fail | `tests/integration/evidence/release_audio_zh_bridge/impatient/` | |
| `release_audio_zh_bridge` | `follow_up` | Pass / Fail | `tests/integration/evidence/release_audio_zh_bridge/follow_up/` | |
| `release_audio_en_bridge` | `direct` | Pass / Fail | `tests/integration/evidence/release_audio_en_bridge/direct/` | |
| `release_audio_en_bridge` | `impatient` | Pass / Fail | `tests/integration/evidence/release_audio_en_bridge/impatient/` | |
| `release_audio_en_bridge` | `follow_up` | Pass / Fail | `tests/integration/evidence/release_audio_en_bridge/follow_up/` | |

## Evidence File Checklist

Each case must include the exact files below:

| File | Pass / Fail | Link or path | Notes |
| --- | --- | --- | --- |
| `metadata.json` | | | Scenario id, seed, release tag, commit SHA, backend URL, browser/device fields |
| `frame_log.json` | | | WebSocket frames, transcript/review events, and errors |
| `stt_transcript.json` | | | Final backend STT merchant transcript |
| `tts_events.json` | | | Backend TTS `ai_to_merchant` output |
| `browser_speaker.json` | | | BrowserAudioBridge speaker playback observation |
| `raw_capture_summary.json` | | | Current-run STT/TTS/browser-speaker evidence counts |
| `judge.json` | | | DeepSeek-V4-Pro verdict and per-check rationales |

## Release Evidence Linkage

- The filled checklist must link the release tag, commit SHA, CI workflow run, AI-merchant artifact, and release notes.
- The release notes must link back to this filled checklist or the approved artifact that contains it.
- Link only evidence approved for release communication. Do not publish private working notes or internal planning content.

## Sign-Off

| Role | Name | Date | Decision | Notes |
| --- | --- | --- | --- | --- |
| Release operator | | | Pass / Fail | |
| Release reviewer | | | Pass / Fail | |
