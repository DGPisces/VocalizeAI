"""Release artifact helper tests."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from tools.release.artifacts import (
    read_project_version,
    verify_sha256sums,
    write_release_manifest,
    write_sha256sums,
)


def test_read_project_version_from_pyproject(tmp_path) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        '[project]\nname = "vocalize-ai"\nversion = "0.1.0"\n',
        encoding="utf-8",
    )

    assert read_project_version(pyproject) == "0.1.0"


def test_sha256sums_round_trip_and_detects_tamper(tmp_path) -> None:
    asset = tmp_path / "VocalizeAI-0.1.0-macos-arm64.zip"
    asset.write_bytes(b"artifact bytes")
    checksum_file = tmp_path / "SHA256SUMS"

    lines = write_sha256sums([asset], checksum_file)

    assert len(lines) == 1
    assert verify_sha256sums(checksum_file) == [asset.name]

    asset.write_bytes(b"tampered")
    with pytest.raises(ValueError, match="checksum mismatch"):
        verify_sha256sums(checksum_file)


def test_release_manifest_records_versioned_layout(tmp_path) -> None:
    manifest_path = tmp_path / "manifest.json"

    manifest = write_release_manifest(
        manifest_path,
        version="0.1.0",
        artifact_name="VocalizeAI-0.1.0-macos-arm64",
        arch="arm64",
        signing_mode="ad-hoc",
        entrypoint="bin/vocalize",
        backend_executable="app/vocalize/vocalize",
        frontend_dist="app/vocalize/_internal/vocalize_runtime/frontend",
        speech_provider="bin/vocalize-mac-speech-provider",
    )

    saved = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert saved == manifest
    assert saved["layout_version"] == "v0.1-macos-onefolder"
    assert saved["entrypoints"]["cli"] == "bin/vocalize"
    assert saved["resources"]["frontend_dist"].endswith("vocalize_runtime/frontend")
    assert saved["signing"]["notarization_required_for_public_release"] is True


def test_install_verify_release_script_accepts_matching_artifact(tmp_path) -> None:
    if shutil.which("shasum") is None:
        pytest.skip("shasum is not installed")

    asset = tmp_path / "VocalizeAI-0.1.0-macos-arm64.zip"
    asset.write_bytes(b"artifact bytes")
    checksum_file = tmp_path / "SHA256SUMS"
    write_sha256sums([asset], checksum_file)

    result = subprocess.run(
        [
            "bash",
            "install/verify-release.sh",
            str(checksum_file),
            str(asset),
        ],
        check=False,
        cwd=Path(__file__).resolve().parents[1],
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0, result.stderr
    assert f"{asset.name}: OK" in result.stdout
