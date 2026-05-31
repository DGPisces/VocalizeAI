"""Installer script smoke tests."""
from __future__ import annotations

import hashlib
import subprocess
import zipfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_install_and_uninstall_scripts_parse() -> None:
    result = subprocess.run(
        ["bash", "-n", "install/install.sh", "install/uninstall.sh"],
        cwd=REPO_ROOT,
        check=False,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0, result.stderr


def test_installer_creates_self_contained_local_install(tmp_path) -> None:
    artifact = tmp_path / "VocalizeAI-0.1.0-macos-arm64.zip"
    checksums = tmp_path / "SHA256SUMS"
    install_dir = tmp_path / "VocalizeAI"
    bundle = tmp_path / "bundle" / "VocalizeAI-0.1.0-macos-arm64"
    _write_fake_bundle(bundle)
    _zip_dir(bundle, artifact)
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    checksums.write_text(f"{digest}  {artifact.name}\n", encoding="utf-8")

    result = subprocess.run(
        [
            "bash",
            str(REPO_ROOT / "install" / "install.sh"),
            "--artifact",
            str(artifact),
            "--checksums",
            str(checksums),
            "--install-dir",
            str(install_dir),
            "--yes",
        ],
        cwd=tmp_path,
        check=False,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0, result.stderr
    assert (install_dir / ".vocalize-install-root").is_file()
    assert (install_dir / "vocalize").is_file()
    assert (install_dir / "uninstall.sh").is_file()
    assert (install_dir / "bin").is_dir()
    assert (install_dir / "app").is_dir()
    assert (install_dir / "config").is_dir()
    assert (install_dir / "logs").is_dir()
    assert (install_dir / "cache").is_dir()

    uninstall = subprocess.run(
        ["bash", str(install_dir / "uninstall.sh"), "--yes"],
        check=False,
        text=True,
        capture_output=True,
    )

    assert uninstall.returncode == 0, uninstall.stderr
    assert not install_dir.exists()


def _write_fake_bundle(bundle: Path) -> None:
    for directory in [
        bundle / "bin",
        bundle / "app" / "vocalize",
        bundle / "config",
        bundle / "logs",
        bundle / "cache",
    ]:
        directory.mkdir(parents=True)
    for script in [
        bundle / "vocalize",
        bundle / "bin" / "vocalize",
        bundle / "bin" / "vocalize-mac-speech-provider",
    ]:
        script.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    (bundle / "uninstall.sh").write_text(
        (REPO_ROOT / "install" / "uninstall.sh").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (bundle / ".vocalize-install-root").write_text("marker\n", encoding="utf-8")
    (bundle / "VERSION").write_text("0.1.0\n", encoding="utf-8")
    (bundle / "manifest.json").write_text("{}\n", encoding="utf-8")
    (bundle / "config" / ".env.example").write_text("", encoding="utf-8")


def _zip_dir(source: Path, destination: Path) -> None:
    with zipfile.ZipFile(destination, "w") as archive:
        for path in source.rglob("*"):
            archive.write(path, path.relative_to(source.parent))
