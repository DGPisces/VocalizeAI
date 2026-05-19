# VocalizeAI Release Flow

Manual release recipe for VocalizeAI. No automated tooling (Release Please, semantic-release)
is adopted at v1 scale — the solo-maintainer overhead exceeds the benefit.

v1.0.0 was the first exercise of this flow (Phase 8), establishing the baseline.
Each subsequent release follows the same steps.

---

## Prerequisites

- `gh` CLI authenticated (`gh auth status`)
- Working directory clean on `main` (`git status`)
- All CI checks green on `main`

---

## Step 1 — Create a release branch

```bash
git checkout main && git pull
git checkout -b release/vX.Y.Z
```

---

## Step 2 — Bump version in pyproject.toml

```bash
# Edit pyproject.toml: update [project] version = "X.Y.Z"
$EDITOR pyproject.toml
```

Commit:

```bash
git add pyproject.toml
git commit -m "chore(release): bump version to X.Y.Z"
```

---

## Step 3 — Write CHANGELOG entry

Edit `CHANGELOG.md` at repo root:

1. Move items from `## [Unreleased]` into a new `## [X.Y.Z] — YYYY-MM-DD` section.
2. Leave `## [Unreleased]` empty with the stub line.
3. Add the comparison link at the bottom:
   `[X.Y.Z]: https://github.com/DGPisces/VocalizeAI/compare/vA.B.C...vX.Y.Z`

```bash
git add CHANGELOG.md
git commit -m "docs(release): CHANGELOG entry for vX.Y.Z"
```

---

## Step 4 — Open and merge the release PR

```bash
git push -u origin release/vX.Y.Z
gh pr create \
  --base main \
  --title "Release vX.Y.Z" \
  --body "Bump version to X.Y.Z. See CHANGELOG.md for details."
```

Wait for CI to pass, self-approve (solo maintainer), then merge to `main`.

```bash
# After merge:
git checkout main && git pull
```

---

## Step 5 — Tag the release

```bash
git tag -a vX.Y.Z -m "Release vX.Y.Z"
git push origin vX.Y.Z
```

---

## Step 6 — Create GitHub Release

Prepare a release notes file (can reuse the CHANGELOG section):

```bash
# Extract the relevant CHANGELOG section into a temp file, or write inline:
gh release create vX.Y.Z \
  --title "vX.Y.Z" \
  --notes-file path/to/release-notes.md \
  --verify-tag
```

Alternatively, draft the release in the GitHub UI after pushing the tag.

---

## Step 7 — Sync to public repo

Run the sync-private-to-public flow (documented in `.planning/skills/`) to push
sanitized source to `github.com/DGPisces/VocalizeAI`. The tag and release travel
with the sync.

---

## Step 8 — Post-release verification

- Confirm the GitHub Release page shows the correct tag and notes.
- Confirm the Discussions > Announcements tab auto-created a release announcement (optional).
- Run layer4 smoke checklist against the production Pi deployment:
  `docs/release/layer4-smoke-checklist.md`.

---

## History

| Version | Date       | Internal SHA | Public SHA |
|---------|------------|--------------|------------|
| 1.0.0   | 2026-05-18 | d9bd923      | 591aa9e    |
