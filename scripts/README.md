# scripts/

Ad-hoc utilities run by maintainers, not part of the runtime call path.

Current public scripts:

- `build-public-filelist.py` — builds the filtered file list for the public
  export flow.
- `build-macos-release.sh` — builds the packaged macOS release artifact,
  optional signing/notarization path, zip, and `SHA256SUMS`.
- `smoke.sh` — local backend smoke check for source development.

Contrast with `install/` (user-facing install/update/uninstall scripts) and
`tools/` (release and CI helper modules).
