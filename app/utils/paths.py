from __future__ import annotations

import os
import sys
from pathlib import Path


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def _app_dir() -> Path:
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


def _resource_dir() -> Path:
    if is_frozen():
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            return Path(meipass)
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


APP_DIR = _app_dir()
RESOURCE_DIR = _resource_dir()
PROJECT_ROOT = APP_DIR
INPUT_DIR = APP_DIR / "input"
DATA_DIR = APP_DIR / "data"
RAW_DIR = DATA_DIR / "raw"
COOKIE_DIR = DATA_DIR / "cookies"
LOG_DIR = APP_DIR / "logs"
OUTPUT_DIR = APP_DIR / "output"
REPORT_DIR = OUTPUT_DIR / "reports"
DB_PATH = DATA_DIR / "gmgn_wallet.db"
ENV_PATH = APP_DIR / ".env"
ENV_EXAMPLE_PATH = RESOURCE_DIR / ".env.example"
WALLET_FILENAMES = ("地址.txt", "wallets.txt")
DEFAULT_WALLET_NAME = "地址.txt"


def data_file(name: str) -> Path:
    override = DATA_DIR / name
    if override.exists():
        return override
    bundled = RESOURCE_DIR / "data" / name
    if bundled.exists():
        return bundled
    return override


def default_wallet_file() -> Path:
    for name in WALLET_FILENAMES:
        candidate = INPUT_DIR / name
        if candidate.exists():
            return candidate
    for name in WALLET_FILENAMES:
        candidate = APP_DIR / name
        if candidate.exists():
            return candidate
    return INPUT_DIR / DEFAULT_WALLET_NAME


def read_wallet_file() -> str:
    path = default_wallet_file()
    if not path.exists():
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def write_wallet_file(text: str, path: Path | None = None) -> Path:
    target = path or (INPUT_DIR / DEFAULT_WALLET_NAME)
    target.parent.mkdir(parents=True, exist_ok=True)
    body = text.replace("\r\n", "\n").strip()
    target.write_text((body + "\n") if body else "", encoding="utf-8")
    return target


def open_in_explorer(path: Path) -> None:
    path = Path(path)
    if path.suffix and path.exists() and path.is_file():
        os.startfile(str(path))  # type: ignore[attr-defined]
        return
    path.mkdir(parents=True, exist_ok=True)
    os.startfile(str(path))  # type: ignore[attr-defined]


def _copy_if_needed(src: Path, dest: Path) -> None:
    if not src.exists() or src.resolve() == dest.resolve():
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(src.read_bytes())


def _seed_env_file() -> None:
    if ENV_PATH.exists():
        return
    for candidate in (
        RESOURCE_DIR / "defaults" / ".env",
        RESOURCE_DIR / ".env",
        ENV_EXAMPLE_PATH,
    ):
        if candidate.exists():
            _copy_if_needed(candidate, ENV_PATH)
            return
    ENV_PATH.write_text(
        "HELIUS_API_KEY=\nGMGN_API_KEY=\nENABLE_MORALIS=false\n",
        encoding="utf-8",
    )


def _seed_wallet_file() -> None:
    dest = INPUT_DIR / DEFAULT_WALLET_NAME
    if dest.exists():
        return
    for candidate in (
        APP_DIR / DEFAULT_WALLET_NAME,
        RESOURCE_DIR / "input" / DEFAULT_WALLET_NAME,
        RESOURCE_DIR / DEFAULT_WALLET_NAME,
    ):
        if candidate.exists() and candidate.resolve() != dest.resolve():
            _copy_if_needed(candidate, dest)
            return
    dest.write_text("# 每行一个 Solana 钱包地址\n", encoding="utf-8")


def _materialize_bundled_data() -> None:
    for name in ("special_assets.json", "solana_program_registry.json"):
        dest = DATA_DIR / name
        if dest.exists():
            continue
        src = RESOURCE_DIR / "data" / name
        _copy_if_needed(src, dest)


def ensure_runtime_dirs() -> None:
    for path in (INPUT_DIR, DATA_DIR, RAW_DIR, COOKIE_DIR, LOG_DIR, OUTPUT_DIR, REPORT_DIR):
        path.mkdir(parents=True, exist_ok=True)
    _seed_env_file()
    _seed_wallet_file()
    _materialize_bundled_data()
