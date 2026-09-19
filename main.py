from __future__ import annotations

import multiprocessing
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.gui.main_window import run_app
from app.utils.paths import ensure_runtime_dirs


def main() -> None:
    ensure_runtime_dirs()
    run_app()


if __name__ == "__main__":
    multiprocessing.freeze_support()
    try:
        main()
    except Exception:
        detail = traceback.format_exc()
        try:
            from tkinter import messagebox

            messagebox.showerror("启动失败", detail[-2000:])
        except Exception:
            sys.stderr.write(detail)
        raise
