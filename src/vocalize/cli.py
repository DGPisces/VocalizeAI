"""Command-line entry points for local VocalizeAI installs."""
from __future__ import annotations

import argparse
import getpass
import hashlib
import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import webbrowser
import zipfile
from pathlib import Path

from vocalize.config import Config
from vocalize.doctor import run_doctor
from vocalize.install_state import (
    InstallPaths,
    detect_install_root,
    ensure_install_dirs,
    mark_install_root,
    read_preferences,
    record_global_symlink,
    remove_install_root,
    write_env_file,
    write_preferences,
    write_providers_yaml,
)


YES_VALUES = {"1", "yes", "y", "true", "on"}
NO_VALUES = {"0", "no", "n", "false", "off"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="vocalize")
    subcommands = parser.add_subparsers(dest="command", required=True)

    setup_parser = subcommands.add_parser("setup", help="configure this install")
    setup_parser.add_argument("--llm-base-url")
    setup_parser.add_argument("--llm-api-key")
    setup_parser.add_argument("--llm-model")
    setup_parser.add_argument("--global-command", choices=["yes", "no"])
    setup_parser.add_argument("--open-browser", choices=["yes", "no"])
    setup_parser.add_argument("--non-interactive", action="store_true")

    doctor_parser = subcommands.add_parser("doctor", help="check readiness")
    doctor_parser.add_argument("--skip-llm-probe", action="store_true")

    start_parser = subcommands.add_parser("start", help="start VocalizeAI")
    start_parser.add_argument("--background", action="store_true")
    start_parser.add_argument("--no-browser", action="store_true")

    subcommands.add_parser("stop", help="stop background server")
    subcommands.add_parser("status", help="show background server status")

    logs_parser = subcommands.add_parser("logs", help="print local logs")
    logs_parser.add_argument("--lines", type=int, default=80)
    logs_parser.add_argument("--follow", action="store_true")

    update_parser = subcommands.add_parser("update", help="update from artifact")
    update_parser.add_argument("--artifact", type=Path, required=True)
    update_parser.add_argument("--checksums", type=Path)

    uninstall_parser = subcommands.add_parser("uninstall", help="remove install")
    uninstall_parser.add_argument("--yes", action="store_true")

    subcommands.add_parser("serve", help="run the backend server")

    args = parser.parse_args(argv)
    paths = ensure_install_dirs(detect_install_root())

    if args.command == "setup":
        return _setup(args, paths)
    if args.command == "doctor":
        return _doctor(args, paths)
    if args.command == "start":
        return _start(args, paths)
    if args.command == "stop":
        return _stop(paths)
    if args.command == "status":
        return _status(paths)
    if args.command == "logs":
        return _logs(args, paths)
    if args.command == "update":
        return _update(args, paths)
    if args.command == "uninstall":
        return _uninstall(args, paths)
    if args.command == "serve":
        from vocalize.main import main as serve_main

        serve_main()
        return 0

    parser.error(f"unknown command: {args.command}")


def _setup(args: argparse.Namespace, paths: InstallPaths) -> int:
    cfg = Config.from_env()
    base_url = _value_or_prompt(
        args.llm_base_url,
        "LLM base URL",
        default=cfg.openai_base_url,
        non_interactive=args.non_interactive,
    )
    model = _value_or_prompt(
        args.llm_model,
        "LLM model",
        default=cfg.openai_model,
        non_interactive=args.non_interactive,
    )
    api_key = args.llm_api_key
    if not api_key and not args.non_interactive:
        api_key = getpass.getpass("LLM API key: ")
    if not api_key:
        print("ERROR: LLM API key is required", file=sys.stderr)
        return 2

    open_browser = _bool_choice(
        args.open_browser,
        "Open browser automatically on start?",
        default=True,
        non_interactive=args.non_interactive,
    )
    global_command = _bool_choice(
        args.global_command,
        "Install optional global `vocalize` command symlink?",
        default=False,
        non_interactive=args.non_interactive,
    )

    mark_install_root(paths.root)
    write_env_file(
        paths,
        openai_api_key=api_key,
        openai_base_url=base_url,
        openai_model=model,
    )
    write_providers_yaml(paths)
    write_preferences(paths, {"open_browser": open_browser})
    if global_command:
        symlink_path = _create_global_symlink(paths)
        print(f"Global command: {symlink_path}")
    else:
        record_global_symlink(paths, None)

    print(f"Configured: {paths.root}")
    print(f"Env: {paths.env_file}")
    print(f"Providers: {paths.providers_file}")
    return 0


