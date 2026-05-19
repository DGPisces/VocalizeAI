# Contributing to VocalizeAI

## How VocalizeAI accepts contributions

VocalizeAI follows an out-of-band contribution model (D-16):

1. **File an issue** on the public repo describing the bug or feature request.
2. **Optional PR**: You may open a pull request for discussion. Public-repo PRs
   are **not merged directly** into the public repo — the maintainer reviews the
   change, applies an equivalent change to the private repo, and the next
   release-tag sync carries it out to the public mirror.
3. This keeps the private repo as the single source of truth and preserves the
   `.planning/` workflow.

If your contribution is security-related, see [SECURITY.md](SECURITY.md) first.

## Setting up the dev environment

See [README.md](README.md) — the "Development setup" and "Self-host quickstart"
sections cover all required steps. Do not duplicate the setup here.

## Running tests

```bash
# Backend (pytest)
source .venv/bin/activate
pytest

# Frontend unit tests (vitest)
cd frontend && npm test

# Frontend integration tests (Playwright)
cd frontend && npm run test:integration
```

Note: `tests/integration/` release-audio cases require a physical audio setup
(microphone + speaker) and a live Pi orchestrator. These are gated behind
`--release-audio` and do not run in PR CI.

All checks must pass on your PR before merge. CI runs lint (ruff + mypy + tsc),
backend unit tests (pytest), frontend unit tests (vitest), and the 8 text-bypass
integration scenarios. See `.github/workflows/ci.yml` for the full pipeline.

When you open a PR, GitHub auto-fills `.github/PULL_REQUEST_TEMPLATE.md`. Please
complete every section: Summary, Test plan, Linked issue, Checklist.

## Code style

**Python:**
- `ruff check` for linting (line length 88; see `[tool.ruff]` in `pyproject.toml`)
- `mypy` in strict mode (see `[tool.mypy]` in `pyproject.toml`)

**TypeScript:**
- TypeScript strict mode (`tsconfig.json`)
- `npx tsc --noEmit` must pass

## Commit conventions

Follow the existing commit format:

```
feat(<area>): <verb> <noun>
fix(<area>): <verb> <noun>
docs(<area>): <verb> <noun>
chore(<area>): <verb> <noun>
test(<area>): <verb> <noun>
refactor(<area>): <verb> <noun>
```

Examples: `feat(server): add X-Invite-Token gate`, `fix(frontend): handle 401 on session create`.

## Branches

Use `feat/<name>`, `fix/<name>`, `docs/<name>`, `chore/<name>`. Never commit
directly to `main`.

## Issue triage / vulnerability reporting

- Ordinary bugs and feature requests: file a GitHub issue.
- Security vulnerabilities: follow the process in [SECURITY.md](SECURITY.md).
  Do NOT file public GitHub issues for security topics.

## CI behavior for external PRs

VocalizeAI's CI pipeline has two tiers depending on where your PR originates:

**External fork PRs** (contributors opening a PR from their own fork):
- Run: ruff lint (`src/`), mypy type check, pytest unit tests, TypeScript type check,
  Vitest unit tests, and the Playwright loopback smoke tests.
- Skip: the `ai-merchant` job. This job requires repository secrets (LLM API keys) that
  GitHub does not expose to fork PRs for security reasons. The job is skipped with a
  neutral status — it is **not counted as a required check** for fork PRs.
- All skipped jobs show a "skipped" badge, not a failure. Your PR is considered
  CI-green when all non-skipped jobs pass.

**Internal PRs** (PRs opened from a branch within `DGPisces/VocalizeAI`):
- Run all jobs including `ai-merchant` (deterministic judge; no live LLM key required
  in CI — real-LLM coverage is gated behind `--release-audio` and runs pre-release).

**What this means for contributors:**
- You don't need to supply any API keys. Lint, type checks, and unit tests are fully
  self-contained and run on GitHub-hosted ubuntu runners.
- If the maintainer needs to validate AI-merchant behavior on your PR, they will apply
  the change to the internal repo (per the out-of-band contribution model above) and
  run the full pipeline there.
- The `good first issue` label on GitHub Issues marks well-scoped bugs and features
  suitable for first-time contributors.

## Code of Conduct

VocalizeAI does not adopt a formal Code of Conduct at this stage. Standard
professional conduct is expected: be respectful, assume good faith, focus on
the technical content. Disputes that cannot be resolved in-thread escalate to
the maintainer via email (see SECURITY.md for the contact channel; for
non-security disputes use the same address).

## License

VocalizeAI is licensed under [Apache 2.0](LICENSE). By submitting a contribution,
you agree that your contribution is licensed under Apache 2.0 per §5 of the
license ("Submission of Contributions").
