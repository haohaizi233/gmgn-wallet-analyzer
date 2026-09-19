from __future__ import annotations

import threading
from tkinter import messagebox

import customtkinter as ctk

from app.api.exceptions import GMGNError
from app.api.gmgn_client import GMGNClient
from app.config import load_config, save_api_settings
from app.gui.theme import BG, MUTED, PANEL, TEXT, font
from app.utils.paths import APP_DIR, COOKIE_DIR, ENV_PATH, INPUT_DIR, LOG_DIR, OUTPUT_DIR, default_wallet_file, open_in_explorer


class SettingsPage(ctk.CTkFrame):
    def __init__(self, master, app, **kwargs):
        super().__init__(master, fg_color=BG, **kwargs)
        self.app = app
        scroll = ctk.CTkScrollableFrame(self, fg_color=BG, corner_radius=0)
        scroll.pack(fill="both", expand=True)
        card = ctk.CTkFrame(scroll, fg_color=PANEL)
        card.pack(fill="x", padx=24, pady=24)
        ctk.CTkLabel(card, text="系统设置", font=font(20, "bold"), text_color=TEXT, anchor="w").pack(fill="x", padx=20, pady=(18, 6))
        ctk.CTkLabel(
            card,
            text="每个 GMGN API Key 拥有独立加权限流器。若短时间内多把 Key 同时 429，会自动判定可能存在 IP/账号共享限制并进入全局冷却。不要填写钱包私钥。",
            font=font(13),
            text_color=MUTED,
            anchor="w",
            wraplength=900,
        ).pack(fill="x", padx=20)

        form = ctk.CTkFrame(card, fg_color="transparent")
        form.pack(fill="x", padx=20, pady=12)
        ctk.CTkLabel(form, text="API Keys（一行一把）", font=font(13), text_color=TEXT).grid(row=0, column=0, sticky="nw", pady=8)
        self.keys_box = ctk.CTkTextbox(form, width=420, height=90, font=font(13))
        self.keys_box.grid(row=0, column=1, sticky="w", padx=8)
        self.keys_box.insert("1.0", "\n".join(app.config.api_keys or ([app.config.api_key] if app.config.api_key else [])))

        ctk.CTkLabel(form, text="Base URL", font=font(13), text_color=TEXT).grid(row=1, column=0, sticky="w", pady=8)
        self.base_var = ctk.StringVar(value=app.config.api_base)
        ctk.CTkEntry(form, textvariable=self.base_var, width=420).grid(row=1, column=1, sticky="w", padx=8)

        ctk.CTkLabel(form, text="套餐", font=font(13), text_color=TEXT).grid(row=2, column=0, sticky="w", pady=8)
        self.plan_var = ctk.StringVar(value=app.config.plan)
        ctk.CTkComboBox(form, variable=self.plan_var, values=["Free", "Plus", "Pro"], width=160, command=self._plan_changed).grid(row=2, column=1, sticky="w", padx=8)

        ctk.CTkLabel(form, text="服务端 Rate / Capacity", font=font(13), text_color=TEXT).grid(row=3, column=0, sticky="w", pady=8)
        cap = ctk.CTkFrame(form, fg_color="transparent")
        cap.grid(row=3, column=1, sticky="w", padx=8)
        self.rate_var = ctk.StringVar(value=str(int(app.config.rate)))
        self.cap_var = ctk.StringVar(value=str(int(app.config.capacity)))
        ctk.CTkEntry(cap, textvariable=self.rate_var, width=80).pack(side="left")
        ctk.CTkLabel(cap, text=" / ").pack(side="left")
        ctk.CTkEntry(cap, textvariable=self.cap_var, width=80).pack(side="left")
        ctk.CTkLabel(cap, text="  （每 Key；Free=5）", font=font(12), text_color=MUTED).pack(side="left")

        ctk.CTkLabel(form, text="目标利用率", font=font(13), text_color=TEXT).grid(row=4, column=0, sticky="w", pady=8)
        self.util_var = ctk.StringVar(value=self._util_label(app.config.target_utilization))
        ctk.CTkComboBox(form, variable=self.util_var, values=["保守 60%", "推荐 75%", "较高 80%"], width=160).grid(row=4, column=1, sticky="w", padx=8)

        ctk.CTkLabel(form, text="API Workers", font=font(13), text_color=TEXT).grid(row=5, column=0, sticky="w", pady=8)
        self.workers_var = ctk.StringVar(value=str(app.config.api_workers))
        ctk.CTkComboBox(form, variable=self.workers_var, values=["2", "3", "4", "6", "8"], width=160).grid(row=5, column=1, sticky="w", padx=8)

        self.key_table = ctk.CTkLabel(card, text=self._key_status_text(), font=font(12), text_color=MUTED, anchor="w", justify="left")
        self.key_table.pack(fill="x", padx=20)

        btns = ctk.CTkFrame(card, fg_color="transparent")
        btns.pack(fill="x", padx=20, pady=(8, 20))
        ctk.CTkButton(btns, text="保存", width=100, command=self.save).pack(side="left")
        ctk.CTkButton(btns, text="测试连接", width=100, fg_color="#E5E7EB", text_color=TEXT, command=self.test).pack(side="left", padx=8)
        ctk.CTkButton(btns, text="全部测试", width=100, fg_color="#E5E7EB", text_color=TEXT, command=self.test_all).pack(side="left")
        self.status = ctk.CTkLabel(btns, text="", font=font(13), text_color=MUTED)
        self.status.pack(side="left", padx=8)

        sources = ctk.CTkFrame(scroll, fg_color=PANEL)
        sources.pack(fill="x", padx=24, pady=(0, 24))
        ctk.CTkLabel(sources, text="数据源设置", font=font(18, "bold"), text_color=TEXT, anchor="w").pack(fill="x", padx=20, pady=(16, 4))
        ctk.CTkLabel(
            sources,
            text="没有付费 Moralis 不影响分析。主路径是 Solana RPC（有 Helius Key 时走 Helius RPC）+ DEX Screener。Moralis 仅在有付费 Key 时作为 Swap 主索引；GMGN 只做辅助验证。",
            font=font(12),
            text_color=MUTED,
            anchor="w",
            wraplength=900,
        ).pack(fill="x", padx=20)
        grid = ctk.CTkFrame(sources, fg_color="transparent")
        grid.pack(fill="x", padx=20, pady=10)
        ctk.CTkLabel(grid, text="Helius Keys（一行一把，可选增强）", font=font(13), text_color=TEXT).grid(row=0, column=0, sticky="nw", pady=6)
        self.helius_box = ctk.CTkTextbox(grid, width=420, height=70, font=font(13))
        self.helius_box.grid(row=0, column=1, sticky="w", padx=8)
        existing_helius = list(getattr(app.config, "helius_api_keys", None) or [])
        if not existing_helius and getattr(app.config, "helius_api_key", ""):
            existing_helius = [app.config.helius_api_key]
        if existing_helius:
            self.helius_box.insert("1.0", "\n".join(existing_helius))
        ctk.CTkLabel(grid, text="Helius 月度 Credit 预算（本地估算）", font=font(13), text_color=TEXT).grid(row=1, column=0, sticky="w", pady=6)
        self.helius_budget_var = ctk.StringVar(value=str(int(getattr(app.config, "helius_monthly_credit_budget", 1_000_000) or 1_000_000)))
        ctk.CTkEntry(grid, textvariable=self.helius_budget_var, width=160).grid(row=1, column=1, sticky="w", padx=8)
        ctk.CTkLabel(grid, text="Helius 目标 RPS", font=font(13), text_color=TEXT).grid(row=2, column=0, sticky="w", pady=6)
        self.helius_rps_var = ctk.StringVar(value=str(getattr(app.config, "helius_target_rps", 8) or 8))
        ctk.CTkEntry(grid, textvariable=self.helius_rps_var, width=160).grid(row=2, column=1, sticky="w", padx=8)
        ctk.CTkLabel(grid, text="Solana RPC URL（可选覆盖）", font=font(13), text_color=TEXT).grid(row=3, column=0, sticky="w", pady=6)
        self.rpc_var = ctk.StringVar(value=getattr(app.config, "solana_rpc_url", "") or "")
        ctk.CTkEntry(grid, textvariable=self.rpc_var, width=420).grid(row=3, column=1, sticky="w", padx=8)
        ctk.CTkLabel(grid, text="有 Helius Key 时自动生成官方 RPC，不必手填。", font=font(11), text_color=MUTED).grid(row=4, column=1, sticky="w", padx=8)
        ctk.CTkLabel(grid, text="验证模式", font=font(13), text_color=TEXT).grid(row=5, column=0, sticky="w", pady=6)
        self.mode_var = ctk.StringVar(value=getattr(app.config, "verification_mode", "BALANCED") or "BALANCED")
        ctk.CTkComboBox(grid, variable=self.mode_var, values=["FAST", "BALANCED", "STRICT"], width=160).grid(row=5, column=1, sticky="w", padx=8)
        ctk.CTkLabel(grid, text="启用 Moralis（主索引）", font=font(13), text_color=TEXT).grid(row=6, column=0, sticky="w", pady=6)
        self.enable_moralis_var = ctk.BooleanVar(value=bool(getattr(app.config, "enable_moralis", False)))
        ctk.CTkCheckBox(grid, text="仅在有付费 Moralis Key 时勾选", variable=self.enable_moralis_var).grid(row=6, column=1, sticky="w", padx=8)
        ctk.CTkLabel(grid, text="Moralis Keys（可选，一行一把）", font=font(13), text_color=TEXT).grid(row=7, column=0, sticky="nw", pady=6)
        self.moralis_box = ctk.CTkTextbox(grid, width=420, height=70, font=font(13))
        self.moralis_box.grid(row=7, column=1, sticky="w", padx=8)
        existing_moralis = list(getattr(app.config, "moralis_api_keys", None) or [])
        if not existing_moralis and getattr(app.config, "moralis_api_key", ""):
            existing_moralis = [app.config.moralis_api_key]
        if existing_moralis:
            self.moralis_box.insert("1.0", "\n".join(existing_moralis))
        src_btns = ctk.CTkFrame(sources, fg_color="transparent")
        src_btns.pack(fill="x", padx=20, pady=(0, 16))
        ctk.CTkButton(src_btns, text="测试数据源", width=120, command=self.test_sources).pack(side="left")
        self.source_status = ctk.CTkLabel(src_btns, text="Solana RPC 主路径 · Helius 可选增强 · DEX 无需 Key · Moralis 可选", font=font(12), text_color=MUTED)
        self.source_status.pack(side="left", padx=10)

        cookies = ctk.CTkFrame(scroll, fg_color=PANEL)
        cookies.pack(fill="x", padx=24, pady=(0, 24))
        ctk.CTkLabel(cookies, text="GMGN Cookie 号池", font=font(18, "bold"), text_color=TEXT, anchor="w").pack(fill="x", padx=20, pady=(16, 4))
        ctk.CTkLabel(
            cookies,
            text="自动化采集会复用已保存的登录态。全部失效时自动弹出有头窗口扫码，成功后写回号池。不要手工粘贴 Cookie 字符串。",
            font=font(12),
            text_color=MUTED,
            anchor="w",
            wraplength=900,
        ).pack(fill="x", padx=20)
        self.cookie_table = ctk.CTkLabel(cookies, text=self._cookie_status_text(), font=font(12), text_color=MUTED, anchor="w", justify="left")
        self.cookie_table.pack(fill="x", padx=20, pady=8)
        ck_btns = ctk.CTkFrame(cookies, fg_color="transparent")
        ck_btns.pack(fill="x", padx=20, pady=(0, 16))
        ctk.CTkButton(ck_btns, text="扫码登录（加入号池）", width=160, command=self.qr_login).pack(side="left")
        ctk.CTkButton(ck_btns, text="清理失效 Cookie", width=130, fg_color="#E5E7EB", text_color=TEXT, command=self.purge_cookies).pack(side="left", padx=8)
        ctk.CTkButton(ck_btns, text="打开 Cookie 目录", width=130, fg_color="#E5E7EB", text_color=TEXT, command=lambda: open_in_explorer(COOKIE_DIR)).pack(side="left")
        self.cookie_status = ctk.CTkLabel(ck_btns, text="", font=font(12), text_color=MUTED)
        self.cookie_status.pack(side="left", padx=10)

        paths_card = ctk.CTkFrame(scroll, fg_color=PANEL)
        paths_card.pack(fill="x", padx=24, pady=(0, 24))
        ctk.CTkLabel(paths_card, text="本机目录", font=font(18, "bold"), text_color=TEXT, anchor="w").pack(fill="x", padx=20, pady=(16, 4))
        ctk.CTkLabel(
            paths_card,
            text="打包成 exe 后，配置、地址 txt、Excel 都在程序旁边的文件夹，不会写进临时解压目录。",
            font=font(12),
            text_color=MUTED,
            anchor="w",
            wraplength=900,
        ).pack(fill="x", padx=20)
        ctk.CTkLabel(
            paths_card,
            text=(
                f"默认地址文件：{default_wallet_file()}\n"
                f"Excel 输出：{OUTPUT_DIR}\n"
                f"运行目录：{APP_DIR}\n"
                f"配置文件：{ENV_PATH}\n"
                f"日志目录：{LOG_DIR}"
            ),
            font=font(12),
            text_color=TEXT,
            anchor="w",
            justify="left",
        ).pack(fill="x", padx=20, pady=8)
        path_btns = ctk.CTkFrame(paths_card, fg_color="transparent")
        path_btns.pack(fill="x", padx=20, pady=(0, 16))
        ctk.CTkButton(path_btns, text="打开运行目录", width=110, fg_color="#E5E7EB", text_color=TEXT, command=lambda: open_in_explorer(APP_DIR)).pack(side="left")
        ctk.CTkButton(path_btns, text="打开输入目录", width=110, fg_color="#E5E7EB", text_color=TEXT, command=lambda: open_in_explorer(INPUT_DIR)).pack(side="left", padx=8)
        ctk.CTkButton(path_btns, text="打开输出目录", width=110, fg_color="#E5E7EB", text_color=TEXT, command=lambda: open_in_explorer(OUTPUT_DIR)).pack(side="left")
        ctk.CTkButton(path_btns, text="打开日志目录", width=110, fg_color="#E5E7EB", text_color=TEXT, command=lambda: open_in_explorer(LOG_DIR)).pack(side="left", padx=8)

    def _util_label(self, value: float) -> str:
        if value <= 0.62:
            return "保守 60%"
        if value >= 0.78:
            return "较高 80%"
        return "推荐 75%"

    def _util_value(self) -> float:
        text = self.util_var.get()
        if "60" in text:
            return 0.60
        if "80" in text:
            return 0.80
        return 0.75

    def _parse_key_box(self, box) -> list[str]:
        text = box.get("1.0", "end")
        keys = []
        for line in text.replace(",", "\n").splitlines():
            item = line.strip()
            if item and item not in keys:
                keys.append(item)
        return keys

    def _helius_keys(self) -> list[str]:
        return self._parse_key_box(self.helius_box)

    def _moralis_keys(self) -> list[str]:
        return self._parse_key_box(self.moralis_box)

    def _keys(self) -> list[str]:
        return self._parse_key_box(self.keys_box)

    def _key_status_text(self) -> str:
        lines = [
            f"每 Key 限额：{self.app.config.plan}  {int(self.app.config.rate)} weighted units/s",
            f"最大目标利用率：{int(self.app.config.target_utilization * 100)}%   初始：{int(getattr(self.app.config,'initial_utilization',0.6)*100)}%   Workers：{self.app.config.api_workers}",
            "短时间多 Key 同时 429 时自动降级为共享限制保护。",
        ]
        for item in self.app.credentials.snapshot():
            lines.append(
                f"{item['masked']}  {item['state']}  目标={int((item.get('utilization') or 0)*100)}%  请求={item['requests']}  429={item['429']}  inflight={item.get('inflight')}"
            )
        return "\n".join(lines)

    def _cookie_status_text(self) -> str:
        try:
            from app.browser.cookie_pool import CookiePool

            pool = CookiePool()
            rows = pool.all()
        except Exception as exc:
            return f"号池读取失败：{exc}"
        if not rows:
            return f"号池为空。目录：{COOKIE_DIR}"
        lines = [f"共 {len(rows)} 个账号，可用 {len(pool.usable())} 个（必须含登录 Cookie）"]
        for item in rows:
            lines.append(f"{item.id}  {item.state}  失败={item.fail_count}")
        return "\n".join(lines)

    def qr_login(self) -> None:
        self.cookie_status.configure(text="正在打开有头浏览器，请扫码…")

        def work():
            try:
                from app.browser.cookie_pool import CookiePool
                from app.browser.session import BrowserSession

                session = BrowserSession(
                    CookiePool(),
                    headed=True,
                    on_event=lambda m: self.after(0, lambda msg=m: self.cookie_status.configure(text=msg)),
                )
                try:
                    account = session.login_new_account()
                    account_id = account.id
                    self.after(0, lambda: self._qr_ok(account_id))
                finally:
                    session.close()
            except Exception as exc:
                self.after(0, lambda: self._qr_fail(str(exc)))

        threading.Thread(target=work, daemon=True).start()

    def _qr_ok(self, account_id: str) -> None:
        self.cookie_table.configure(text=self._cookie_status_text())
        self.cookie_status.configure(text=f"已加入号池 {account_id}")
        messagebox.showinfo("扫码登录", f"Cookie 已保存：{account_id}")

    def _qr_fail(self, message: str) -> None:
        self.cookie_status.configure(text="扫码失败")
        messagebox.showerror("扫码登录失败", message)

    def purge_cookies(self) -> None:
        from app.browser.cookie_pool import CookiePool

        removed = CookiePool().purge_expired()
        self.cookie_table.configure(text=self._cookie_status_text())
        self.cookie_status.configure(text=f"已清理 {removed} 个失效 Cookie")

    def _plan_changed(self, value: str) -> None:
        mapping = {"Free": ("5", "5"), "Plus": ("20", "20"), "Pro": ("50", "50")}
        rate, cap = mapping.get(value, ("5", "5"))
        self.rate_var.set(rate)
        self.cap_var.set(cap)

    def save(self) -> None:
        from app.config import _rehome_helius_keys

        moralis_keys, helius_keys = _rehome_helius_keys(self._moralis_keys(), self._helius_keys())
        if moralis_keys != self._moralis_keys() or helius_keys != self._helius_keys():
            self.moralis_box.delete("1.0", "end")
            if moralis_keys:
                self.moralis_box.insert("1.0", "\n".join(moralis_keys))
            self.helius_box.delete("1.0", "end")
            if helius_keys:
                self.helius_box.insert("1.0", "\n".join(helius_keys))
            if not moralis_keys:
                self.enable_moralis_var.set(False)
        keys = self._keys()
        save_api_settings(
            keys[0] if keys else "",
            self.base_var.get().strip() or "https://openapi.gmgn.ai",
            self.plan_var.get(),
            float(self.rate_var.get() or 5),
            float(self.cap_var.get() or 5),
            api_keys=keys,
            target_utilization=self._util_value(),
            api_workers=int(self.workers_var.get() or 4),
            extra={
                "MORALIS_API_KEY": self._moralis_keys()[0] if self._moralis_keys() else "",
                "MORALIS_API_KEYS": ",".join(self._moralis_keys()),
                "ENABLE_MORALIS": "true" if (self.enable_moralis_var.get() and self._moralis_keys()) else "false",
                "HELIUS_API_KEY": self._helius_keys()[0] if self._helius_keys() else "",
                "HELIUS_API_KEYS": ",".join(self._helius_keys()),
                "HELIUS_TARGET_RPS": self.helius_rps_var.get().strip() or "8",
                "HELIUS_MONTHLY_CREDIT_BUDGET": self.helius_budget_var.get().strip() or "1000000",
                "SOLANA_RPC_URL": self.rpc_var.get().strip() or "https://api.mainnet-beta.solana.com",
                "VERIFICATION_MODE": self.mode_var.get().strip() or "BALANCED",
                "ENABLE_GMGN": "true",
                "ENABLE_GMGN_DEEP_HISTORY_FALLBACK": "false",
                "GMGN_COLLECTION_MODE": getattr(self.app.config, "collection_mode", "automation") or "automation",
                "GMGN_BROWSER_HEADED": "true" if getattr(self.app.config, "browser_headed", True) else "false",
            },
        )
        self.app.reload_config()
        self.key_table.configure(text=self._key_status_text())
        self.status.configure(text="已保存到 .env")
        messagebox.showinfo("已保存", "API 配置已写入 .env。每个 Key 独立限流；共享限制会自动检测。")

    def test(self) -> None:
        self.save()
        self.status.configure(text="测试中…")

        def work():
            try:
                client = GMGNClient(
                    self.app.config.api_key,
                    self.app.config.api_base,
                    limiter=self.app.limiter,
                    credential_pool=self.app.credentials,
                )
                data = client.test_connection()
                self.after(0, lambda: self._test_ok(data))
            except GMGNError as exc:
                self.after(0, lambda: self._test_fail(str(exc)))
            except Exception as exc:
                self.after(0, lambda: self._test_fail(str(exc)))

        threading.Thread(target=work, daemon=True).start()

    def test_all(self) -> None:
        self.save()
        self.status.configure(text="逐把 Key 测试中（经过全局限流）…")

        def work():
            errors = []
            from app.api.credential_pool import CredentialPool, mask_key

            for i, key in enumerate(self.app.config.api_keys, start=1):
                try:
                    pool = CredentialPool([key])
                    client = GMGNClient(key, self.app.config.api_base, limiter=self.app.limiter, credential_pool=pool)
                    client.test_connection()
                except Exception as exc:
                    errors.append(f"{mask_key(key, i)}: {exc}")
            if errors:
                self.after(0, lambda: self._test_fail("\n".join(errors)))
            else:
                self.after(0, lambda: self._test_ok({"keys": len(self.app.config.api_keys)}))

        threading.Thread(target=work, daemon=True).start()

    def _test_ok(self, data) -> None:
        self.status.configure(text="连接成功")
        self.key_table.configure(text=self._key_status_text())
        self.app.set_api_status("API 正常")
        messagebox.showinfo("测试连接", f"连接成功。\n{list(data)[:8] if isinstance(data, dict) else type(data)}")

    def _test_fail(self, message: str) -> None:
        self.status.configure(text="连接失败")
        self.app.set_api_status("API 异常")
        messagebox.showerror("测试连接失败", message)

    def test_sources(self) -> None:
        self.save()
        self.source_status.configure(text="探测中（仅用户触发，不会在启动时打接口）…")

        def work():
            lines = []
            try:
                from app.providers.moralis.client import MoralisProvider

                m = MoralisProvider(
                    self.app.config.moralis_api_key,
                    api_keys=list(getattr(self.app.config, "moralis_api_keys", None) or []),
                    enabled_flag=bool(getattr(self.app.config, "enable_moralis", False)),
                )
                if m.enabled and m.api_key:
                    m.test_connection()
                    lines.append(f"Moralis：HEALTHY（{len(m.api_keys)} Key 轮换）")
                elif m.api_key:
                    lines.append("Moralis：DISABLED（已保存 Key，未启用）")
                else:
                    lines.append("Moralis：DISABLED / 未配置（可选）")
            except Exception as exc:
                lines.append(f"Moralis：失败 {exc}")
            try:
                from app.providers.solana.rpc_client import SolanaRpcProvider

                r = SolanaRpcProvider(self.app.config.solana_rpc_url)
                r.test_connection()
                lines.append("Solana RPC：HEALTHY")
            except Exception as exc:
                lines.append(f"Solana RPC：失败 {exc}")
            try:
                from app.providers.helius.client import HeliusProvider

                h = HeliusProvider(
                    self.app.config.helius_api_key,
                    api_keys=list(getattr(self.app.config, "helius_api_keys", None) or []),
                    target_rps=float(getattr(self.app.config, "helius_target_rps", 8) or 8),
                    monthly_budget=int(getattr(self.app.config, "helius_monthly_credit_budget", 1_000_000) or 1_000_000),
                )
                if h.api_key:
                    probed = h.probe_capabilities()
                    lines.append("Helius：" + h.health_detail)
                    for cap, status in probed.items():
                        lines.append(f"  {cap}: {status}")
                else:
                    lines.append("Helius：NOT CONFIGURED (Optional)")
            except Exception as exc:
                lines.append(f"Helius：失败 {exc}")
            try:
                from app.providers.dexscreener.client import DexScreenerProvider

                d = DexScreenerProvider()
                d.test_connection()
                lines.append("DEX Screener：HEALTHY")
            except Exception as exc:
                lines.append(f"DEX Screener：失败 {exc}")
            try:
                from app.providers.jupiter.client import JupiterProvider

                j = JupiterProvider()
                probed = j.test_connection()
                lines.append("Jupiter：" + ("HEALTHY" if probed.get("ok") else "EMPTY"))
            except Exception as exc:
                lines.append(f"Jupiter：失败 {exc}")
            try:
                from app.providers.geckoterminal.client import GeckoTerminalProvider

                g = GeckoTerminalProvider()
                probed = g.test_connection()
                lines.append("GeckoTerminal：" + ("HEALTHY" if probed.get("ok") else "EMPTY"))
            except Exception as exc:
                lines.append(f"GeckoTerminal：失败 {exc}")
            text = "\n".join(lines)
            self.after(0, lambda: self._sources_done(text))

        threading.Thread(target=work, daemon=True).start()

    def _sources_done(self, text: str) -> None:
        self.source_status.configure(text=text.splitlines()[0] if text else "完成")
        messagebox.showinfo("数据源探测", text)
