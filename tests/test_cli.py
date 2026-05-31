"""CLI tests."""
from __future__ import annotations

import json
import os
import stat
import sys
import types
import zipfile
from pathlib import Path

from vocalize.cli import main
from vocalize.doctor import DoctorCheck


def test_cli_doctor_returns_zero_when_all_checks_pass(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        "vocalize.cli.run_doctor",
        lambda *, skip_llm_probe=False: [DoctorCheck("macos", True, "ok")],
    )

    assert main(["doctor"]) == 0
    assert "PASS macos: ok" in capsys.readouterr().out


def test_cli_doctor_returns_one_when_check_fails(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        "vocalize.cli.run_doctor",
        lambda *, skip_llm_probe=False: [
            DoctorCheck(
                "speech_provider",
                False,
                "speech permission is denied",
                "grant permission",
            )
        ],
    )

    assert main(["doctor"]) == 1
    out = capsys.readouterr().out
    assert "FAIL speech_provider: speech permission is denied" in out
    assert "fix: grant permission" in out


def test_cli_doctor_passes_skip_llm_probe(monkeypatch) -> None:
    seen: list[bool] = []

    def fake_run_doctor(*, skip_llm_probe: bool = False):
        seen.append(skip_llm_probe)
        return [DoctorCheck("llm_probe", True, "skipped")]

    monkeypatch.setattr("vocalize.cli.run_doctor", fake_run_doctor)

    assert main(["doctor", "--skip-llm-probe"]) == 0
    assert seen == [True]


def test_cli_setup_writes_env_providers_and_preferences(
    monkeypatch,
    tmp_path,
    capsys,
) -> None:
    monkeypatch.setenv("VOCALIZE_INSTALL_ROOT", str(tmp_path))
    monkeypatch.setattr("vocalize.config.load_dotenv", lambda *args, **kwargs: None)

    result = main(
        [
            "setup",
            "--non-interactive",
            "--llm-api-key",
            "sk-test",
            "--llm-base-url",
            "https://llm.example/v1",
            "--llm-model",
            "test-model",
            "--llm-thinking-mode",
            "enabled",
            "--global-command",
            "no",
            "--open-browser",
            "no",
        ]
    )

    assert result == 0
    assert (tmp_path / ".vocalize-install-root").is_file()
    env_text = (tmp_path / "config" / ".env").read_text(encoding="utf-8")
    assert "OPENAI_API_KEY=sk-test" in env_text
    assert "OPENAI_BASE_URL=https://llm.example/v1" in env_text
    assert "OPENAI_MODEL=test-model" in env_text
    assert "OPENAI_THINKING_MODE=enabled" in env_text
    providers = (tmp_path / "config" / "providers.yaml").read_text(encoding="utf-8")
    assert "provider: macos-native" in providers
    preferences = json.loads(
        (tmp_path / "config" / "preferences.json").read_text(encoding="utf-8")
    )
    assert preferences == {"open_browser": False}
    assert "Configured:" in capsys.readouterr().out


def test_cli_setup_can_create_removable_global_symlink(
    monkeypatch,
    tmp_path,
) -> None:
    install_root = tmp_path / "VocalizeAI"
    global_bin = tmp_path / "global-bin"
    install_root.mkdir()
    (install_root / "vocalize").write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setenv("VOCALIZE_INSTALL_ROOT", str(install_root))
    monkeypatch.setenv("VOCALIZE_GLOBAL_BIN_DIR", str(global_bin))
    monkeypatch.setattr("vocalize.config.load_dotenv", lambda *args, **kwargs: None)

    assert (
        main(
            [
                "setup",
                "--non-interactive",
                "--llm-api-key",
                "sk-test",
                "--global-command",
                "yes",
                "--open-browser",
                "yes",
            ]
        )
        == 0
    )

    symlink = global_bin / "vocalize"
    assert symlink.is_symlink()
    assert symlink.resolve() == install_root / "vocalize"
    install_state = json.loads(
        (install_root / "config" / "install.json").read_text(encoding="utf-8")
    )
    assert install_state["global_symlink"] == str(symlink)


