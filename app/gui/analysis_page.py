from __future__ import annotations

import time
import queue
import threading
from datetime import datetime, timedelta
from tkinter import TclError, filedialog, messagebox
from typing import Optional

import customtkinter as ctk

from app.domain.enums import EventType, ReportPeriod, TaskStatus
from app.domain.models import AnalysisOptions, WalletAnalysisRequest, WalletReport
from app.gui.components.data_table import DataTable
from app.gui.components.option_drawer import OptionDrawer
from app.gui.components.progress_panel import ProgressPanel
from app.gui.components.stat_card import StatCard
from app.gui.theme import BG, DANGER, MUTED, PANEL, PRIMARY, TEXT, font
from app.gui.worker import AnalysisWorker
from app.services.completeness_service import CompletenessService
from app.utils.paths import INPUT_DIR, OUTPUT_DIR, default_wallet_file, open_in_explorer, read_wallet_file, write_wallet_file
from app.utils.time_utils import LOCAL_TZ, format_datetime, format_elapsed, period_window
from app.utils.validators import parse_wallet_lines, short_address, summarize_wallet_input

TOKEN_COLUMNS = [
    ("idx", "#", 40),
    ("symbol", "币种", 80),
    ("address", "代币合约", 120),
    ("platform", "来源平台", 100),
    ("acq", "获得方式", 90),
    ("mcap", "入场市值", 110),
    ("first_buy", "首笔买入", 110),
    ("amount", "首买数量", 100),
    ("buy_time", "买入时间", 140),
    ("created", "创建时间", 140),
    ("diff", "时差", 110),
    ("hold", "持仓时长", 100),
    ("buys", "买入笔数", 70),
    ("buy_usd", "买入总额", 110),
    ("sells", "卖出笔数", 70),
    ("sell_usd", "卖出总额", 110),
    ("pnl", "总盈亏%", 90),
    ("status", "状态", 80),
]
TRADE_COLUMNS = [
    ("time", "时间", 140),
    ("type", "类型", 90),
    ("symbol", "币种", 80),
    ("address", "代币合约", 120),
    ("amount", "数量", 90),
    ("usd", "USD金额", 110),
    ("sol", "SOL金额", 90),
    ("price", "价格USD", 100),
    ("gas_usd", "Gas USD", 90),
    ("gas_sol", "Gas SOL", 80),
    ("pnl", "单笔盈亏", 120),
    ("platform", "来源平台", 100),
    ("wallet", "钱包", 120),
    ("tx", "TxHash", 140),
]
WARN_COLUMNS = [
    ("wallet", "钱包", 120),
    ("token", "Token", 80),
    ("field", "字段", 90),
    ("value", "最终值", 140),
    ("status", "状态", 110),
    ("reason", "原因", 220),
    ("source", "数据来源", 140),
    ("est", "是否估算", 70),
]


