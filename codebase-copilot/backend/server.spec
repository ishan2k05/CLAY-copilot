# -*- mode: python ; coding: utf-8 -*-
import sys
import os
from PyInstaller.utils.hooks import collect_data_files, collect_submodules, collect_dynamic_libs

block_cipher = None

# Collect all dynamic imports required by FastAPI, Uvicorn, and parsers
hidden_imports = [
    "uvicorn",
    "uvicorn.logging",
    "uvicorn.loops",
    "uvicorn.loops.auto",
    "uvicorn.loops.asyncio",
    "uvicorn.protocols",
    "uvicorn.protocols.http",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespans",
    "uvicorn.lifespans.on",
    "uvicorn.lifespans.off",
    "fastapi",
    "pydantic",
    "sqlite3",
    "aiosqlite",
    "httpx",
    "unidiff",
    "tree_sitter",
    "tree_sitter_typescript",
    "tree_sitter_rust",
    "tree_sitter_go",
    "tree_sitter_cpp",
    "app",
    "app.main",
] + collect_submodules("app")

datas = collect_data_files("app")

# Collect compiled C-extension binaries (.pyd DLLs) for Tree-Sitter grammars
binaries = []
for pkg in ["tree_sitter", "tree_sitter_typescript", "tree_sitter_rust", "tree_sitter_go", "tree_sitter_cpp"]:
    try:
        binaries.extend(collect_dynamic_libs(pkg))
    except Exception:
        pass

# Include runtime_settings.json as default template if present
if os.path.exists("runtime_settings.json"):
    datas.append(("runtime_settings.json", "."))

a = Analysis(
    ['run_server.py'],
    pathex=['.'],
    binaries=binaries,
    datas=datas,
    hiddenimports=hidden_imports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['tkinter', 'matplotlib', 'pandas', 'IPython', 'pytest'],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='clay-server',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
