# Branch Protection and Review Gates

Public `main` must be protected after the `v0.1.0` reset.

## Required PR Checks

- `Backend lint, type, and unit tests`
- `Provider API contract tests`
- `macOS speech provider build`
- `Frontend lint, build, and unit tests`
- `Packaging and installer smoke`
- `Public tree audit`

## Required Review

- Require at least one approving review.
- Require review from Code Owners.
- `CODEOWNERS` routes every path to `@DGPisces`; every PR must be reviewed by
  DGPisces before merge.
- Do not allow direct pushes to `main`, except for the one-time orphan public
  reset operation in Phase 24.

## Release Secrets

The `Release` workflow fails closed unless these repository secrets exist:

- `APPLE_DEVELOPER_ID_CERTIFICATE_BASE64`
- `APPLE_DEVELOPER_ID_CERTIFICATE_PASSWORD`
- `APPLE_DEVELOPER_ID_APPLICATION`
- `APPLE_ID`
- `APPLE_TEAM_ID`
- `APPLE_APP_SPECIFIC_PASSWORD`
- `MACOS_KEYCHAIN_PASSWORD` optional; generated per run if absent

## Maintainer Setup Checklist

1. Enable branch protection or a ruleset for `main`.
2. Require the checks listed above.
3. Require Code Owner review.
4. Disable direct pushes to `main`.
5. Add the Apple release secrets before creating the public `v0.1.0` release.
