from __future__ import annotations

import customtkinter as ctk

from app.gui.theme import BG, MUTED, PANEL, TEXT, font
from app.version import APP_NAME, __version__


class AboutPage(ctk.CTkFrame):
    def __init__(self, master, **kwargs):
        super().__init__(master, fg_color=BG, **kwargs)
        scroll = ctk.CTkScrollableFrame(self, fg_color=BG, corner_radius=0)
        scroll.pack(fill="both", expand=True)
        card = ctk.CTkFrame(scroll, fg_color=PANEL)
        card.pack(fill="x", expand=True, padx=24, pady=24)
        ctk.CTkLabel(card, text=APP_NAME, font=font(22, "bold"), text_color=TEXT, anchor="w").pack(fill="x", padx=24, pady=(24, 8))
        text = (
            f"版本 {__version__}\n\n"
            "本工具是 Solana 钱包交易的多数据源可验证分析器。\n"
            "核心原则：准确、完整、可解释、可恢复、Excel 0 空字段。\n\n"
            "数据源：\n"
            "- Moralis：可选 Swap 主索引（需付费 Key；没有不影响分析）\n"
            "- Solana RPC：链上事实主路径与裁决（公共节点无需 Key）\n"
            "- DEX Screener：池子 / 市值 / FDV（无需 Key）\n"
            "- GMGN：默认为网页自动化（Cookie 号池 + 扫码登录）；也可改回 OpenAPI Key\n"
            "- Helius：可选增强（RPC / 历史）；无 Key 时完全正常\n\n"
            "代币名单 = 周期流水 ∪ 当前 SPL 持仓 ∪ 官方 holdings（若可用）。\n"
            "缺首次买入时按该 mint 的 ATA 历史补扫，不把转入误判成抓漏。\n"
            "自动化模式会复用 Cookie；失效时有头窗口扫码并写回号池。\n"
            "不把 symbol 当主键；所有关联使用 (chain, mint)。\n"
            "首次买入由链上 blockTime 最终裁决。\n"
            "无法验证时输出明确语义，禁止用 0 伪装无数据。\n"
            "官方盈亏与本地 FIFO 分开保存。\n\n"
            "独立运行时：\n"
            "- input/地址.txt：钱包列表（可直接改这个 txt）\n"
            "- output/：导出的 Excel\n"
            "- 程序同目录 .env：API Key 与运行参数（现有配置作为默认）"
        )
        ctk.CTkLabel(card, text=text, justify="left", anchor="nw", font=font(14), text_color=MUTED).pack(fill="both", expand=True, padx=24, pady=(0, 24))
