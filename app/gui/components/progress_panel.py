from __future__ import annotations

import customtkinter as ctk

from app.gui.theme import MUTED, PANEL, PRIMARY, TEXT, font

IDLE_PROGRESS = frozenset({"等待开始", "已取消", "分析失败"})


def coalesce_progress(
    message: str,
    done: int,
    total: int,
    sticky: tuple[int, int] | None,
) -> tuple[int, int, tuple[int, int] | None, str, float]:
    """Keep a running bar when noisy browser logs arrive without done/total."""
    done = int(done or 0)
    total = int(total or 0)
    text = message or "分析中"
    if total > 0:
        sticky = (max(0, done), total)
    elif text in IDLE_PROGRESS:
        sticky = None
        done, total = 0, 0
    elif sticky:
        done, total = sticky
    if total > 0:
        return done, total, sticky, f"{done} / {total}", min(1.0, done / total)
    if text in IDLE_PROGRESS:
        return 0, 0, None, text, 0.0
    return 0, 0, sticky, "进行中", 0.12


class ProgressPanel(ctk.CTkFrame):
    def __init__(self, master, **kwargs):
        super().__init__(master, fg_color=PANEL, **kwargs)
        self.stage = ctk.CTkLabel(self, text="等待开始", font=font(13, "bold"), text_color=TEXT, anchor="w")
        self.stage.pack(fill="x", padx=10, pady=(10, 0))
        self.detail = ctk.CTkLabel(self, text="当前钱包：-    当前 Token：-", font=font(12), text_color=MUTED, anchor="w")
        self.detail.pack(fill="x", padx=10, pady=(2, 0))
        self.api_label = ctk.CTkLabel(self, text="API 正常", font=font(12), text_color=PRIMARY, anchor="w")
        self.api_label.pack(fill="x", padx=10, pady=(2, 0))
        self.meta = ctk.CTkLabel(self, text="Keys：-    数据源：等待任务", font=font(11), text_color=MUTED, anchor="w")
        self.meta.pack(fill="x", padx=10, pady=(2, 0))
        self.bar = ctk.CTkProgressBar(self, height=8)
        self.bar.pack(fill="x", padx=10, pady=(8, 12))
        self.bar.set(0)
        self.count = ctk.CTkLabel(self, text="等待开始", font=font(12), text_color=MUTED, anchor="e")
        self.count.pack(fill="x", padx=10, pady=(0, 10))
        self._keys = "Keys：-"
        self._sources = "数据源：等待任务"
        self._last_progress: tuple | None = None
        self._last_api = "API 正常"
        self._sticky: tuple[int, int] | None = None

    def update_progress(self, message: str, done: int, total: int, wallet: str = "", token: str = "", api: str = "") -> None:
        done, total, self._sticky, count, fraction = coalesce_progress(message, done, total, self._sticky)
        key = (message or "分析中", done, total, wallet or "-", token or "-", api or "", count)
        if key == self._last_progress:
            return
        self._last_progress = key
        self.stage.configure(text=key[0])
        self.detail.configure(text=f"当前钱包：{key[3]}    当前 Token：{key[4]}")
        if api and api != self._last_api:
            self.api_label.configure(text=api)
            self._last_api = api
        if self.count.cget("text") != count:
            self.count.configure(text=count)
        self.bar.set(fraction)

    def set_api(self, text: str) -> None:
        text = text or "API 正常"
        if text == self._last_api:
            return
        self._last_api = text
        self.api_label.configure(text=text)

    def set_keys(self, text: str) -> None:
        text = text or "Keys：-"
        if text == self._keys:
            return
        self._keys = text
        self._refresh_meta()

    def set_sources(self, text: str) -> None:
        text = text or "数据源：等待任务"
        if text == self._sources:
            return
        self._sources = text
        self._refresh_meta()

    def _refresh_meta(self) -> None:
        self.meta.configure(text=f"{self._keys}    {self._sources}")

    def reset(self) -> None:
        self._last_progress = None
        self._sticky = None
        self.update_progress("等待开始", 0, 0)
        self.set_keys("Keys：-")
        self.set_sources("数据源：等待任务")
        self.set_api("API 正常")
        self.bar.set(0)