def test_cli_uninstall_removes_marked_install_and_symlink(monkeypatch, tmp_path) -> None:
    install_root = tmp_path / "VocalizeAI"
    global_bin = tmp_path / "global-bin"
    install_root.mkdir()
    (install_root / "vocalize").write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setenv("VOCALIZE_INSTALL_ROOT", str(install_root))
    monkeypatch.setenv("VOCALIZE_GLOBAL_BIN_DIR", str(global_bin))
    monkeypatch.setattr("vocalize.config.load_dotenv", lambda *args, **kwargs: None)

    assert (
        main(
            [
                "setup",
                "--non-interactive",
                "--llm-api-key",
                "sk-test",
                "--global-command",
                "yes",
                "--open-browser",
                "no",
            ]
        )
        == 0
    )
    symlink = global_bin / "vocalize"
    assert symlink.is_symlink()

    assert main(["uninstall", "--yes"]) == 0

    assert not install_root.exists()
    assert not symlink.exists()


def test_cli_update_preserves_config_logs_and_cache(monkeypatch, tmp_path) -> None:
    install_root = tmp_path / "VocalizeAI"
    bundle = tmp_path / "bundle" / "VocalizeAI-0.1.1-macos-arm64"
    artifact = tmp_path / "update.zip"
    install_root.mkdir()
    for name in ["config", "logs", "cache"]:
        (install_root / name).mkdir()
    (install_root / "config" / ".env").write_text("OPENAI_API_KEY=old\n")
    (install_root / "logs" / "vocalize.log").write_text("old log\n")
    (install_root / ".vocalize-install-root").write_text("marker\n")
    (install_root / "VERSION").write_text("0.1.0\n")

    (bundle / "bin").mkdir(parents=True)
    (bundle / "app").mkdir()
    (bundle / "config").mkdir()
    (bundle / "logs").mkdir()
    (bundle / "cache").mkdir()
    (bundle / "VERSION").write_text("0.1.1\n")
    (bundle / "vocalize").write_text("#!/bin/sh\n")
    (bundle / "vocalize").chmod(0o755)
    (bundle / "target").write_text("target\n")
    os.symlink("target", bundle / "link")
    _zip_dir(bundle, artifact)

    monkeypatch.setenv("VOCALIZE_INSTALL_ROOT", str(install_root))

    assert main(["update", "--artifact", str(artifact)]) == 0

    assert (install_root / "VERSION").read_text(encoding="utf-8") == "0.1.1\n"
    assert (install_root / "config" / ".env").read_text() == "OPENAI_API_KEY=old\n"
    assert (install_root / "logs" / "vocalize.log").read_text() == "old log\n"
    assert os.access(install_root / "vocalize", os.X_OK)
    assert (install_root / "link").is_symlink()
    assert os.readlink(install_root / "link") == "target"


def test_cli_start_foreground_applies_install_env(monkeypatch, tmp_path) -> None:
    install_root = tmp_path / "VocalizeAI"
    (install_root / "config").mkdir(parents=True)
    (install_root / "logs").mkdir()
    (install_root / "cache").mkdir()
    (install_root / "config" / ".env").write_text("OPENAI_API_KEY=sk-test\n")
    monkeypatch.setenv("VOCALIZE_INSTALL_ROOT", str(install_root))
    seen_env: list[str | None] = []
    monkeypatch.setitem(
        sys.modules,
        "vocalize.main",
        types.SimpleNamespace(
            main=lambda: seen_env.append(os.getenv("VOCALIZE_ENV_FILE"))
        ),
    )

    assert main(["start", "--no-browser"]) == 0

    assert seen_env == [str(install_root / "config" / ".env")]


def _zip_dir(source: Path, destination: Path) -> None:
    with zipfile.ZipFile(destination, "w") as archive:
        for path in source.rglob("*"):
            arcname = path.relative_to(source.parent).as_posix()
            if path.is_symlink():
                info = zipfile.ZipInfo(arcname)
                info.external_attr = (stat.S_IFLNK | 0o755) << 16
                archive.writestr(info, os.readlink(path))
            else:
                archive.write(path, arcname)
