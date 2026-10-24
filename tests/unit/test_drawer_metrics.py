from __future__ import annotations

import unittest
from decimal import Decimal

from app.browser.drawer_metrics import parse_drawer_metrics


SAMPLE = """
MSFTx
$497.16743
$52.2M
Total Profit
-11.26(-1.89%)
Unrealized
Sell All
SOL Balance
33.06 SOL
Balance
$0
Position%
0% (0 of 1.15)
Holding Duration
2h
Avg MC B/S
$52.82M / $52.60M
Bought
$578.8 / 2 TXs (1.15)
Sold
$666.32 / 5 TXs (1.33)
Transfers In
$3.04 / 2 TXs (0.006)
Transfers Out
$0 / 0 TXs (0)
"""


class DrawerMetricsTests(unittest.TestCase):
    def test_parses_total_profit_and_percent(self):
        m = parse_drawer_metrics(SAMPLE)
        self.assertEqual(m.total_profit, Decimal("-11.26"))
        self.assertEqual(m.total_profit_pnl, Decimal("-1.89"))

    def test_parses_avg_mc_buy_sell(self):
        m = parse_drawer_metrics(SAMPLE)
        self.assertEqual(m.avg_mc_buy, Decimal("52820000"))
        self.assertEqual(m.avg_mc_sell, Decimal("52600000"))

    def test_parses_bought_and_sold(self):
        m = parse_drawer_metrics(SAMPLE)
        self.assertEqual(m.bought_usd, Decimal("578.8"))
        self.assertEqual(m.bought_txs, 2)
        self.assertEqual(m.sold_usd, Decimal("666.32"))
        self.assertEqual(m.sold_txs, 5)

    def test_parses_transfers(self):
        m = parse_drawer_metrics(SAMPLE)
        self.assertEqual(m.transfers_in_usd, Decimal("3.04"))
        self.assertEqual(m.transfers_in_txs, 2)
        self.assertEqual(m.transfers_out_usd, Decimal("0"))
        self.assertEqual(m.transfers_out_txs, 0)

    def test_parses_balance(self):
        m = parse_drawer_metrics(SAMPLE)
        self.assertEqual(m.balance_usd, Decimal("0"))

    def test_empty_text_returns_empty(self):
        m = parse_drawer_metrics("")
        self.assertIsNone(m.total_profit)
        self.assertFalse(m.has_profit)

    def test_chinese_labels(self):
        text = "总盈亏\n-11.26(-1.89%)\n平均市值\n$52.82M / $52.60M\n买入\n$578.8 / 2 TXs"
        m = parse_drawer_metrics(text)
        self.assertEqual(m.total_profit, Decimal("-11.26"))
        self.assertEqual(m.avg_mc_buy, Decimal("52820000"))
        self.assertEqual(m.bought_usd, Decimal("578.8"))

    def test_overrides_take_precedence(self):
        """Sanity: drawer values are non-None so callers can prefer them."""
        m = parse_drawer_metrics(SAMPLE)
        self.assertIsNotNone(m.bought_usd)
        self.assertIsNotNone(m.bought_txs)
        self.assertIsNotNone(m.sold_usd)
        self.assertIsNotNone(m.sold_txs)
        self.assertIsNotNone(m.avg_mc_buy)
        self.assertIsNotNone(m.total_profit)

    def test_positive_profit_with_plus_sign(self):
        """GMGN renders positive profit with a leading '+', e.g. +18.24(+5.87%)."""
        text = (
            "microsoft\n$0.0\nMCP $61.4K\nTotal Profit\n\n+18.24(+5.87%)\n"
            "Unrealized\n\nSell All\nSOL Balance\n\n36.6 SOL\nBalance\n\n$0\n"
            "Avg MC B/S\n$52.47K / $50.0K\nBought\n$303.42 / 1 TXs (1.0)"
        )
        m = parse_drawer_metrics(text)
        self.assertEqual(m.total_profit, Decimal("18.24"))
        self.assertEqual(m.total_profit_pnl, Decimal("5.87"))
        self.assertEqual(m.avg_mc_buy, Decimal("52470"))
        self.assertEqual(m.bought_usd, Decimal("303.42"))
        self.assertEqual(m.bought_txs, 1)

    def test_real_drawer_snippet_format(self):
        """The exact format captured from the live GMGN drawer (icon glyphs, \\n)."""
        text = (
            "\ue0af\nFUCK\n$0.0\nMCP\xa0$9.6K\n4h\n1d\n30d\n\ue01b\n"
            "Total Profit\n\n-25.56(-5.96%)\n\ue093\n\nUnrealized\n\nSell All\n"
            "SOL Balance\n\n36.6 SOL\nBalance\n\n$0\n"
            "Avg MC B/S\n$10.33K / $0\nBought\n$417.74 / 2 TXs (1.0)"
        )
        m = parse_drawer_metrics(text)
        self.assertEqual(m.total_profit, Decimal("-25.56"))
        self.assertEqual(m.total_profit_pnl, Decimal("-5.96"))
        self.assertEqual(m.balance_usd, Decimal("0"))


if __name__ == "__main__":
    unittest.main()
