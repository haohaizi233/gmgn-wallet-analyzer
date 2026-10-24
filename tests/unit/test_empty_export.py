from __future__ import annotations

import unittest
from decimal import Decimal

from app.domain.enums import AcquisitionStatus, AcquisitionType, EventType, FieldStatus, TaskStatus
from app.domain.models import AcquisitionInfo, AuditedValue, TokenAnalysisResult, TradeRecord, known
from app.services.completeness_service import CompletenessService, assert_no_empty_export_cells
from app.utils.money import format_usd


class EmptyExportTests(unittest.TestCase):
    def test_token_and_trade_rows_have_no_empty_cells(self):
        acq = AcquisitionInfo(
            acquisition_type=AcquisitionType.TRANSFER_IN,
            timestamp=100,
            price_usd=None,
            amount=Decimal("1"),
            cost_usd=None,
            cost_sol=None,
            gas_usd=None,
            gas_sol=None,
            market_cap=None,
            status=AcquisitionStatus.NO_BUY_HISTORY,
            reason="未发现 Buy，存在 TransferIn",
            source="wallet_activity",
        )
        token = TokenAnalysisResult(
            wallet_address="Wallet",
            token_address="Mint",
            chain="sol",
            symbol="ZZ",
            name="ZZ",
            source_platform=known("Raydium", "token_info.pool.exchange"),
            acquisition=acq,
            created_at=known(50, "token_info.creation_timestamp"),
            open_at=AuditedValue(None, "token_info.open_timestamp", FieldStatus.API_MISSING, reason="GMGN 未提供"),
            pool_created_at=AuditedValue(None, "pool", FieldStatus.API_MISSING, reason="GMGN 未提供"),
            time_diff_seconds=known(50, "calc"),
            buy_count=0,
            buy_total_usd=None,
            sell_count=1,
            sell_total_usd=Decimal("10"),
            realized_profit=AuditedValue(None, "fifo", FieldStatus.NOT_APPLICABLE, reason="无法验证历史成本"),
            unrealized_profit=AuditedValue(None, "holdings", FieldStatus.API_MISSING, reason="未使用 holdings"),
            total_profit=AuditedValue(None, "profits", FieldStatus.API_MISSING, reason="无单 Token 拆分"),
            total_profit_pnl=AuditedValue(None, "profits", FieldStatus.API_MISSING, reason="无单 Token 拆分"),
            fifo_realized_profit=AuditedValue(None, "fifo", FieldStatus.NOT_APPLICABLE, reason="无法验证历史成本"),
            current_balance=Decimal("0"),
            holding_duration_seconds=known(20, "calc"),
            missing_cost_count=1,
            status=TaskStatus.WARNING,
            first_buy_display=known(Decimal("12.5"), "activity", estimated=True),
            first_buy_amount=known(Decimal("1"), "activity"),
            first_buy_time=known(100, "activity"),
            market_cap=AuditedValue(None, "activity", FieldStatus.NOT_APPLICABLE, reason="无可验证历史市值"),
        )
        svc = CompletenessService()
        svc.apply_time_fields(token, last_sell_ts=120)
        row = svc.token_export_row(token)
        trade = TradeRecord("Wallet", "sol", "Mint", "ZZ", "ZZ", "hash", 120, EventType.SELL, Decimal("1"), Decimal("10"), Decimal("10"), None, None, None, None, {}, single_pnl_display="无法验证历史成本")
        trow = svc.trade_export_row(trade, "Raydium")
        assert_no_empty_export_cells([row, trow])
        self.assertIn("$", row["首笔买入"])
        self.assertNotIn("不适用", row["首笔买入"])
        self.assertNotIn(None, row.values())
        self.assertNotIn("", row.values())

    def test_zero_sells_export_zero_not_unresolved(self):
        acq = AcquisitionInfo(
            acquisition_type=AcquisitionType.BUY,
            timestamp=100,
            price_usd=Decimal("1"),
            amount=Decimal("1"),
            cost_usd=Decimal("10"),
            cost_sol=None,
            gas_usd=None,
            gas_sol=None,
            market_cap=Decimal("1000"),
            status=AcquisitionStatus.INFERRED,
            reason="",
            source="wallet_activity",
        )
        token = TokenAnalysisResult(
            wallet_address="Wallet",
            token_address="Mint",
            chain="sol",
            symbol="AA",
            name="AA",
            source_platform=known("Pump.fun", "dex"),
            acquisition=acq,
            created_at=known(50, "token_info.creation_timestamp"),
            open_at=known(50, "dexscreener.pairCreatedAt", estimated=True),
            pool_created_at=known(50, "dexscreener.pairCreatedAt"),
            time_diff_seconds=known(50, "calc"),
            buy_count=1,
            buy_total_usd=Decimal("10"),
            sell_count=0,
            sell_total_usd=Decimal("0"),
            realized_profit=known(Decimal("0"), "local_fifo", reason="无已实现卖出"),
            unrealized_profit=known(Decimal("5"), "local_fifo", estimated=True),
            total_profit=known(Decimal("5"), "local_fifo", estimated=True),
            total_profit_pnl=known(Decimal("0.5"), "local_fifo", estimated=True),
            fifo_realized_profit=known(Decimal("0"), "local_fifo"),
            current_balance=Decimal("1"),
            holding_duration_seconds=known(20, "calc"),
            missing_cost_count=0,
            status=TaskStatus.SUCCESS,
            first_buy_display=known(Decimal("10"), "activity"),
            first_buy_amount=known(Decimal("1"), "activity"),
            first_buy_time=known(100, "activity"),
            market_cap=known(Decimal("1000"), "activity", estimated=True),
        )
        row = CompletenessService().token_export_row(token)
        self.assertEqual(row["卖出总额"], "$0.0000")
        self.assertNotIn("无法验证卖出总额", row["卖出总额"])
        self.assertNotEqual(row["开盘时间"], "GMGN 未提供")

    def test_negative_time_diff_is_signed_duration(self):
        acq = AcquisitionInfo(
            acquisition_type=AcquisitionType.BUY,
            timestamp=50,
            price_usd=Decimal("1"),
            amount=Decimal("1"),
            cost_usd=Decimal("10"),
            cost_sol=None,
            gas_usd=None,
            gas_sol=None,
            market_cap=Decimal("1000"),
            status=AcquisitionStatus.INFERRED,
            reason="",
            source="wallet_activity",
        )
        token = TokenAnalysisResult(
            wallet_address="Wallet",
            token_address="Mint",
            chain="sol",
            symbol="AA",
            name="AA",
            source_platform=known("Pump.fun", "dex"),
            acquisition=acq,
            created_at=known(150, "token_info.creation_timestamp"),
            open_at=known(150, "dexscreener.pairCreatedAt", estimated=True),
            pool_created_at=known(150, "dexscreener.pairCreatedAt"),
            time_diff_seconds=known(-100, "calc"),
            buy_count=1,
            buy_total_usd=Decimal("10"),
            sell_count=0,
            sell_total_usd=Decimal("0"),
            realized_profit=known(Decimal("0"), "local_fifo", reason="无已实现卖出"),
            unrealized_profit=known(Decimal("5"), "local_fifo", estimated=True),
            total_profit=known(Decimal("5"), "local_fifo", estimated=True),
            total_profit_pnl=known(Decimal("0.5"), "local_fifo", estimated=True),
            fifo_realized_profit=known(Decimal("0"), "local_fifo"),
            current_balance=Decimal("1"),
            holding_duration_seconds=known(20, "calc"),
            missing_cost_count=0,
            status=TaskStatus.SUCCESS,
            first_buy_display=known(Decimal("10"), "activity"),
            first_buy_amount=known(Decimal("1"), "activity"),
            first_buy_time=known(50, "activity"),
            market_cap=known(Decimal("1000"), "activity", estimated=True),
        )
        row = CompletenessService().token_export_row(token)
        self.assertEqual(row["时差"], "-1分钟")
        self.assertEqual(row["时差HMS"], "-00:01:40")
        self.assertNotIn("时间异常", row["时差"])

    def test_drawer_columns_populated_when_metrics_present(self):
        from app.browser.drawer_metrics import DrawerMetrics

        acq = AcquisitionInfo(
            acquisition_type=AcquisitionType.BUY,
            timestamp=100,
            price_usd=Decimal("1"),
            amount=Decimal("1"),
            cost_usd=Decimal("1"),
            cost_sol=None,
            gas_usd=None,
            gas_sol=None,
            market_cap=Decimal("1000"),
            status=AcquisitionStatus.VERIFIED,
            reason="ok",
            source="wallet_activity",
        )
        dm = DrawerMetrics(
            total_profit=Decimal("52.82"),
            total_profit_pnl=Decimal("-1.89"),
            avg_mc_buy=Decimal("222.03"),
            avg_mc_sell=Decimal("300"),
            bought_usd=Decimal("100"),
            bought_txs=2,
            sold_usd=Decimal("50"),
            sold_txs=1,
            transfers_in_usd=Decimal("10"),
            transfers_in_txs=1,
            transfers_out_usd=Decimal("5"),
            transfers_out_txs=1,
            balance_usd=Decimal("0"),
        )
        token = TokenAnalysisResult(
            wallet_address="W",
            token_address="M",
            chain="sol",
            symbol="LLY",
            name="LLY",
            source_platform=known("Raydium", "x"),
            acquisition=acq,
            created_at=known(50, "x"),
            open_at=known(50, "x"),
            pool_created_at=known(50, "x"),
            time_diff_seconds=known(0, "x"),
            buy_count=2,
            buy_total_usd=Decimal("100"),
            sell_count=1,
            sell_total_usd=Decimal("50"),
            realized_profit=known(Decimal("52.82"), "gmgn_drawer"),
            unrealized_profit=known(Decimal("0"), "gmgn_drawer"),
            total_profit=known(Decimal("52.82"), "gmgn_drawer"),
            total_profit_pnl=known(Decimal("-1.89"), "gmgn_drawer"),
            fifo_realized_profit=known(Decimal("52.82"), "local_fifo"),
            current_balance=Decimal("0"),
            holding_duration_seconds=known(0, "x"),
            missing_cost_count=0,
            status=TaskStatus.SUCCESS,
            first_buy_display=known(Decimal("100"), "x"),
            first_buy_amount=known(Decimal("1"), "x"),
            first_buy_time=known(50, "x"),
            market_cap=known(Decimal("222.03"), "gmgn_drawer"),
            drawer_metrics=dm,
        )
        row = CompletenessService().token_export_row(token)
        self.assertEqual(row["平均买入MC"], format_usd(Decimal("222.03")))
        self.assertEqual(row["平均卖出MC"], format_usd(Decimal("300")))
        self.assertEqual(row["转入总额"], format_usd(Decimal("10")))
        self.assertEqual(row["转入笔数"], 1)
        self.assertEqual(row["转出总额"], format_usd(Decimal("5")))
        self.assertEqual(row["转出笔数"], 1)
        self.assertEqual(row["余额"], "$0")
        self.assertNotIn("已实现盈亏", row)
        self.assertNotIn("未实现盈亏", row)
        self.assertNotIn("本地FIFO已实现", row)

    def test_drawer_columns_show_missing_when_absent(self):
        token = TokenAnalysisResult(
            wallet_address="W",
            token_address="M",
            chain="sol",
            symbol="X",
            name="X",
            source_platform=known("Raydium", "x"),
            acquisition=AcquisitionInfo(
                acquisition_type=AcquisitionType.BUY,
                timestamp=100,
                price_usd=Decimal("1"),
                amount=Decimal("1"),
                cost_usd=Decimal("1"),
                cost_sol=None,
                gas_usd=None,
                gas_sol=None,
                market_cap=Decimal("1000"),
                status=AcquisitionStatus.VERIFIED,
                reason="ok",
                source="x",
            ),
            created_at=known(50, "x"),
            open_at=known(50, "x"),
            pool_created_at=known(50, "x"),
            time_diff_seconds=known(0, "x"),
            buy_count=0,
            buy_total_usd=Decimal("0"),
            sell_count=0,
            sell_total_usd=Decimal("0"),
            realized_profit=known(Decimal("0"), "x"),
            unrealized_profit=known(Decimal("0"), "x"),
            total_profit=known(Decimal("0"), "x"),
            total_profit_pnl=known(Decimal("0"), "x"),
            fifo_realized_profit=known(Decimal("0"), "x"),
            current_balance=Decimal("0"),
            holding_duration_seconds=known(0, "x"),
            missing_cost_count=0,
            status=TaskStatus.SUCCESS,
            first_buy_display=known(Decimal("0"), "x"),
            first_buy_amount=known(Decimal("0"), "x"),
            first_buy_time=known(0, "x"),
            market_cap=known(Decimal("0"), "x"),
            drawer_metrics=None,
        )
        row = CompletenessService().token_export_row(token)
        self.assertEqual(row["平均买入MC"], "未抓取抽屉")
        self.assertEqual(row["余额"], "未抓取抽屉")


if __name__ == "__main__":
    unittest.main()
