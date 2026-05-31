"""Release artifact manifest and checksum utilities."""
from __future__ import annotations

import argparse
import hashlib
import json
import platform
import sys
import tomllib
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable, Iterable, cast


LAYOUT_VERSION = "v0.1-macos-onefolder"


def read_project_version(pyproject_path: Path) -> str:
    """Read the project version from ``pyproject.toml``."""
    data = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
    version = data.get("project", {}).get("version")
    if not isinstance(version, str) or not version:
        raise ValueError(f"missing [project].version in {pyproject_path}")
    return version


def sha256_file(path: Path) -> str:
    """Return the hex SHA-256 digest for a file."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_sha256sums(assets: Iterable[Path], output: Path) -> list[str]:
    """Write a GitHub Release-compatible ``SHA256SUMS`` file."""
    lines: list[str] = []
    for asset in assets:
        asset = asset.resolve()
        if not asset.is_file():
            raise FileNotFoundError(asset)
        lines.append(f"{sha256_file(asset)}  {asset.name}")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return lines


def verify_sha256sums(
    checksum_file: Path,
    *,
    base_dir: Path | None = None,
    artifact_names: Iterable[str] | None = None,
) -> list[str]:
    """Verify entries in ``SHA256SUMS`` and return verified artifact names."""
    if base_dir is None:
        base_dir = checksum_file.parent
    wanted = set(artifact_names or [])
    verified: list[str] = []

    for line_number, raw_line in enumerate(
        checksum_file.read_text(encoding="utf-8").splitlines(),
        start=1,
    ):
        line = raw_line.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) != 2:
            raise ValueError(f"invalid checksum line {line_number}: {raw_line!r}")
        expected, filename = parts
        if wanted and filename not in wanted:
            continue
        path = base_dir / filename
        if not path.is_file():
            raise FileNotFoundError(path)
        actual = sha256_file(path)
        if actual != expected:
            raise ValueError(
                f"checksum mismatch for {filename}: expected {expected}, got {actual}"
            )
        verified.append(filename)

    if wanted and wanted != set(verified):
        missing = ", ".join(sorted(wanted - set(verified)))
        raise ValueError(f"missing checksum entries for: {missing}")
    return verified


def write_release_manifest(
    output: Path,
    *,
    version: str,
    artifact_name: str,
    arch: str,
    signing_mode: str,
    entrypoint: str,
    backend_executable: str,
    frontend_dist: str,
    speech_provider: str,
) -> dict[str, object]:
    """Write the versioned release artifact manifest."""
    manifest: dict[str, object] = {
        "schema_version": 1,
        "layout_version": LAYOUT_VERSION,
        "app_version": version,
        "artifact_name": artifact_name,
        "created_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
        "platform": {
            "os": "macos",
            "arch": arch,
            "builder": platform.platform(),
        },
        "entrypoints": {
            "cli": entrypoint,
            "backend": backend_executable,
            "speech_provider": speech_provider,
        },
        "resources": {
            "frontend_dist": frontend_dist,
            "config_template": "config/.env.example",
        },
        "signing": {
            "mode": signing_mode,
            "developer_id_required_for_public_release": True,
            "notarization_required_for_public_release": True,
        },
        "preserved_user_state": [
            "config/",
            "logs/",
            "cache/",
        ],
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return manifest


def _version_command(args: argparse.Namespace) -> int:
    print(read_project_version(args.pyproject))
    return 0


def _manifest_command(args: argparse.Namespace) -> int:
    write_release_manifest(
        args.output,
        version=args.version,
        artifact_name=args.artifact_name,
        arch=args.arch,
        signing_mode=args.signing_mode,
        entrypoint=args.entrypoint,
        backend_executable=args.backend_executable,
        frontend_dist=args.frontend_dist,
        speech_provider=args.speech_provider,
    )
    return 0


def _sha256_command(args: argparse.Namespace) -> int:
    write_sha256sums(args.assets, args.output)
    return 0


def _verify_command(args: argparse.Namespace) -> int:
    verify_sha256sums(
        args.checksums,
        base_dir=args.base_dir,
        artifact_names=args.artifact_name,
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tools.release.artifacts")
    subcommands = parser.add_subparsers(dest="command", required=True)

    version_parser = subcommands.add_parser("version")
    version_parser.add_argument("--pyproject", type=Path, required=True)
    version_parser.set_defaults(func=_version_command)

    manifest_parser = subcommands.add_parser("manifest")
    manifest_parser.add_argument("--output", type=Path, required=True)
    manifest_parser.add_argument("--version", required=True)
    manifest_parser.add_argument("--artifact-name", required=True)
    manifest_parser.add_argument("--arch", required=True)
    manifest_parser.add_argument("--signing-mode", required=True)
    manifest_parser.add_argument("--entrypoint", required=True)
    manifest_parser.add_argument("--backend-executable", required=True)
    manifest_parser.add_argument("--frontend-dist", required=True)
    manifest_parser.add_argument("--speech-provider", required=True)
    manifest_parser.set_defaults(func=_manifest_command)

    sha_parser = subcommands.add_parser("sha256")
    sha_parser.add_argument("--output", type=Path, required=True)
    sha_parser.add_argument("assets", type=Path, nargs="+")
    sha_parser.set_defaults(func=_sha256_command)

    verify_parser = subcommands.add_parser("verify")
    verify_parser.add_argument("--checksums", type=Path, required=True)
    verify_parser.add_argument("--base-dir", type=Path)
    verify_parser.add_argument("--artifact-name", action="append")
    verify_parser.set_defaults(func=_verify_command)

    args = parser.parse_args(argv)
    try:
        command = cast(Callable[[argparse.Namespace], int], args.func)
        return command(args)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