class AnalysisPage(ctk.CTkFrame):
    def __init__(self, master, app, **kwargs):
        super().__init__(master, fg_color=BG, **kwargs)
        self.app = app
        self.worker: Optional[AnalysisWorker] = None
        self.cancel_event = threading.Event()
        self.reports: list[WalletReport] = []
        self.current: Optional[WalletReport] = None
        self.period = ReportPeriod.D1
        self.job_id: Optional[str] = None
        self.page_key = "analysis"

        left = ctk.CTkFrame(self, width=320, fg_color=PANEL, corner_radius=0)
        left.pack(side="left", fill="y")
        left.pack_propagate(False)
        form = ctk.CTkScrollableFrame(left, fg_color=PANEL, corner_radius=0)
        form.pack(fill="both", expand=True)
        right = ctk.CTkFrame(self, fg_color=BG)
        right.pack(side="left", fill="both", expand=True)
        self._left_panel = left
        self._right_panel = right

        ctk.CTkLabel(form, text="钱包地址", font=font(14, "bold"), text_color=TEXT, anchor="w").pack(fill="x", padx=16, pady=(16, 6))
        self.wallet_text = ctk.CTkTextbox(form, height=110, font=font(13))
        self.wallet_text.pack(fill="x", padx=16)
        self.wallet_text.bind("<KeyRelease>", lambda _e: self._refresh_wallet_stats())
        self.wallet_error = ctk.CTkLabel(form, text="", text_color=DANGER, font=font(11), anchor="w")
        self.wallet_error.pack(fill="x", padx=16)
        self.wallet_count = ctk.CTkLabel(form, text="有效钱包：0  重复：0  无效：0", font=font(11), text_color=MUTED, anchor="w")
        self.wallet_count.pack(fill="x", padx=16)
        self.mode_label = ctk.CTkLabel(form, text="模式：单钱包", font=font(11), text_color=MUTED, anchor="w")
        self.mode_label.pack(fill="x", padx=16)
        btns = ctk.CTkFrame(form, fg_color="transparent")
        btns.pack(fill="x", padx=16, pady=6)
        ctk.CTkButton(btns, text="从文件导入", width=90, height=28, command=self._import_file).pack(side="left")
        ctk.CTkButton(btns, text="重新加载", width=80, height=28, fg_color="#E5E7EB", text_color=TEXT, command=self._reload_wallets).pack(side="left", padx=6)
        ctk.CTkButton(btns, text="保存", width=60, height=28, fg_color="#E5E7EB", text_color=TEXT, command=self._save_wallets).pack(side="left", padx=6)
        ctk.CTkButton(btns, text="清空", width=60, height=28, fg_color="#E5E7EB", text_color=TEXT, command=lambda: self.wallet_text.delete("1.0", "end")).pack(side="left")
        dirs = ctk.CTkFrame(form, fg_color="transparent")
        dirs.pack(fill="x", padx=16)
        ctk.CTkButton(dirs, text="打开输入目录", width=100, height=28, fg_color="#E5E7EB", text_color=TEXT, command=lambda: open_in_explorer(INPUT_DIR)).pack(side="left")
        ctk.CTkButton(dirs, text="打开输出目录", width=100, height=28, fg_color="#E5E7EB", text_color=TEXT, command=lambda: open_in_explorer(OUTPUT_DIR)).pack(side="left", padx=6)
        self._reload_wallets(silent=True)

        ctk.CTkLabel(form, text="时间范围", font=font(14, "bold"), text_color=TEXT, anchor="w").pack(fill="x", padx=16, pady=(10, 6))
        period_bar = ctk.CTkFrame(form, fg_color="transparent")
        period_bar.pack(fill="x", padx=16)
        self.period_buttons = {}
        for key, label in [(ReportPeriod.D1, "最近 1 天"), (ReportPeriod.D7, "最近 7 天"), (ReportPeriod.D30, "最近 30 天")]:
            btn = ctk.CTkButton(period_bar, text=label, height=28, command=lambda k=key: self._set_period(k))
            btn.pack(side="left", expand=True, fill="x", padx=2)
            self.period_buttons[key] = btn
        date_row = ctk.CTkFrame(form, fg_color="transparent")
        date_row.pack(fill="x", padx=16, pady=6)
        self.start_var = ctk.StringVar()
        self.end_var = ctk.StringVar()
        self._suppress_manual_date = False
        ctk.CTkLabel(date_row, text="开始", font=font(12), text_color=MUTED).grid(row=0, column=0, sticky="w")
        ctk.CTkEntry(date_row, textvariable=self.start_var, width=130).grid(row=1, column=0, padx=(0, 8))
        ctk.CTkLabel(date_row, text="结束", font=font(12), text_color=MUTED).grid(row=0, column=1, sticky="w")
        ctk.CTkEntry(date_row, textvariable=self.end_var, width=130).grid(row=1, column=1)
        self.period_hint = ctk.CTkLabel(form, text="", font=font(11), text_color=MUTED, anchor="w")
        self.period_hint.pack(fill="x", padx=16, pady=(0, 4))
        self.start_var.trace_add("write", lambda *_a: self._on_manual_date())
        self.end_var.trace_add("write", lambda *_a: self._on_manual_date())
        self._set_period(ReportPeriod.D1)

        self.opt_created = ctk.BooleanVar(value=True)
        self.opt_platform = ctk.BooleanVar(value=True)
        self.opt_transfer = ctk.BooleanVar(value=True)
        self.opt_fill = ctk.BooleanVar(value=True)
        self.opt_raw = ctk.BooleanVar(value=True)
        self.opt_pump = ctk.BooleanVar(value=False)
        mode_default = str(getattr(app.config, "collection_mode", "automation") or "automation")
        self.opt_collect = ctk.StringVar(value="自动化" if mode_default != "api" else "API")
        self.opt_headed = ctk.BooleanVar(value=bool(getattr(app.config, "browser_headed", True)))
        self.max_tx_var = ctk.StringVar(value="2500")
        self.adv_drawer = OptionDrawer(form, title="高级选项", summary="自动化 · 有头 · 5 项开启 · 上限 2500")
        self.adv_drawer.trigger.configure(command=self._toggle_advanced)
        self.adv_drawer.trigger.pack(fill="x", padx=16, pady=(10, 0))
        self._adv_built = False
        for var in (
            self.opt_created,
            self.opt_platform,
            self.opt_transfer,
            self.opt_fill,
            self.opt_raw,
            self.opt_pump,
            self.opt_headed,
        ):
            var.trace_add("write", lambda *_a: self._refresh_adv_summary())
        self.max_tx_var.trace_add("write", lambda *_a: self._refresh_adv_summary())
        self.opt_collect.trace_add("write", lambda *_a: self._refresh_adv_summary())
        self._refresh_adv_summary()

        start_bar = ctk.CTkFrame(form, fg_color="transparent")
        start_bar.pack(fill="x", padx=16, pady=12)
        self.start_btn = ctk.CTkButton(start_bar, text="开始分析", height=36, command=self.start_analysis)
        self.start_btn.pack(side="left", expand=True, fill="x")
        self.stop_btn = ctk.CTkButton(start_bar, text="停止", height=36, fg_color=DANGER, width=80, command=self.stop_analysis, state="disabled")
        self.stop_btn.pack(side="left", padx=(8, 0))
        self.progress = ProgressPanel(form)
        self.progress.pack(fill="x", padx=12, pady=(0, 12))

        cards = ctk.CTkFrame(right, fg_color="transparent")
        self._card_row = cards
        cards.pack(fill="x", padx=12, pady=(12, 0))
        self.cards = {}
        for key, title in [
            ("tokens", "代币总数"),
            ("buysell", "买入 / 卖出"),
            ("realized", "GMGN 已实现盈亏"),
            ("total", "GMGN 总盈亏"),
            ("open", "当前持仓数"),
            ("gas", "Gas 总费用"),
            ("special", "特殊获得资产数"),
        ]:
            card = StatCard(cards, title)
            card.pack(side="left", expand=True, fill="x", padx=4)
            self.cards[key] = card

        self.wallet_table = DataTable(
            right,
            [
                ("wallet", "钱包", 110),
                ("state", "状态", 90),
                ("tokens", "Token", 60),
                ("done", "已完成", 60),
                ("stage", "当前阶段", 90),
                ("key", "使用Key", 80),
                ("reqs", "API请求", 70),
                ("rl", "429", 50),
                ("cache", "缓存", 60),
                ("elapsed", "耗时", 70),
            ],
            allow_fullscreen=False,
        )
        self.wallet_table.pack(fill="x", padx=12, pady=(8, 0))
        self.wallet_table.tree.configure(height=6)
        self.wallet_table.tree.bind("<Double-1>", lambda _e: self._select_wallet_row())

        tabs = ctk.CTkTabview(right)
        tabs.pack(fill="both", expand=True, padx=12, pady=12)
        self._tabs = tabs
        self.token_tab = tabs.add("代币分析")
        self.trade_tab = tabs.add("交易明细")
        self.pos_tab = tabs.add("持仓分析")
        self.pnl_tab = tabs.add("盈亏统计")
        self.plat_tab = tabs.add("平台分布")
        self.warn_tab = tabs.add("异常与补全")
        self.log_tab = tabs.add("运行日志")
        self.raw_tab = tabs.add("原始数据")

        self.token_table = DataTable(self.token_tab, TOKEN_COLUMNS, on_fullscreen=self.toggle_results_fullscreen)
        self.token_table.pack(fill="both", expand=True)
        self.token_table.set_filters(["全部", "有盈利", "亏损", "特殊获得", "非 Launchpad", "仍持仓", "已清仓"])
        self.trade_table = DataTable(self.trade_tab, TRADE_COLUMNS, on_fullscreen=self.toggle_results_fullscreen)
        self.trade_table.pack(fill="both", expand=True)
        self.trade_table.set_filters(["全部", "buy", "sell", "transferIn", "transferOut"])
        self.pos_table = DataTable(self.pos_tab, TOKEN_COLUMNS, on_fullscreen=self.toggle_results_fullscreen)
        self.pos_table.pack(fill="both", expand=True)
        self.pnl_table = DataTable(
            self.pnl_tab,
            [("symbol", "币种", 90), ("address", "代币合约", 140), ("fifo", "本地FIFO已实现", 140), ("gmgn", "GMGN说明", 280), ("status", "状态", 90)],
            on_fullscreen=self.toggle_results_fullscreen,
        )
        self.pnl_table.pack(fill="both", expand=True)
        self.plat_table = DataTable(self.plat_tab, [("platform", "来源平台", 160), ("count", "Token数", 80), ("buys", "买入笔数", 90), ("sells", "卖出笔数", 90)], on_fullscreen=self.toggle_results_fullscreen)
        self.plat_table.pack(fill="both", expand=True)
        self.warn_table = DataTable(self.warn_tab, WARN_COLUMNS, on_fullscreen=self.toggle_results_fullscreen)
        self.warn_table.pack(fill="both", expand=True)
        self.run_log = ctk.CTkTextbox(self.log_tab, font=font(12))
        self.run_log.pack(fill="both", expand=True, padx=8, pady=8)
        self.raw_box = ctk.CTkTextbox(self.raw_tab, font=font(12))
        self.raw_box.pack(fill="both", expand=True, padx=8, pady=8)
        self._wallet_rows: dict[str, dict] = {}
        self._pending_report: Optional[WalletReport] = None
        self._last_keys_ts = 0.0
        self._complete = CompletenessService()
        self._results_fullscreen = False
        self._fs_pack: list[tuple] = []
        self._fs_esc: str | None = None
        self._refresh_keys(force=True)

    def _refresh_wallet_stats(self) -> None:
        stats = summarize_wallet_input(self.wallet_text.get("1.0", "end"))
        self.wallet_count.configure(text=f"有效钱包：{stats['valid']}  重复：{stats['duplicate']}  无效：{stats['invalid']}")
        self.mode_label.configure(text="模式：批量任务" if stats["valid"] > 1 else "模式：单钱包")

    def _detail_tables(self) -> list[DataTable]:
        return [
            self.token_table,
            self.trade_table,
            self.pos_table,
            self.pnl_table,
            self.plat_table,
            self.warn_table,
        ]

    def toggle_results_fullscreen(self) -> None:
        if self._results_fullscreen:
            self._exit_results_fullscreen()
        else:
            self._enter_results_fullscreen()

    def _hide_packed(self, widget) -> None:
        try:
            info = dict(widget.pack_info())
        except TclError:
            return
        slaves = list(widget.master.pack_slaves())
        try:
            idx = slaves.index(widget)
        except ValueError:
            idx = -1
        before = slaves[idx + 1] if 0 <= idx < len(slaves) - 1 else None
        widget.pack_forget()
        self._fs_pack.append((widget, info, before))

    def _enter_results_fullscreen(self) -> None:
        if self._results_fullscreen:
            return
        self.adv_drawer.close()
        self._fs_pack = []
        app = self.app
        for widget in (
            self._card_row,
            self.wallet_table,
            self._left_panel,
            getattr(app, "log_panel", None),
            getattr(app, "status", None),
            getattr(app, "sidebar", None),
        ):
            if widget is not None:
                self._hide_packed(widget)
        self._results_fullscreen = True
        for table in self._detail_tables():
            table.set_fullscreen_active(True)
        top = self.winfo_toplevel()
        self._fs_esc = top.bind("<Escape>", self._on_results_fs_escape, add="+")

    def _exit_results_fullscreen(self) -> None:
        if not self._results_fullscreen:
            return
        top = self.winfo_toplevel()
        if self._fs_esc:
            try:
                top.unbind("<Escape>", self._fs_esc)
            except TclError:
                pass
            self._fs_esc = None
        for widget, info, before in reversed(self._fs_pack):
            opts = {k: v for k, v in info.items() if k != "in"}
            if before is not None:
                try:
                    if str(before.winfo_manager()) == "pack":
                        opts["before"] = before
                except TclError:
                    pass
            try:
                widget.pack(**opts)
            except TclError:
                widget.pack(**{k: v for k, v in opts.items() if k != "before"})
        self._fs_pack = []
        self._results_fullscreen = False
        for table in self._detail_tables():
            table.set_fullscreen_active(False)

    def _on_results_fs_escape(self, _event=None):
        if self._results_fullscreen:
            self._exit_results_fullscreen()
            return "break"
        return None

    def _toggle_advanced(self) -> None:
        if self.adv_drawer.is_open():
            self.adv_drawer.close()
            return
        if not self._adv_built:
            self.adv_drawer._ensure_popup()
            self._build_advanced_drawer()
            self._adv_built = True
        self.adv_drawer.open()

    def _build_advanced_drawer(self) -> None:
        body = self.adv_drawer.body
        assert body is not None
        ctk.CTkLabel(body, text="采集方式", font=font(12), text_color=MUTED).pack(anchor="w")
        ctk.CTkSegmentedButton(body, values=["自动化", "API"], variable=self.opt_collect, width=220).pack(anchor="w", pady=(0, 6))
        ctk.CTkLabel(
            body,
            text="自动化走 gmgn.ai Cookie 号池（失效自动扫码）。API 走 OpenAPI Key。",
            font=font(11),
            text_color=MUTED,
            wraplength=260,
            justify="left",
        ).pack(anchor="w", pady=(0, 6))
        ctk.CTkCheckBox(body, text="浏览器有头模式（扫码时强制有头）", variable=self.opt_headed, font=font(12)).pack(anchor="w", pady=(0, 8))
        row = ctk.CTkFrame(body, fg_color="transparent")
        row.pack(fill="x", pady=(0, 6))
        ctk.CTkLabel(row, text="每钱包交易上限", font=font(12), text_color=MUTED).pack(side="left")
        self.max_tx = ctk.CTkEntry(row, width=80, height=28, textvariable=self.max_tx_var)
        self.max_tx.pack(side="right")
        for var, label in [
            (self.opt_created, "获取 Token 创建时间"),
            (self.opt_platform, "获取来源平台/池子"),
            (self.opt_transfer, "识别 Transfer / Bridge"),
            (self.opt_fill, "自动补齐缺失字段"),
            (self.opt_raw, "保存原始 API JSON"),
            (self.opt_pump, "仅分析 Pump.fun Token"),
        ]:
            ctk.CTkCheckBox(body, text=label, variable=var, font=font(12)).pack(anchor="w", pady=2)
        self._refresh_adv_summary()

    def _refresh_adv_summary(self) -> None:
        enabled = sum(
            1
            for var in (
                self.opt_created,
                self.opt_platform,
                self.opt_transfer,
                self.opt_fill,
                self.opt_raw,
                self.opt_pump,
            )
            if var.get()
        )
        limit = (self.max_tx_var.get() or "2500").strip() or "2500"
        collect = self.opt_collect.get() or "自动化"
        headed = "有头" if self.opt_headed.get() else "无头"
        self.adv_drawer.set_summary(f"{collect} · {headed} · {enabled} 项开启 · 上限 {limit}")

    def _persist_collection_settings(self, mode: str, headed: bool) -> None:
        try:
            from dotenv import set_key

            from app.utils.paths import ENV_PATH, ensure_runtime_dirs

            ensure_runtime_dirs()
            set_key(str(ENV_PATH), "GMGN_COLLECTION_MODE", mode, quote_mode="never")
            set_key(str(ENV_PATH), "GMGN_BROWSER_HEADED", "true" if headed else "false", quote_mode="never")
            self.app.config.collection_mode = mode
            self.app.config.browser_headed = headed
        except Exception:
            pass

    def _refresh_keys(self, *, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last_keys_ts < 1.0:
            return
        self._last_keys_ts = now
        try:
            items = self.app.credentials.snapshot()
        except Exception:
            items = []
        if not items:
            self.progress.set_keys("Keys：未配置")
            self.app.set_key_status("Keys：未配置")
            return
        healthy = sum(1 for i in items if i["state"] == "HEALTHY")
        cooldown = sum(1 for i in items if i["state"] in ("RATE_LIMITED", "COOLDOWN"))
        summary = f"Keys：{healthy} Healthy / {cooldown} Cooldown"
        self.progress.set_keys(summary)
        parts = [summary]
        for item in items:
            extra = f"{int((item.get('utilization') or 0)*100)}%"
            if item["state"] in ("RATE_LIMITED", "COOLDOWN"):
                extra = "冷却中"
            parts.append(f"{item['masked']} {item['state']} {extra}")
        self.app.set_key_status(" · ".join(parts))

    def _append_run_log(self, text: str) -> None:
        try:
            self.run_log.insert("end", text + "\n")
            self.run_log.see("end")
        except Exception:
            pass

    def _select_wallet_row(self) -> None:
        sel = self.wallet_table.tree.selection()
        if not sel:
            return
        row = self.wallet_table.row_by_id(sel[0])
        wallet = (row or {}).get("_copy") or (row or {}).get("_id")
        if not wallet:
            values = self.wallet_table.tree.item(sel[0], "values")
            wallet = values[0] if values else ""
        for report in self.reports:
            if report.request.wallet_address == wallet or short_address(report.request.wallet_address) == wallet:
                self.render_report(report)
                return

    def _set_period(self, period: ReportPeriod) -> None:
        self.period = period
        for key, btn in self.period_buttons.items():
            btn.configure(fg_color=PRIMARY if key == period else "#E5E7EB", text_color="#FFFFFF" if key == period else TEXT)
        now = datetime.now(tz=LOCAL_TZ)
        days = {ReportPeriod.D1: 1, ReportPeriod.D7: 7, ReportPeriod.D30: 30}.get(period, 7)
        start = now - timedelta(days=days)
        self._suppress_manual_date = True
        self.start_var.set(start.strftime("%Y-%m-%d"))
        self.end_var.set(now.strftime("%Y-%m-%d"))
        self._suppress_manual_date = False
        hints = {
            ReportPeriod.D1: "按当前时刻往前滚动 24 小时，不含 24 小时以前的成交",
            ReportPeriod.D7: "按当前时刻往前滚动 7 天（7×24 小时）",
            ReportPeriod.D30: "按当前时刻往前滚动 30 天",
        }
        if getattr(self, "period_hint", None) is not None:
            self.period_hint.configure(text=hints.get(period, "自定义日期按开始日 00:00 到结束日 23:59:59"))

    def _on_manual_date(self) -> None:
        if self._suppress_manual_date:
            return
        if self.period == ReportPeriod.CUSTOM:
            return
        self.period = ReportPeriod.CUSTOM
        for btn in self.period_buttons.values():
            btn.configure(fg_color="#E5E7EB", text_color=TEXT)
        if getattr(self, "period_hint", None) is not None:
            self.period_hint.configure(text="自定义日期按开始日 00:00 到结束日 23:59:59")

    def _set_wallet_text(self, text: str) -> None:
        self.wallet_text.delete("1.0", "end")
        if text:
            self.wallet_text.insert("1.0", text.rstrip() + "\n")
        self._refresh_wallet_stats()

    def _reload_wallets(self, silent: bool = False) -> None:
        text = read_wallet_file()
        self._set_wallet_text(text)
        if silent:
            return
        path = default_wallet_file()
        if path.exists():
            messagebox.showinfo("已加载", str(path))
        else:
            messagebox.showwarning("未找到地址文件", f"请把钱包地址放到：\n{path}")

    def _save_wallets(self) -> None:
        path = write_wallet_file(self.wallet_text.get("1.0", "end"))
        messagebox.showinfo("已保存", str(path))

    def _import_file(self) -> None:
        path = filedialog.askopenfilename(initialdir=str(INPUT_DIR), filetypes=[("Text", "*.txt"), ("All", "*.*")])
        if not path:
            return
        text = open(path, encoding="utf-8").read()
        self._set_wallet_text(text)

    def start_analysis(self) -> None:
        self.adv_drawer.close()
        self._refresh_wallet_stats()
        wallets, errors = parse_wallet_lines(self.wallet_text.get("1.0", "end"))
        stats = summarize_wallet_input(self.wallet_text.get("1.0", "end"))
        if errors:
            self.wallet_error.configure(text="；".join(f"{e.address}: {e.reason}" for e in errors[:3]))
        else:
            self.wallet_error.configure(text="")
        if not wallets:
            messagebox.showerror("无法开始", "请输入有效 Solana 钱包地址")
            return
        moralis_ready = bool(getattr(self.app.config, "enable_moralis", False) and getattr(self.app.config, "moralis_api_key", ""))
        collect_mode = "automation" if (self.opt_collect.get() or "自动化") != "API" else "api"
        if collect_mode == "automation":
            self.app.log_panel.append("采集方式：GMGN 网页自动化。Cookie 失效时会弹出有头窗口扫码。")
        elif not moralis_ready and not getattr(self.app.config, "helius_api_key", "") and not self.app.config.has_api_key:
            self.app.log_panel.append("未配置 Moralis / Helius / GMGN。将使用 Solana 公共 RPC + DEX Screener。", "WARNING")
        if not getattr(self.app.config, "helius_api_key", ""):
            self.app.log_panel.append("Helius: NOT CONFIGURED (Optional)", "INFO")
        try:
            if self.period == ReportPeriod.CUSTOM:
                start = datetime.strptime(self.start_var.get().strip(), "%Y-%m-%d").replace(tzinfo=LOCAL_TZ)
                end = datetime.strptime(self.end_var.get().strip() + " 23:59:59", "%Y-%m-%d %H:%M:%S").replace(tzinfo=LOCAL_TZ)
                start_ts, end_ts = period_window(self.period, start, end)
            else:
                start_ts, end_ts = period_window(self.period)
        except ValueError:
            messagebox.showerror("日期错误", "开始/结束日期格式应为 YYYY-MM-DD")
            return
        if self.period == ReportPeriod.D1:
            window_desc = "最近 24 小时（滚动）"
        elif self.period == ReportPeriod.CUSTOM:
            window_desc = "自定义自然日"
        else:
            window_desc = f"滚动 {self.period.value}"
        self.app.log_panel.append(
            f"报告窗口：{window_desc} {format_datetime(start_ts)} → {format_datetime(end_ts)}"
        )
        options = AnalysisOptions(
            fetch_token_created_at=self.opt_created.get(),
            fetch_platform_pool=self.opt_platform.get(),
            detect_transfer_bridge=self.opt_transfer.get(),
            autofill_missing=self.opt_fill.get(),
            save_raw_json=self.opt_raw.get(),
            pumpfun_only=self.opt_pump.get(),
            collection_mode=collect_mode,
            browser_headed=bool(self.opt_headed.get()),
        )
        self._persist_collection_settings(collect_mode, bool(self.opt_headed.get()))
        max_tx = int(self.max_tx_var.get() or 2500)
        requests = [
            WalletAnalysisRequest(
                wallet_address=w,
                chain="sol",
                period=self.period,
                start_time=start_ts,
                end_time=end_ts,
                max_transactions=max_tx,
                options=options,
            )
            for w in wallets
        ]
        self.cancel_event = threading.Event()
        page_key = getattr(self, "page_key", "analysis")
        self.worker = AnalysisWorker(
            requests,
            self.app.config,
            self.app.ui_queue,
            self.cancel_event,
            self.app.db,
            self.app.limiter,
            page=page_key,
            credentials=self.app.credentials,
        )
        self.job_id = self.worker.job_id
        self.reports = []
        self.current = None
        self._pending_report = None
        self._rendered_key = None
        self.start_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        mode = "单钱包" if len(wallets) == 1 else f"批量 · {len(wallets)} 钱包"
        self.app.set_job_status(f"任务：{self.job_id[:8]} · RUNNING · {mode}")
        source_text = "数据源：GMGN 网页自动化" if collect_mode == "automation" else "数据源：运行中"
        self.progress.set_sources(source_text)
        self.app.set_source_status(source_text)
        self.progress.update_progress(f"开始分析 · {mode}", 0, len(wallets), wallets[0], "")
        self.wallet_table.set_rows(
            [
                {
                    "_id": w,
                    "wallet": short_address(w),
                    "state": "QUEUED",
                    "tokens": 0,
                    "done": 0,
                    "stage": "WAITING",
                    "key": "-",
                    "reqs": 0,
                    "rl": 0,
                    "cache": "-",
                    "elapsed": "-",
                    "_copy": w,
                }
                for w in wallets
            ]
        )
        self._refresh_keys(force=True)
        self._append_run_log(f"JOB_STARTED {self.job_id} {mode}")
        self.app.log_panel.append(f"开始分析 Job {self.job_id[:8]} {mode} {', '.join(short_address(w) for w in wallets)}")
        self.worker.start()

    def stop_analysis(self) -> None:
        self.cancel_event.set()
        self.app.log_panel.append("已请求停止，等待当前请求安全退出", "WARNING")

    def _patch_wallet(self, wallet: str, **fields) -> None:
        if not wallet:
            return
        payload = {"_id": wallet, "_copy": wallet, "wallet": short_address(wallet)}
        payload.update(fields)
        self.wallet_table.upsert_row(payload)

    def handle_message(self, msg: dict) -> None:
        jid = msg.get("job_id")
        if jid and self.job_id and jid != self.job_id:
            return
        kind = msg.get("type")
        wallet = str(msg.get("wallet") or "")
        if kind in ("WALLET_WAITING_API", "RATE_LIMITED", "KEY_RECOVERED"):
            self._refresh_keys(force=True)
        if kind in ("WALLET_STARTED", "WALLET_WAITING_API", "RATE_LIMITED", "KEY_RECOVERED", "wallet_done"):
            self._append_run_log(f"{kind} {wallet} {msg.get('message') or ''}".strip())
        if kind == "progress":
            stage = str(msg.get("stage") or "RUNNING")
            token_raw = str(msg.get("token") or "")
            token_shown = short_address(token_raw) if len(token_raw) > 16 else token_raw
            self.progress.update_progress(
                msg.get("message", ""),
                int(msg.get("done") or 0),
                int(msg.get("total") or 0),
                short_address(wallet),
                token_shown,
            )
            patch = {"state": "RUNNING", "stage": stage}
            if stage in {"token", "first_buy", "browser_token"}:
                patch["done"] = int(msg.get("done") or 0)
                patch["tokens"] = int(msg.get("total") or 0)
            self._patch_wallet(wallet, **patch)
        elif kind == "api_state":
            text = str(msg.get("state") or "API 正常")
            self.progress.set_api(text)
            self.app.set_api_status(text)
        elif kind == "WALLET_STARTED":
            self._patch_wallet(wallet, state="RUNNING", stage="STARTED")
        elif kind in ("WALLET_WAITING_API", "RATE_LIMITED"):
            text = msg.get("message") or "API 限流，等待恢复"
            self.progress.set_api(text)
            self.app.set_api_status(text)
            self._patch_wallet(wallet, state="WAITING_API", stage="API Cooldown")
            self.app.log_panel.append(text, "WARNING")
        elif kind == "KEY_RECOVERED":
            self.progress.set_api("Key 已恢复，任务继续")
            self.app.set_api_status("API 正常")
            self._patch_wallet(wallet, state="RUNNING", stage="RESUMED")
            self.app.log_panel.append("Key 已恢复", "SUCCESS")
        elif kind == "PROVIDER_HEALTH":
            text = msg.get("message") or "数据源：运行中"
            self.progress.set_sources(text)
            self.app.set_source_status(text)
            self._append_run_log(text)
        elif kind == "wallet_done":
            report: WalletReport = msg["report"]
            self.reports.append(report)
            self._patch_wallet_from_report(report)
            self.app.mark_history_dirty()
            self.app.log_panel.append(
                f"{report.status.value} {short_address(report.request.wallet_address)} 完成，Excel={report.excel_path}",
                "SUCCESS" if report.status.value == "SUCCESS" else "WARNING",
            )
            showing = self.current is None or self.current.request.wallet_address == report.request.wallet_address
            if showing:
                self._apply_or_defer_report(report)
        elif kind in ("done", "cancelled", "error"):
            self.start_btn.configure(state="normal")
            self.stop_btn.configure(state="disabled")
            self._refresh_keys(force=True)
            reports = msg.get("reports") or self.reports
            if reports:
                self._apply_or_defer_report(reports[-1])
            self.app.mark_history_dirty()
            if kind == "cancelled":
                self.progress.update_progress("已取消", 0, 0)
                self.app.set_job_status("任务：已取消")
                self.app.log_panel.append("分析已取消", "WARNING")
            elif kind == "error":
                self.progress.update_progress("分析失败", 0, 0)
                self.app.set_job_status("任务：失败")
                self.app.log_panel.append(msg.get("message") or "分析失败", "ERROR")
                messagebox.showerror("分析失败", msg.get("message") or "未知错误")
            else:
                self.progress.set_api("API 正常")
                self.app.set_job_status("任务：已完成")
                self.app.log_panel.append("全部任务完成", "SUCCESS")

    def _apply_or_defer_report(self, report: WalletReport) -> None:
        if getattr(self.app, "active_page", lambda: "analysis")() != "analysis":
            self._pending_report = report
            return
        self.render_report(report)

    def _patch_wallet_from_report(self, report: WalletReport) -> None:
        wallet = report.request.wallet_address
        stats = report.api_stats
        self._patch_wallet(
            wallet,
            state=report.status.value,
            tokens=getattr(report.summary, "token_count", 0) or 0,
            done=getattr(report.summary, "token_count", 0) or 0,
            stage="DONE",
            reqs=getattr(stats, "requests", 0) or 0,
            rl=getattr(stats, "retries_429", 0) or 0,
            cache=getattr(stats, "cache_hit_rate", "-") or "-",
            elapsed=format_elapsed(report.elapsed_seconds),
        )

    def render_report(self, report: WalletReport) -> None:
        render_key = (
            report.request.wallet_address,
            report.status.value,
            len(report.tokens),
            len(report.trades),
            str(report.excel_path or ""),
        )
        if render_key == getattr(self, "_rendered_key", None):
            self._patch_wallet_from_report(report)
            return
        self._rendered_key = render_key
        self.current = report
        self.progress.update_progress(
            f"{report.status.value} · {format_elapsed(report.elapsed_seconds)}",
            int(getattr(report.summary, "token_count", 0) or 0),
            int(getattr(report.summary, "token_count", 0) or 0),
            short_address(report.request.wallet_address),
            "",
        )
        self.app.set_job_status(
            f"任务：{short_address(report.request.wallet_address)} · {report.status.value} · {format_elapsed(report.elapsed_seconds)}"
        )
        s = report.summary
        self.cards["tokens"].set_value(str(s.token_count))
        self.cards["buysell"].set_value(f"{s.buy_count} / {s.sell_count}")
        self.cards["realized"].set_value(str(s.gmgn_realized_profit.export("usd_compact")))
        self.cards["total"].set_value(str(s.gmgn_total_profit.export("usd_compact")))
        self.cards["open"].set_value(str(s.open_positions))
        self.cards["gas"].set_value(str(s.gas_total_usd.export("usd_compact")))
        self.cards["special"].set_value(str(s.special_acquisition_count))
        complete = self._complete
        token_rows = []
        for idx, token in enumerate(report.tokens, start=1):
            row = complete.token_export_row(token)
            filters = ["全部"]
            if token.acquisition.acquisition_type.value != "BUY":
                filters.append("特殊获得")
            if str(token.source_platform.export("text")) in {"非 Launchpad", "未知来源"}:
                filters.append("非 Launchpad")
            if token.position_status.value == "仍持仓":
                filters.append("仍持仓")
            else:
                filters.append("已清仓")
            fifo_text = str(token.fifo_realized_profit.export("text"))
            if fifo_text.startswith("-"):
                filters.append("亏损")
                tag = "loss"
            elif any(ch.isdigit() for ch in fifo_text) and fifo_text not in {"0", "0.0"}:
                filters.append("有盈利")
                tag = "profit"
            else:
                tag = "special" if token.acquisition.acquisition_type.value != "BUY" else ""
            token_rows.append(
                {
                    "_id": token.token_address,
                    "idx": idx,
                    "symbol": row["币种"],
                    "address": short_address(token.token_address),
                    "platform": row["来源平台"],
                    "acq": row["获得方式"],
                    "mcap": row["入场市值"],
                    "first_buy": row["首笔买入"],
                    "amount": row["首买数量"],
                    "buy_time": row["买入时间"],
                    "created": row["创建时间"],
                    "diff": row["时差"],
                    "hold": row["持仓时长"],
                    "buys": row["买入笔数"],
                    "buy_usd": row["买入总额"],
                    "sells": row["卖出笔数"],
                    "sell_usd": row["卖出总额"],
                    "pnl": row["总盈亏%"],
                    "status": row["状态"],
                    "_copy": token.token_address,
                    "_filters": " ".join(filters),
                    "_filter": filters[1] if len(filters) > 1 else "全部",
                    "_tag": tag,
                }
            )
        self.token_table.set_rows(token_rows)
        pos_rows = [{**r, "_id": f"pos:{r['_id']}"} for r in token_rows if "仍持仓" in r.get("_filters", "")]
        self.pos_table.set_rows(pos_rows)
        platforms = {t.token_address: str(t.source_platform.export("text")) for t in report.tokens}
        trade_rows = []
        for idx, tr in enumerate(report.trades):
            row = complete.trade_export_row(tr, platforms.get(tr.token_address, "未知来源"))
            trade_rows.append(
                {
                    "_id": f"{tr.tx_hash}:{tr.event_type.value}:{idx}",
                    "time": row["时间"],
                    "type": row["类型"],
                    "symbol": row["币种"],
                    "address": short_address(tr.token_address),
                    "amount": row["数量"],
                    "usd": row["USD金额"],
                    "sol": row["SOL金额"],
                    "price": row["价格USD"],
                    "gas_usd": row["Gas USD"],
                    "gas_sol": row["Gas SOL"],
                    "pnl": row["单笔盈亏"],
                    "platform": row["来源平台"],
                    "wallet": short_address(tr.wallet_address),
                    "tx": short_address(tr.tx_hash, 6, 4),
                    "_copy": tr.tx_hash,
                    "_filter": tr.event_type.value,
                    "_tag": "profit" if tr.event_type == EventType.BUY else ("loss" if tr.event_type == EventType.SELL else "special"),
                }
            )
        self.trade_table.set_rows(trade_rows)
        self.pnl_table.set_rows(
            [
                {
                    "_id": f"pnl:{t.token_address}",
                    "symbol": t.symbol,
                    "address": short_address(t.token_address),
                    "fifo": t.fifo_realized_profit.export("usd"),
                    "gmgn": t.realized_profit.export("text"),
                    "status": t.status.value,
                    "_copy": t.token_address,
                }
                for t in report.tokens
            ]
        )
        plat_count: dict[str, dict[str, int]] = {}
        for token in report.tokens:
            name = str(token.source_platform.export("text"))
            bucket = plat_count.setdefault(name, {"count": 0, "buys": 0, "sells": 0})
            bucket["count"] += 1
            bucket["buys"] += token.buy_count
            bucket["sells"] += token.sell_count
        self.plat_table.set_rows(
            [{"_id": k, "platform": k, "count": v["count"], "buys": v["buys"], "sells": v["sells"]} for k, v in plat_count.items()]
        )
        self.warn_table.set_rows(
            [
                {
                    "_id": f"{w.wallet_address}:{w.token_address}:{w.field_name}:{idx}",
                    "wallet": short_address(w.wallet_address),
                    "token": w.token_symbol,
                    "field": w.field_name,
                    "value": w.final_value,
                    "status": w.status,
                    "reason": w.reason,
                    "source": w.source,
                    "est": "是" if w.estimated else "否",
                    "_copy": w.token_address,
                }
                for idx, w in enumerate(report.warnings)
            ]
        )
        raw_text = "\n".join(report.raw_paths) if report.raw_paths else "未保存原始 JSON"
        raw_body = f"Excel: {report.excel_path}\nJSON: {report.json_path}\n请求数: {report.api_stats.requests}\n缓存命中率: {report.api_stats.cache_hit_rate}\n\n原始文件:\n{raw_text}"
        if getattr(self, "_raw_body", None) != raw_body:
            self._raw_body = raw_body
            self.raw_box.delete("1.0", "end")
            self.raw_box.insert("1.0", raw_body)
        self.app.set_cache_rate(report.api_stats.cache_hit_rate)
