from __future__ import annotations

from pathlib import Path

from tools.ci.public_tree_audit import audit, main


def test_audit_rejects_private_paths(tmp_path: Path) -> None:
    (tmp_path / "AGENTS.md").write_text("private\n")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("print('ok')\n")

    findings = audit(tmp_path, ["AGENTS.md", "src/app.py"])

    assert any("AGENTS.md" in finding.format() for finding in findings)


def test_audit_rejects_old_gpu_default_content(tmp_path: Path) -> None:
    readme = tmp_path / "README.md"
    readme.write_text("Set GPU_HOST before deploy.\n")

    findings = audit(tmp_path, ["README.md"])

    assert any("old GPU default deployment variable" in finding.format() for finding in findings)


def test_audit_scans_dot_directories(tmp_path: Path) -> None:
    issue_template = tmp_path / ".github" / "ISSUE_TEMPLATE" / "bug.yml"
    issue_template.parent.mkdir(parents=True)
    issue_template.write_text("OS: Raspberry Pi OS\n")

    findings = audit(tmp_path, [".github/ISSUE_TEMPLATE/bug.yml"])

    assert any("old Pi deployment reference" in finding.format() for finding in findings)


def test_audit_allows_mac_first_public_content(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text(
        "Install VocalizeAI on macOS and configure your LLM endpoint.\n"
    )
    (tmp_path / ".env.example").write_text("LLM_API_KEY=your_api_key\n")

    findings = audit(tmp_path, ["README.md", ".env.example"])

    assert findings == []


def test_main_accepts_file_list(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    (tmp_path / "README.md").write_text("Mac-first local installer.\n")
    file_list = tmp_path / "files.txt"
    file_list.write_text("README.md\n")

    result = main(["--root", str(tmp_path), "--file-list", str(file_list)])

    assert result == 0
    assert "Public tree audit passed" in capsys.readouterr().out
