from __future__ import annotations

from datetime import datetime
from tkinter import filedialog

import customtkinter as ctk

from app.gui.theme import MUTED, PANEL, TEXT, font
from app.utils.paths import LOG_DIR

LEVEL_COLORS = {
    "DEBUG": "#6B7280",
    "INFO": "#0F766E",
    "WARNING": "#C2410C",
    "ERROR": "#B91C1C",
    "CRITICAL": "#7F1D1D",
    "SUCCESS": "#15803D",
    "RATE_LIMIT": "#1D4ED8",
}


class LogPanel(ctk.CTkFrame):
    def __init__(self, master, max_lines: int = 1000, **kwargs):
        super().__init__(master, fg_color=PANEL, **kwargs)
        self.max_lines = max_lines
        self.auto_scroll = ctk.BooleanVar(value=True)
        bar = ctk.CTkFrame(self, fg_color="transparent")
        bar.pack(fill="x", padx=8, pady=(6, 0))
        ctk.CTkLabel(bar, text="运行日志", font=font(13, "bold"), text_color=TEXT).pack(side="left")
        ctk.CTkCheckBox(bar, text="自动滚动", variable=self.auto_scroll, font=font(12)).pack(side="right", padx=6)
        ctk.CTkButton(bar, text="导出日志", width=80, height=26, command=self.export).pack(side="right", padx=6)
        ctk.CTkButton(bar, text="清空", width=60, height=26, fg_color="#E5E7EB", text_color=TEXT, command=self.clear).pack(side="right")
        self.text = ctk.CTkTextbox(self, height=200, font=font(12), text_color=TEXT)
        self.text.pack(fill="both", expand=True, padx=8, pady=8)
        self.text.configure(state="disabled")
        for level, color in LEVEL_COLORS.items():
            try:
                self.text.tag_config(level, foreground=color)
            except Exception:
                pass

    def append(self, message: str, level: str = "INFO", *, raw: bool = False) -> None:
        self.append_many([(message, level)], raw=raw)

    def append_many(self, items: list[tuple[str, str]], *, raw: bool = False) -> None:
        if not items:
            return
        box = getattr(self.text, "_textbox", self.text)
        self.text.configure(state="normal")
        for message, level in items:
            text = (message or "").rstrip("\n")
            if "RATE_LIMIT" in text.upper():
                level = "RATE_LIMIT"
            if raw or (text.startswith("[") and "] " in text[:16]):
                line = text + "\n"
            else:
                stamp = datetime.now().strftime("%H:%M:%S")
                line = f"[{stamp}] {level} {text}\n"
            tag = level if level in LEVEL_COLORS else "INFO"
            try:
                box.insert("end", line, tag)
            except Exception:
                box.insert("end", line)
        current = int(self.text.index("end-1c").split(".")[0])
        if current > self.max_lines:
            self.text.delete("1.0", f"{current - self.max_lines}.0")
        if self.auto_scroll.get():
            self.text.see("end")
        self.text.configure(state="disabled")

    def clear(self) -> None:
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.configure(state="disabled")

    def export(self) -> None:
        path = filedialog.asksaveasfilename(initialdir=str(LOG_DIR), defaultextension=".log", filetypes=[("Log", "*.log"), ("Text", "*.txt")])
        if not path:
            return
        content = self.text.get("1.0", "end")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(content)