def _doctor(args: argparse.Namespace, paths: InstallPaths) -> int:
    env = _install_env(paths)
    previous = os.environ.copy()
    os.environ.update(env)
    try:
        checks = run_doctor(skip_llm_probe=args.skip_llm_probe)
    finally:
        os.environ.clear()
        os.environ.update(previous)
    for check in checks:
        status = "PASS" if check.ok else "FAIL"
        print(f"{status} {check.name}: {check.detail}")
        if check.remediation and not check.ok:
            print(f"  fix: {check.remediation}")
    return 0 if all(check.ok for check in checks) else 1


def _start(args: argparse.Namespace, paths: InstallPaths) -> int:
    env = _install_env(paths)
    preferences = read_preferences(paths)
    open_browser = bool(preferences.get("open_browser", True)) and not args.no_browser

    if args.background:
        if _is_pid_running(_read_pid(paths)):
            print("VocalizeAI is already running")
            return 0
        paths.logs_dir.mkdir(parents=True, exist_ok=True)
        log_handle = paths.log_file.open("ab")
        process = subprocess.Popen(  # noqa: S603 - local packaged command.
            _serve_command(),
            env=env,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        paths.pid_file.write_text(str(process.pid), encoding="utf-8")
        print(f"Started in background: pid {process.pid}")
        if open_browser:
            webbrowser.open(_local_url(env))
        return 0

    if open_browser:
        webbrowser.open(_local_url(env))
    from vocalize.main import main as serve_main

    previous = os.environ.copy()
    os.environ.update(env)
    try:
        serve_main()
        return 0
    finally:
        os.environ.clear()
        os.environ.update(previous)


def _stop(paths: InstallPaths) -> int:
    pid = _read_pid(paths)
    if not _is_pid_running(pid):
        if paths.pid_file.exists():
            paths.pid_file.unlink()
        print("VocalizeAI is not running")
        return 0
    assert pid is not None
    os.kill(pid, signal.SIGTERM)
    for _ in range(20):
        if not _is_pid_running(pid):
            break
        time.sleep(0.1)
    if _is_pid_running(pid):
        print(f"ERROR: process did not stop: {pid}", file=sys.stderr)
        return 1
    paths.pid_file.unlink(missing_ok=True)
    print("Stopped")
    return 0


def _status(paths: InstallPaths) -> int:
    pid = _read_pid(paths)
    if _is_pid_running(pid):
        print(f"running pid={pid}")
        return 0
    print("stopped")
    return 1


def _logs(args: argparse.Namespace, paths: InstallPaths) -> int:
    if not paths.log_file.is_file():
        print(f"No log file yet: {paths.log_file}")
        return 0
    if args.follow:
        return subprocess.call(["tail", "-f", str(paths.log_file)])  # noqa: S603,S607
    lines = max(1, args.lines)
    content = paths.log_file.read_text(encoding="utf-8", errors="replace").splitlines()
    for line in content[-lines:]:
        print(line)
    return 0


def _update(args: argparse.Namespace, paths: InstallPaths) -> int:
    if args.checksums:
        _verify_sha256sums(
            args.checksums,
            base_dir=args.artifact.parent,
            artifact_names=[args.artifact.name],
        )

    with tempfile.TemporaryDirectory(prefix="vocalize-update-") as tmp:
        extract_root = Path(tmp)
        _extract_release_zip(args.artifact, extract_root)
        bundle = _single_extracted_bundle(extract_root)
        _copy_update_payload(bundle, paths.root)
    print(f"Updated: {paths.root}")
    return 0


def _uninstall(args: argparse.Namespace, paths: InstallPaths) -> int:
    if not args.yes:
        answer = input(f"Remove {paths.root}? Type 'yes' to continue: ")
        if answer.strip().lower() != "yes":
            print("Cancelled")
            return 1
    remove_install_root(paths)
    print(f"Removed: {paths.root}")
    return 0


def _install_env(paths: InstallPaths) -> dict[str, str]:
    env = os.environ.copy()
    env["VOCALIZE_INSTALL_ROOT"] = str(paths.root)
    env["LOG_DIR"] = str(paths.logs_dir)
    if paths.env_file.is_file():
        env["VOCALIZE_ENV_FILE"] = str(paths.env_file)
    provider = paths.bin_dir / "vocalize-mac-speech-provider"
    if provider.is_file():
        env.setdefault("VOCALIZE_SPEECH_PROVIDER_AUTO_START", "1")
        env.setdefault("VOCALIZE_SPEECH_PROVIDER_COMMAND", str(provider))
    frontend = paths.app_dir / "vocalize" / "_internal" / "vocalize_runtime" / "frontend"
    if (frontend / "index.html").is_file():
        env.setdefault("VOCALIZE_FRONTEND_DIST", str(frontend))
    return env


def _serve_command() -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, "serve"]
    return [sys.executable, "-m", "vocalize", "serve"]


