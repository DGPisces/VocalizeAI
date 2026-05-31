# VocalizeAI Release Flow

Release flow for the Mac-first public product.

## Prerequisites

- Working tree clean on the release branch.
- CI green, including backend, Provider API, macOS helper, frontend,
  packaging/installer, and public-tree audit.
- Required review from DGPisces.
- Apple Developer ID signing and notarization secrets configured in GitHub.
- Human clean-install test plan ready.

## Step 1 — Prepare the Release Branch

```bash
git checkout main
git pull
git checkout -b release/vX.Y.Z
```

Update the version in `pyproject.toml` if needed, then update `CHANGELOG.md`.

## Step 2 — Run Local Release Checks

```bash
.venv/bin/python -m pytest tests --ignore=tests/integration
cd frontend && npm run lint && npm run build && npm test
cd ..
bash -n install/install.sh install/uninstall.sh scripts/build-macos-release.sh
```

For a local unsigned artifact smoke:

```bash
scripts/build-macos-release.sh --signing-mode skip
bash install/verify-release.sh dist/release/SHA256SUMS dist/release/VocalizeAI-*-macos-*.zip
```

## Step 3 — Open the Release PR

```bash
git push -u origin release/vX.Y.Z
gh pr create \
  --base main \
  --title "Release vX.Y.Z" \
  --body "Prepare VocalizeAI vX.Y.Z. See CHANGELOG.md for details."
```

Merge only after CI is green and the maintainer review is complete.

## Step 4 — Tag the Release

```bash
git checkout main
git pull
git tag -a vX.Y.Z -m "Release vX.Y.Z"
git push origin vX.Y.Z
```

The GitHub `Release` workflow builds the signed/notarized macOS artifact,
generates `SHA256SUMS`, verifies install/setup/uninstall smoke, and publishes
the assets to the GitHub Release.

## Step 5 — Human Acceptance

Before the public reset or release announcement, a tester must install from the
GitHub Release artifact and verify:

- `./vocalize setup` with only LLM values, thinking mode, and local port
- `./vocalize doctor` with production checks enabled
- `./vocalize start`
- one Chinese and one English end-to-end smoke task
- `./vocalize uninstall` or `uninstall.sh`

## Branch Protection

See [branch-protection.md](branch-protection.md) for required check names,
review policy, and release secret names.
