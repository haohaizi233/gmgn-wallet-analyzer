# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path

from PyInstaller.building.api import COLLECT, EXE, PYZ
from PyInstaller.building.build_main import Analysis
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = Path(SPECPATH).resolve()

datas = [
    (str(ROOT / "data" / "special_assets.json"), "data"),
    (str(ROOT / "data" / "solana_program_registry.json"), "data"),
    (str(ROOT / ".env.example"), "."),
]
env_path = ROOT / ".env"
if env_path.exists():
    datas.append((str(env_path), "defaults"))
wallet_path = ROOT / "地址.txt"
if wallet_path.exists():
    datas.append((str(wallet_path), "input"))

datas += collect_data_files("playwright")

hiddenimports = collect_submodules("app") + collect_submodules("playwright") + ["PIL._tkinter_finder", "certifi"]

a = Analysis(
    [str(ROOT / "main.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "PyQt5",
        "PyQt6",
        "PySide2",
        "PySide6",
        "matplotlib",
        "IPython",
        "jupyter",
        "notebook",
        "pygame",
        "pygame.pkgdata",
        "black",
        "jedi",
        "pytest",
        "numpy",
        "zmq",
        "gi",
    ],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="GMGNWallet",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
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
    upx=False,
    upx_exclude=[],
    name="GMGNWallet",
)
