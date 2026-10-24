from __future__ import annotations

import unittest
from decimal import Decimal

from app.domain.enums import EventType
from app.domain.models import TradeRecord
from app.services.pnl_service import apply_fifo


def t(ts: int, event: EventType, amount: str, cost: str | None) -> TradeRecord:
    return TradeRecord(
        "W", "sol", "M", "S", "S", f"{event.value}{ts}", ts, event,
        Decimal(amount), Decimal("1"), Decimal(cost) if cost is not None else None,
        None, None, None, None, {},
    )


class FifoTests(unittest.TestCase):
    def test_partial_match_and_missing_cost(self):
        trades = [
            t(1, EventType.BUY, "10", "10"),
            t(2, EventType.BUY, "5", "10"),
            t(3, EventType.SELL, "12", "30"),
            t(4, EventType.SELL, "10", "20"),
        ]
        result = apply_fifo(trades)
        self.assertGreater(result.missing_cost_count, 0)
        self.assertEqual(result.missing_cost_count, 1)
        self.assertEqual(result.missing_cost_sell_count, 1)
        self.assertGreater(result.missing_cost_token_amount, 0)
        sell12 = next(x for x in result.trades if x.tx_hash == "sell3")
        self.assertTrue(sell12.single_pnl_display.startswith("$") or sell12.single_pnl_display.startswith("-$"))
        self.assertNotIn("'", sell12.single_pnl_display)
        sell_over = next(x for x in result.trades if x.tx_hash == "sell4")
        self.assertEqual(sell_over.single_pnl_display, "无法验证历史成本")
        buy = next(x for x in result.trades if x.event_type == EventType.BUY)
        self.assertEqual(buy.single_pnl_display, "未实现")

    def test_no_sells_realized_is_zero(self):
        trades = [t(1, EventType.BUY, "10", "10")]
        result = apply_fifo(trades)
        self.assertEqual(result.realized_profit, Decimal("0"))
        self.assertEqual(result.missing_cost_count, 0)
        self.assertEqual(result.current_balance, Decimal("10"))


class UnrealizedRugGuardTests(unittest.TestCase):
    def test_holding_below_market_cap_is_kept(self):
        from app.services.wallet_analysis_service import _unrealized_profit
        from app.domain.enums import FieldStatus

        av = _unrealized_profit(Decimal("500"), Decimal("100"), Decimal("10000"), Decimal("5000"))
        self.assertEqual(av.value, Decimal("400"))
        self.assertIn(av.status, {FieldStatus.ESTIMATED, FieldStatus.KNOWN})

    def test_holding_above_market_cap_is_rejected(self):
        from app.services.wallet_analysis_service import _unrealized_profit
        from app.domain.enums import FieldStatus

        av = _unrealized_profit(Decimal("231000000"), Decimal("5800"), Decimal("5000"), Decimal("1000"))
        self.assertIsNone(av.value)
        self.assertEqual(av.status, FieldStatus.API_MISSING)
        self.assertIn("代币市值", av.reason)

    def test_holding_far_above_liquidity_is_rejected(self):
        from app.services.wallet_analysis_service import _unrealized_profit
        from app.domain.enums import FieldStatus

        # MC missing, but liquidity tiny vs holding -> bogus
        av = _unrealized_profit(Decimal("180000000"), Decimal("100"), None, Decimal("500"))
        self.assertIsNone(av.value)
        self.assertEqual(av.status, FieldStatus.API_MISSING)
        self.assertIn("流动性", av.reason)

    def test_low_liquidity_small_holding_is_kept(self):
        from app.services.wallet_analysis_service import _unrealized_profit

        # Low liquidity but holding also small -> not bogus
        av = _unrealized_profit(Decimal("500"), Decimal("100"), None, Decimal("300"))
        self.assertEqual(av.value, Decimal("400"))


if __name__ == "__main__":
    unittest.main()
