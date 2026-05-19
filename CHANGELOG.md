# Changelog

All notable changes to VocalizeAI are documented in this file.

Format follows [Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/).
Versions follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

_No unreleased changes yet._

---

## [1.0.0] — 2026-05-18

**First public release.** Apache 2.0 open-source milestone.

Internal source: `d9bd923` · Public repo first commit: `591aa9e`

Release notes: https://github.com/DGPisces/VocalizeAI/releases/tag/v1.0.0

### Added

- **Universal phone-task engine** — 5-layer dialogue pipeline (preflight, question,
  translate, summarize, merchant) handles arbitrary phone-call scenarios without
  per-task configuration.
- **Bilingual zh/en UI** — React frontend with full Chinese and English locale
  support from day 1.
- **Raspberry Pi orchestrator** — production-grade deploy target; install script
  + systemd unit + cloudflared tunnel for zero-port-forward remote access.
- **Audio loopback testing** — Playwright + pytest harness drives
  laptop-loopback calls and post-call callback flows; 8 text-bypass AI merchant
  scenarios with a deterministic judge.
- **Apache 2.0 license** — OSS launch with SECURITY.md, CONTRIBUTING.md,
  issue templates, PR template, and CODEOWNERS.

[Unreleased]: https://github.com/DGPisces/VocalizeAI/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/DGPisces/VocalizeAI/releases/tag/v1.0.0
