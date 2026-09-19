from __future__ import annotations

import os
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist" / "GMGNWallet"


def _copy(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)


def _copy_tree(src: Path, dest: Path) -> None:
    if not src.is_dir():
        return
    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, dest, symlinks=False, copy_function=shutil.copy2)
    print(f"  copied {src.name} -> {dest}")


def _find_playwright_browsers() -> Path | None:
    candidates = []
    env_path = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if env_path:
        candidates.append(Path(env_path))
    candidates.append(Path.home() / "AppData" / "Local" / "ms-playwright")
    for path in candidates:
        if path.is_dir() and any(path.glob("chromium-*")):
            return path
    return None


def _copy_browsers() -> None:
    browser_root = _find_playwright_browsers()
    if browser_root is None:
        print("  WARNING: Playwright browsers not found, skipping browser kernel copy")
        print("  Run: playwright install chromium")
        return
    dest_root = DIST / "playwright_browsers"
    dest_root.mkdir(parents=True, exist_ok=True)
    for name in os.listdir(browser_root):
        src = browser_root / name
        if not src.is_dir():
            continue
        if name.startswith("chromium") or name in ("ffmpeg-1011",):
            _copy_tree(src, dest_root / name)
    print(f"  browser kernel -> {dest_root}")


def main() -> None:
    if not DIST.exists():
        raise SystemExit(f"missing dist folder: {DIST}")
    for name in ("input", "output", "logs", "data"):
        (DIST / name).mkdir(parents=True, exist_ok=True)

    env_src = ROOT / ".env" if (ROOT / ".env").exists() else ROOT / ".env.example"
    if env_src.exists():
        _copy(env_src, DIST / ".env")

    wallet_src = ROOT / "地址.txt"
    if wallet_src.exists():
        _copy(wallet_src, DIST / "input" / "地址.txt")
    elif not (DIST / "input" / "地址.txt").exists():
        (DIST / "input" / "地址.txt").write_text("# 每行一个 Solana 钱包地址\n", encoding="utf-8")

    help_src = ROOT / "packaging" / "使用说明.txt"
    if help_src.exists():
        text = help_src.read_text(encoding="utf-8")
        (DIST / "使用说明.txt").write_text(text, encoding="utf-8")
        (DIST / "README.txt").write_text(text, encoding="utf-8")

    for name in ("special_assets.json", "solana_program_registry.json"):
        src = ROOT / "data" / name
        if src.exists():
            _copy(src, DIST / "data" / name)

    print("copying browser kernel...")
    _copy_browsers()

    print(f"portable files ready: {DIST}")


if __name__ == "__main__":
    main()
