# scripts/

Ad-hoc Python utilities run by maintainers, not part of the runtime call path.

Each script is standalone and invoked from the repo root. Current scripts:

- `build-public-filelist.py` — builds the filtered file list for the
  `sync-private-to-public` skill (consumes `install/public-allowlist.md`
  and `.public-sync-deny`).
- `stability-24h-driver.py` — drives the 24-hour Pi stability rehearsal
  (Phase 4 DEPLOY-02 evidence harness).
The dead-code-scanner whitelist (`vulture-whitelist.py`) was relocated to
`.tooling/vulture-whitelist.py` in Phase 6 (maintainer-only scan tooling lives
under `.tooling/` and is excluded from the public mirror).

Contrast with `infra/` (deployable services) and `tools/` (reserved for release
tooling, currently empty pending future use).
