# Public sync allowlist (VocalizeAI)

Paths are anchored at repo root. Used by `scripts/build-public-filelist.py`.

- Lines under `## Files` are exact path matches.
- Lines under `## Directories` end with `/` and match any tracked file under that prefix.
- Anything that matches an entry in `.public-sync-deny` (or the skill's default deny list:
  `.planning/`, `AGENTS.md`, `CLAUDE.md`, `docs/design/`, `docs/internal/`, `rfc/`,
  `adr/`, `notes/`) is removed AFTER the allowlist pass.

## Files

- README.md
- README.zh-CN.md
- LICENSE
- SECURITY.md
- CONTRIBUTING.md
- pyproject.toml
- .env.example
- .gitignore
- uv.lock

## Directories

- src/
- frontend/
- demos/
- infra/
- tests/
- docs/release/
- docs/
- install/
- .github/
- scripts/
