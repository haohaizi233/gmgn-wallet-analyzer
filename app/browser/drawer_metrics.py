"""Parse the GMGN token-drawer DOM text into structured per-token metrics.

The scraper already captures the drawer's `inner_text()`; this module turns that
blob into Decimals so the analysis service can use GMGN's displayed values
(Total Profit, Avg MC B/S, Bought, Sold, ...) directly instead of recomputing
them via local FIFO and risking mismatches on price/supply fields.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

_SUFFIX = {"K": 3, "M": 6, "B": 9, "T": 12, "k": 3, "m": 6, "b": 9, "t": 12}


def _parse_money(token: Optional[str]) -> Optional[Decimal]:
    """Parse '$52.82M', '$578.8', '-11.26', '$0', '1.79K' -> Decimal."""
    if not token:
        return None
    t = token.strip().replace(",", "").replace("$", "").strip()
    if not t or t in ("-", "--", "N/A"):
        return None
    m = re.match(r"^([+-]?\d+(?:\.\d+)?)\s*([KMBTkmbt]?)$", t)
    if not m:
        return None
    num = Decimal(m.group(1))
    suf = m.group(2)
    if suf:
        num = num * (Decimal(10) ** _SUFFIX[suf])
    return num


def _parse_percent(token: Optional[str]) -> Optional[Decimal]:
    if not token:
        return None
    t = token.strip().replace("%", "").replace("(", "").replace(")", "").strip()
    if not t or t in ("-", "--", "N/A"):
        return None
    try:
        return Decimal(t)
    except Exception:
        return None


def _parse_count(token: Optional[str]) -> Optional[int]:
    if not token:
        return None
    t = token.strip()
    m = re.match(r"^(\d+)", t)
    return int(m.group(1)) if m else None


@dataclass
class DrawerMetrics:
    total_profit: Optional[Decimal] = None
    total_profit_pnl: Optional[Decimal] = None
    avg_mc_buy: Optional[Decimal] = None
    avg_mc_sell: Optional[Decimal] = None
    bought_usd: Optional[Decimal] = None
    bought_txs: Optional[int] = None
    sold_usd: Optional[Decimal] = None
    sold_txs: Optional[int] = None
    transfers_in_usd: Optional[Decimal] = None
    transfers_in_txs: Optional[int] = None
    transfers_out_usd: Optional[Decimal] = None
    transfers_out_txs: Optional[int] = None
    balance_usd: Optional[Decimal] = None
    raw: str = ""

    @property
    def has_profit(self) -> bool:
        return self.total_profit is not None


# label -> list of (english, chinese) aliases
_LABELS = {
    "total_profit": ["Total Profit", "总盈亏", "总利润"],
    "avg_mc": ["Avg MC B/S", "Avg MC", "平均市值", "平均MC"],
    "bought": ["Bought", "买入"],
    "sold": ["Sold", "卖出"],
    "transfers_in": ["Transfers In", "Transfer In", "转入"],
    "transfers_out": ["Transfers Out", "Transfer Out", "转出"],
    "balance": ["Balance", "余额"],
}


def _label_pattern(aliases: list[str]) -> str:
    escaped = [re.escape(a) for a in aliases]
    return "(?:" + "|".join(escaped) + ")"


_MONEY = r"\$?[+-]?[0-9][0-9.,]*[KMBTkmmbt]?"
_INT = r"\d+"
# Anchor a label to the start of a line so "Balance" doesn't match inside "SOL Balance".
_LINE_START = r"(?:\A|\n)\s*"


def parse_drawer_metrics(text: str) -> DrawerMetrics:
    """Best-effort extraction of per-token metrics from the drawer's inner_text."""
    metrics = DrawerMetrics(raw=text or "")
    if not text:
        return metrics
    blob = text

    def find(pattern: str) -> Optional[str]:
        m = re.search(pattern, blob, re.IGNORECASE)
        return m.group(1) if m else None

    def find_pair(pattern: str) -> tuple[Optional[str], Optional[str]]:
        m = re.search(pattern, blob, re.IGNORECASE)
        return (m.group(1), m.group(2)) if m else (None, None)

    # Total Profit: "-11.26(-1.89%)" or "-11.26 (-1.89%)" or "-11.26"
    tp_label = _label_pattern(_LABELS["total_profit"])
    m = re.search(
        rf"{_LINE_START}{tp_label}\s*[:\s]*({_MONEY})\s*(?:\(\s*([+-]?[0-9][0-9.,]*)\s*%\s*\))?",
        blob,
        re.IGNORECASE,
    )
    if m:
        metrics.total_profit = _parse_money(m.group(1))
        metrics.total_profit_pnl = _parse_percent(m.group(2))

    # Avg MC B/S: "$52.82M / $52.60M"
    avg_label = _label_pattern(_LABELS["avg_mc"])
    buy_mc, sell_mc = find_pair(
        rf"{_LINE_START}{avg_label}\s*[:\s]*({_MONEY})\s*(?:/|/|，|,)\s*({_MONEY})"
    )
    metrics.avg_mc_buy = _parse_money(buy_mc)
    metrics.avg_mc_sell = _parse_money(sell_mc)

    # Bought / Sold / Transfers: "$578.8 / 2 TXs" optionally "(1.15)"
    def parse_tx_field(key: str) -> tuple[Optional[Decimal], Optional[int]]:
        label = _label_pattern(_LABELS[key])
        m = re.search(
            rf"{_LINE_START}{label}\s*[:\s]*({_MONEY})\s*/\s*({_INT})\s*TXs?",
            blob,
            re.IGNORECASE,
        )
        if not m:
            return None, None
        return _parse_money(m.group(1)), _parse_count(m.group(2))

    metrics.bought_usd, metrics.bought_txs = parse_tx_field("bought")
    metrics.sold_usd, metrics.sold_txs = parse_tx_field("sold")
    metrics.transfers_in_usd, metrics.transfers_in_txs = parse_tx_field("transfers_in")
    metrics.transfers_out_usd, metrics.transfers_out_txs = parse_tx_field("transfers_out")

    # Balance: "$0" (line-anchored to avoid matching "SOL Balance")
    bal_label = _label_pattern(_LABELS["balance"])
    metrics.balance_usd = _parse_money(find(rf"{_LINE_START}{bal_label}\s*[:\s]*({_MONEY})"))

    return metrics
