# Changelog

All notable changes to VocalizeAI are documented in this file.

Format follows [Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/).
Versions follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

_No unreleased changes yet._

---

## [0.1.0] — 2026-05-31

Initial Mac-first public release.

### Added

- Local macOS release artifact with packaged backend, bundled Vite web console,
  native macOS speech helper, installer, updater, and uninstaller.
- LLM-only setup through `./vocalize setup`.
- `./vocalize doctor`, `start`, `stop`, `status`, `logs`, `update`, and
  `uninstall` commands.
- Vocalize Provider API for realtime STT/TTS health, capabilities, streaming
  events, cancellation, and structured errors.
- Chinese and English web console with task creation, readiness, live
  transcript, clarification, user supplement, manual takeover, diagnostics,
  settings, and post-call review.
- Productized CI gates for backend, Provider API, macOS helper, frontend,
  packaging/installer smoke, and public-tree audit.

[Unreleased]: https://github.com/DGPisces/VocalizeAI/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/DGPisces/VocalizeAI/releases/tag/v0.1.0
