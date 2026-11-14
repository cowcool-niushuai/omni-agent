# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller onedir build for the OmniAgent backend."""

from pathlib import Path

from PyInstaller.utils.hooks import (
    collect_all,
    collect_data_files,
    collect_dynamic_libs,
    collect_submodules,
)

ROOT = Path(SPECPATH).parent
if ROOT.name == "desktop":
    ROOT = ROOT.parent

datas = [
    (str(ROOT / "static"), "static"),
    (str(ROOT / "skills"), "skills"),
    (str(ROOT / "mcp_servers"), "mcp_servers"),
    (str(ROOT / "model_config.example.json"), "."),
    (str(ROOT / ".env.example"), "."),
]
binaries = []
hiddenimports = []

for package in (
    "uvicorn",
    "mcp",
    "langgraph",
    "langgraph.checkpoint.sqlite",
    "openai",
    "anthropic",
    "playwright",
    "tiktoken",
    "structlog",
):
    if package == "mcp":
        # The optional MCP CLI imports typer, which the runtime does not need.
        datas += collect_data_files(package)
        binaries += collect_dynamic_libs(package)
        hiddenimports += collect_submodules(
            package, filter=lambda name: not name.startswith("mcp.cli")
        )
        continue
    package_datas, package_binaries, package_hidden = collect_all(package)
    datas += package_datas
    binaries += package_binaries
    hiddenimports += package_hidden

a = Analysis(
    [str(ROOT / "desktop_backend.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["tkinter", "matplotlib", "numpy", "pandas", "pytest"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="omni-agent-server",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="omni-agent-server",
)