def _local_url(env: dict[str, str]) -> str:
    host = env.get("VOCALIZE_HOST", "127.0.0.1")
    port = env.get("VOCALIZE_PORT", "8080")
    return f"http://{host}:{port}"


def _value_or_prompt(
    value: str | None,
    prompt: str,
    *,
    default: str,
    non_interactive: bool,
) -> str:
    if value:
        return value
    if non_interactive:
        return default
    answer = input(f"{prompt} [{default}]: ").strip()
    return answer or default


def _bool_choice(
    value: str | None,
    prompt: str,
    *,
    default: bool,
    non_interactive: bool,
) -> bool:
    if value:
        return value in YES_VALUES
    if non_interactive:
        return default
    suffix = "Y/n" if default else "y/N"
    answer = input(f"{prompt} [{suffix}]: ").strip().lower()
    if not answer:
        return default
    if answer in YES_VALUES:
        return True
    if answer in NO_VALUES:
        return False
    raise ValueError(f"invalid yes/no answer: {answer}")


def _create_global_symlink(paths: InstallPaths) -> Path:
    bin_dir = Path(os.getenv("VOCALIZE_GLOBAL_BIN_DIR", "/usr/local/bin"))
    bin_dir.mkdir(parents=True, exist_ok=True)
    symlink = bin_dir / "vocalize"
    if symlink.exists() or symlink.is_symlink():
        if symlink.is_symlink() and symlink.resolve() == paths.local_cli.resolve():
            record_global_symlink(paths, symlink)
            return symlink
        raise RuntimeError(f"refusing to replace existing global command: {symlink}")
    symlink.symlink_to(paths.local_cli)
    record_global_symlink(paths, symlink)
    return symlink


def _read_pid(paths: InstallPaths) -> int | None:
    if not paths.pid_file.is_file():
        return None
    try:
        return int(paths.pid_file.read_text(encoding="utf-8").strip())
    except ValueError:
        return None


def _is_pid_running(pid: int | None) -> bool:
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _single_extracted_bundle(extract_root: Path) -> Path:
    bundles = [path for path in extract_root.iterdir() if path.is_dir()]
    if len(bundles) != 1:
        raise RuntimeError("release artifact must contain exactly one bundle directory")
    return bundles[0]


def _extract_release_zip(artifact: Path, extract_root: Path) -> None:
    """Extract a release zip while preserving symlinks and executable bits."""
    with zipfile.ZipFile(artifact) as archive:
        for member in archive.infolist():
            if member.filename.endswith("/"):
                (extract_root / member.filename).mkdir(parents=True, exist_ok=True)
                continue
            target = extract_root / member.filename
            resolved = target.resolve(strict=False)
            if not resolved.is_relative_to(extract_root.resolve()):
                raise RuntimeError(f"unsafe path in release artifact: {member.filename}")
            target.parent.mkdir(parents=True, exist_ok=True)
            mode = member.external_attr >> 16
            if stat.S_ISLNK(mode):
                link_target = archive.read(member).decode("utf-8")
                if target.exists() or target.is_symlink():
                    target.unlink()
                os.symlink(link_target, target)
                continue
            with archive.open(member) as source, target.open("wb") as destination:
                shutil.copyfileobj(source, destination)
            permissions = mode & 0o777
            if permissions:
                target.chmod(permissions)


def _copy_update_payload(bundle: Path, install_root: Path) -> None:
    preserve = {"config", "logs", "cache"}
    for child in bundle.iterdir():
        if child.name in preserve:
            continue
        destination = install_root / child.name
        if destination.is_dir():
            shutil.rmtree(destination)
        elif destination.exists() or destination.is_symlink():
            destination.unlink()
        if child.is_symlink():
            os.symlink(os.readlink(child), destination)
        elif child.is_dir():
            shutil.copytree(child, destination, symlinks=True)
        else:
            shutil.copy2(child, destination)


def _verify_sha256sums(
    checksum_file: Path,
    *,
    base_dir: Path,
    artifact_names: list[str],
) -> None:
    wanted = set(artifact_names)
    verified: set[str] = set()
    for raw_line in checksum_file.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        expected, filename = line.split()
        if filename not in wanted:
            continue
        path = base_dir / filename
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != expected:
            raise RuntimeError(f"checksum mismatch for {filename}")
        verified.add(filename)
    missing = wanted - verified
    if missing:
        raise RuntimeError(f"missing checksum entries for: {', '.join(sorted(missing))}")


if __name__ == "__main__":
    sys.exit(main())
