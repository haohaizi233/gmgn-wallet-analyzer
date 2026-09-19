from __future__ import annotations

import queue
import sys
from tkinter import ttk

import customtkinter as ctk

from app.api.credential_pool import CredentialPool
from app.api.rate_limiter import WeightedRateLimiter
from app.config import AppConfig, load_config
from app.gui.about_page import AboutPage
from app.gui.analysis_page import AnalysisPage
from app.gui.components.log_panel import LogPanel
from app.gui.history_page import HistoryPage
from app.gui.settings_page import SettingsPage
from app.gui.viz_page import VizPage
from app.gui.sidebar import Sidebar
from app.gui.theme import BG, MUTED, PANEL, TEXT, apply_theme, font
from app.gui.ui_scheduler import DRAIN_BUDGET, coalesce_messages
from app.storage.database import Database
from app.storage.repositories import Repositories
from app.utils.logger import attach_queue_handler, get_logger, setup_logging
from app.utils.paths import ENV_PATH, INPUT_DIR, OUTPUT_DIR, ensure_runtime_dirs
from app.version import APP_NAME, __version__

logger = get_logger("gmgn.gui")


class MainWindow(ctk.CTk):
    def __init__(self) -> None:
        apply_theme()
        super().__init__()
        ensure_runtime_dirs()
        setup_logging()
        self.title(f"{APP_NAME} v{__version__}")
        self.geometry("1500x920")
        self.minsize(1200, 760)
        self.configure(fg_color=BG)
        self.config: AppConfig = load_config()
        self.db = Database()
        self.db_repos = Repositories(self.db)
        keys = self.config.api_keys or ([self.config.api_key] if self.config.api_key else [])
        self.credentials = CredentialPool(
            keys,
            rate=self.config.rate,
            capacity=self.config.capacity,
            target_utilization=self.config.target_utilization,
            initial_utilization=getattr(self.config, "initial_utilization", 0.60),
            safety_margin=getattr(self.config, "reset_safety_margin", 3.0),
        )
        self.limiter = self.credentials.all()[0].limiter if self.credentials.all() else WeightedRateLimiter(
            self.config.rate,
            self.config.capacity,
            target_utilization=self.config.target_utilization,
            initial_utilization=getattr(self.config, "initial_utilization", 0.60),
            safety_margin=getattr(self.config, "reset_safety_margin", 3.0),
        )
        self.ui_queue: queue.Queue = queue.Queue()
        self.pages: dict[str, ctk.CTkFrame] = {}
        self._active_page = ""
        self._status_cache: dict[str, str] = {}
        self._limit_text = ""

        root = ctk.CTkFrame(self, fg_color=BG)
        root.pack(fill="both", expand=True)
        self.sidebar = Sidebar(root, self.show_page)
        self.sidebar.pack(side="left", fill="y")
        body = ctk.CTkFrame(root, fg_color=BG)
        body.pack(side="left", fill="both", expand=True)
        self.page_host = ctk.CTkFrame(body, fg_color=BG)
        self.page_host.pack(fill="both", expand=True)
        self.page_host.grid_rowconfigure(0, weight=1)
        self.page_host.grid_columnconfigure(0, weight=1)
        self.log_panel = LogPanel(body)
        self.log_panel.pack(fill="x")
        attach_queue_handler(self._enqueue_log, "gmgn")
        self.status = ctk.CTkFrame(self, height=32, fg_color=PANEL)
        self.status.pack(fill="x")
        self.api_status = ctk.CTkLabel(self.status, text="API 未测试", font=font(12), text_color=MUTED)
        self.api_status.pack(side="left", padx=(12, 8))
        self.job_label = ctk.CTkLabel(self.status, text="任务：等待分析", font=font(12), text_color=MUTED)
        self.job_label.pack(side="left", padx=8)
        self.keys_label = ctk.CTkLabel(self.status, text="Keys：-", font=font(12), text_color=MUTED)
        self.keys_label.pack(side="left", padx=8)
        self.sources_label = ctk.CTkLabel(self.status, text="数据源：等待任务", font=font(12), text_color=MUTED)
        self.sources_label.pack(side="left", padx=8)
        self.plan_label = ctk.CTkLabel(self.status, text=f"套餐：{self.config.plan}", font=font(12), text_color=MUTED)
        self.plan_label.pack(side="left", padx=8)
        self.limit_label = ctk.CTkLabel(self.status, text=f"限额：{int(self.config.rate)}u/s 目标：{int(self.config.target_utilization*100)}%", font=font(12), text_color=MUTED)
        self.limit_label.pack(side="left", padx=8)
        self.db_label = ctk.CTkLabel(self.status, text="数据库：正常", font=font(12), text_color=MUTED)
        self.db_label.pack(side="left", padx=8)
        self.cache_label = ctk.CTkLabel(self.status, text="缓存命中：0%", font=font(12), text_color=MUTED)
        self.cache_label.pack(side="left", padx=8)
        ctk.CTkLabel(self.status, text=f"版本：v{__version__}", font=font(12), text_color=MUTED).pack(side="right", padx=12)

        self.pages["analysis"] = AnalysisPage(self.page_host, self)
        self.pages["history"] = HistoryPage(self.page_host, self)
        self.pages["viz"] = VizPage(self.page_host, self)
        self.pages["settings"] = SettingsPage(self.page_host, self)
        self.pages["about"] = AboutPage(self.page_host)
        for page in self.pages.values():
            page.grid(row=0, column=0, sticky="nsew")
        self.show_page("analysis")
        self.sidebar.highlight("analysis")
        self.after(80, self._poll_queue)
        if getattr(self.config, "enable_moralis", False) and getattr(self.config, "moralis_api_keys", None):
            self.log_panel.append(f"Moralis 主索引已启用，{len(self.config.moralis_api_keys)} 把 Key 轮换。")
        else:
            self.log_panel.append("Moralis 未启用（可选）。分析走 Solana RPC，有 Helius 则用 Helius 节点增强。")
        helius_n = len(getattr(self.config, "helius_api_keys", None) or [])
        if helius_n:
            self.log_panel.append(f"Helius 已配置 {helius_n} 把 Key（可选增强）。")
        else:
            self.log_panel.append("Helius: NOT CONFIGURED (Optional)")
        if not self.config.has_api_key:
            self.log_panel.append("GMGN OpenAPI 未配置。默认采集方式是网页自动化（Cookie 号池）。")
        try:
            from app.browser.cookie_pool import CookiePool

            pool = CookiePool()
            usable = len(pool.usable())
            total = len(pool.all())
            self.log_panel.append(f"GMGN Cookie 号池：{usable}/{total} 可用。")
        except Exception as exc:
            self.log_panel.append(f"GMGN Cookie 号池读取失败：{exc}", "WARNING")
        self.log_panel.append(f"配置文件：{ENV_PATH}")
        self.log_panel.append(f"输入目录：{INPUT_DIR}（默认 地址.txt）")
        self.log_panel.append(f"输出目录：{OUTPUT_DIR}")
        self.log_panel.append("配置已加载，可以开始分析。")

    def show_page(self, key: str) -> None:
        page = self.pages.get(key)
        if page is None:
            return
        same = self._active_page == key
        if not same:
            analysis = self.pages.get("analysis")
            if key != "analysis" and analysis is not None and getattr(analysis, "_results_fullscreen", False):
                analysis._exit_results_fullscreen()
            page.tkraise()
            self._active_page = key
        if key == "history":
            history = self.pages.get("history")
            if history is not None:
                history.refresh_if_dirty()
        elif key == "viz":
            viz = self.pages.get("viz")
            if viz is not None:
                viz.refresh_if_dirty()
        elif key == "analysis" and not same:
            analysis = self.pages.get("analysis")
            pending = getattr(analysis, "_pending_report", None) if analysis is not None else None
            if pending is not None:
                analysis._pending_report = None
                analysis.render_report(pending)

    def active_page(self) -> str:
        return self._active_page

    def reload_config(self) -> None:
        self.config = load_config()
        new_keys = self.config.api_keys or ([self.config.api_key] if self.config.api_key else [])
        current = [c.api_key for c in self.credentials.all()]
        if current != new_keys:
            self.credentials = CredentialPool(
                new_keys,
                rate=self.config.rate,
                capacity=self.config.capacity,
                target_utilization=self.config.target_utilization,
                initial_utilization=getattr(self.config, "initial_utilization", 0.60),
                safety_margin=getattr(self.config, "reset_safety_margin", 3.0),
            )
            if self.credentials.all():
                self.limiter = self.credentials.all()[0].limiter
        else:
            for cred in self.credentials.all():
                cred.limiter.server_rate = self.config.rate
                cred.limiter.capacity = self.config.capacity
                cred.limiter.target_utilization = self.config.target_utilization
                cred.limiter.safety_margin = getattr(self.config, "reset_safety_margin", 3.0)
                if cred.limiter._adaptive_target > cred.limiter.target_utilization:
                    cred.limiter._adaptive_target = cred.limiter.target_utilization
                    cred.limiter.rate = cred.limiter.server_rate * cred.limiter._adaptive_target
        self.plan_label.configure(text=f"套餐：{self.config.plan}")
        self.limit_label.configure(text=f"限额：{int(self.config.rate)}u/s 目标：{int(self.config.target_utilization*100)}%")

    def _set_status(self, attr: str, text: str) -> None:
        if self._status_cache.get(attr) == text:
            return
        self._status_cache[attr] = text
        getattr(self, attr).configure(text=text)

    def set_api_status(self, text: str) -> None:
        self._set_status("api_status", text)
        display = self.limiter.snapshot().display
        if display != self._limit_text:
            self._limit_text = display
            self.limit_label.configure(text=display)

    def set_job_status(self, text: str) -> None:
        self._set_status("job_label", text)

    def set_key_status(self, text: str) -> None:
        compact = (text or "Keys：-").replace("\n", " · ")
        if len(compact) > 48:
            compact = compact[:45] + "..."
        self._set_status("keys_label", compact)

    def set_source_status(self, text: str) -> None:
        compact = (text or "数据源：等待任务").replace("Data Sources：", "数据源：")
        if len(compact) > 42:
            compact = compact[:39] + "..."
        self._set_status("sources_label", compact)

    def set_cache_rate(self, text: str) -> None:
        self._set_status("cache_label", f"缓存命中：{text}")

    def refresh_history(self) -> None:
        page = self.pages.get("history")
        if page:
            page.refresh()

    def mark_history_dirty(self) -> None:
        page = self.pages.get("history")
        if page:
            page.mark_dirty()
            if self._active_page == "history":
                page.refresh_if_dirty()
        viz = self.pages.get("viz")
        if viz:
            viz.mark_dirty()
            if self._active_page == "viz":
                viz.refresh_if_dirty()

    def _enqueue_log(self, formatted: str, level: str) -> None:
        self.ui_queue.put({"type": "log", "text": formatted, "level": level})

    def _poll_queue(self) -> None:
        drained: list[dict] = []
        for _ in range(DRAIN_BUDGET):
            try:
                drained.append(self.ui_queue.get_nowait())
            except queue.Empty:
                break
        if drained:
            batches = coalesce_messages(drained)
            logs = [(str(msg.get("text") or ""), str(msg.get("level") or "INFO")) for msg in batches["logs"]]
            if logs:
                self.log_panel.append_many(logs, raw=True)
            for msg in batches["progress"]:
                self._route_message(msg)
            for msg in batches["critical"]:
                self._route_message(msg)
        first = self.credentials.all()[0] if self.credentials.all() else None
        snapshot = first.limiter.snapshot() if first else self.limiter.snapshot()
        safety_mode = "独立 Key"
        try:
            hot = sum(1 for c in self.credentials.all() if c.count_429)
            if hot >= 2:
                safety_mode = "检测到共享限制"
        except Exception:
            pass
        limit_text = f"限频：{snapshot.display} · {safety_mode}"
        if limit_text != self._limit_text:
            self._limit_text = limit_text
            self.limit_label.configure(text=limit_text)
        self.after(80, self._poll_queue)

    def _route_message(self, msg: dict) -> None:
        job_id = msg.get("job_id")
        page = self.pages.get("analysis")
        if page is not None and job_id and getattr(page, "job_id", None) == job_id:
            page.handle_message(msg)
            return
        target = msg.get("target") or msg.get("page") or "analysis"
        page = self.pages.get(target) or self.pages["analysis"]
        page.handle_message(msg)


def run_app() -> None:
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            try:
                ctypes.windll.user32.SetProcessDPIAware()
            except Exception:
                pass
    app = MainWindow()
    app.mainloop()
