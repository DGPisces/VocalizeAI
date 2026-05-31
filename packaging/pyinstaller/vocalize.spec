# -*- mode: python ; coding: utf-8 -*-
from __future__ import annotations

import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules


ROOT = Path(os.environ.get("VOCALIZE_REPO_ROOT", Path.cwd())).resolve()
FRONTEND_DIST = Path(
    os.environ.get("VOCALIZE_FRONTEND_DIST", ROOT / "frontend" / "dist")
).resolve()
MACOS_HELPER = Path(
    os.environ.get(
        "VOCALIZE_MACOS_HELPER",
        ROOT
        / "macos"
        / "VocalizeSpeechProvider"
        / ".build"
        / "release"
        / "vocalize-mac-speech-provider",
    )
).resolve()
ENV_TEMPLATE = ROOT / ".env.example"

if not FRONTEND_DIST.joinpath("index.html").is_file():
    raise SystemExit(f"Vite frontend build not found: {FRONTEND_DIST}")
if not MACOS_HELPER.is_file():
    raise SystemExit(f"macOS speech provider helper not found: {MACOS_HELPER}")
if not ENV_TEMPLATE.is_file():
    raise SystemExit(f"config template not found: {ENV_TEMPLATE}")

datas = collect_data_files("vocalize.dialogue.prompts")
datas += [
    (str(FRONTEND_DIST), "vocalize_runtime/frontend"),
    (str(ENV_TEMPLATE), "vocalize_runtime/config"),
]

binaries = [
    (str(MACOS_HELPER), "vocalize_runtime/bin"),
]

hiddenimports = (
    collect_submodules("uvicorn")
    + collect_submodules("websockets")
    + [
        "httptools",
        "uvloop",
        "watchfiles",
    ]
)

a = Analysis(
    [str(ROOT / "src" / "vocalize" / "__main__.py")],
    pathex=[str(ROOT / "src")],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="vocalize",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="vocalize",
)
