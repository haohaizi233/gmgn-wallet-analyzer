from __future__ import annotations

import json
import unittest
from decimal import Decimal
from pathlib import Path

from app.api.parsers import parse_activity_page, parse_event_type, parse_token_info
from app.domain.enums import EventType
from app.services.activity_collector import ActivityCollector


class ParserTests(unittest.TestCase):
    def test_activity_fixture_and_event_alias(self):
        path = Path(__file__).resolve().parents[1] / "fixtures" / "activity_page.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        trades, nxt = parse_activity_page(payload, "Wallet", "sol")
        self.assertIsNone(nxt)
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0].token_address, "TokenAAA")
        self.assertEqual(trades[0].event_type, EventType.SELL)
        self.assertEqual(parse_event_type("buy"), EventType.BUY)
        self.assertEqual(parse_event_type("transferIn"), EventType.TRANSFER_IN)

    def test_token_info_keeps_created_and_open_separate(self):
        info = parse_token_info(
            {
                "address": "Mint",
                "symbol": "AAA",
                "creation_timestamp": 10,
                "open_timestamp": 20,
                "pool": {"exchange": "pump_amm", "creation_timestamp": 30},
            },
            "Mint",
        )
        self.assertEqual(info.creation_timestamp, 10)
        self.assertEqual(info.open_timestamp, 20)
        self.assertEqual(info.pool_created_at, 30)
        self.assertEqual(info.pool_exchange, "pump_amm")


class CollectorCursorTests(unittest.TestCase):
    def test_token_history_follows_cursor_until_null(self):
        pages = {
            None: {"activities": [{"event_type": "buy", "tx_hash": "t1", "timestamp": 300, "token": {"address": "M", "symbol": "S"}, "token_amount": "1", "cost_usd": "1"}], "next": "c1"},
            "c1": {"activities": [{"event_type": "buy", "tx_hash": "t0", "timestamp": 100, "token": {"address": "M", "symbol": "S"}, "token_amount": "2", "cost_usd": "2"}], "next": None},
        }

        class FakeClient:
            def __init__(self):
                self.calls = []

            def get_wallet_activity(self, chain, wallet, token_address=None, cursor=None, limit=50, types=None):
                self.calls.append(cursor)
                return pages[cursor]

        client = FakeClient()
        collector = ActivityCollector(client)  # type: ignore
        trades, page_count, complete = collector.collect_token_history("sol", "W", "M", use_cache=False)
        self.assertEqual(page_count, 2)
        self.assertTrue(complete)
        self.assertEqual({t.tx_hash for t in trades}, {"t1", "t0"})
        self.assertEqual(min(t.timestamp for t in trades if t.event_type.value == "buy"), 100)


class MarketCapParserTests(unittest.TestCase):
    def test_activity_item_reads_top_level_address_as_mint(self):
        from app.api.parsers import parse_activity_item

        item = {
            "event_type": "buy",
            "tx_hash": "txAddr",
            "timestamp": 1757800000,
            "token_amount": "2",
            "cost_usd": "10",
            "address": "MintAddr11111111111111111111111111111111",
            "symbol": "BABA",
        }
        trade = parse_activity_item(item, "WalletOwner1111111111111111111111111111111", "sol")
        self.assertIsNotNone(trade)
        assert trade is not None
        self.assertEqual(trade.token_address, "MintAddr11111111111111111111111111111111")
        self.assertEqual(trade.token_symbol, "BABA")

    def test_activity_item_ignores_wallet_address_field(self):
        from app.api.parsers import parse_activity_item

        wallet = "WalletOwner1111111111111111111111111111111"
        item = {
            "event_type": "buy",
            "tx_hash": "txW",
            "timestamp": 1757800000,
            "address": wallet,
        }
        self.assertIsNone(parse_activity_item(item, wallet, "sol"))

    def test_activity_item_reads_market_cap_directly(self):
        from app.api.parsers import parse_activity_item

        item = {
            "event_type": "buy",
            "tx_hash": "tx1",
            "timestamp": 1757800000,
            "token_amount": "83.89",
            "price_usd": "0.0049",
            "cost_usd": "300.46",
            "market_cap": "411950",
            "token": {"address": "MintAAA", "symbol": "AAA", "name": "Alpha", "total_supply": "84000000"},
        }
        trade = parse_activity_item(item, "Wallet1", "sol")
        self.assertIsNotNone(trade)
        assert trade is not None
        self.assertEqual(trade.market_cap, Decimal("411950"))

    def test_activity_item_falls_back_to_token_market_cap(self):
        from app.api.parsers import parse_activity_item

        item = {
            "event_type": "buy",
            "tx_hash": "tx2",
            "timestamp": 1757800000,
            "token_amount": "10",
            "price_usd": "0.01",
            "cost_usd": "0.1",
            "token": {"address": "MintBBB", "symbol": "BBB", "market_cap": "999000"},
        }
        trade = parse_activity_item(item, "Wallet1", "sol")
        assert trade is not None
        self.assertEqual(trade.market_cap, Decimal("999000"))

    def test_acquisition_prefers_trade_market_cap(self):
        from app.domain.enums import AcquisitionType
        from app.services.acquisition_resolver import resolve_acquisition

        class FakeTrade:
            event_type = None
            timestamp = 0
            tx_hash = "tx1"
            token_amount = Decimal("83.89")
            price_usd = Decimal("0.0049")
            cost_usd = Decimal("300.46")
            cost_sol = None
            gas_usd = Decimal("1.174")
            gas_sol = None
            total_supply_at_trade = Decimal("84000000")
            market_cap = Decimal("411950")
            cost_sol_estimated = False

        buy = FakeTrade()
        buy.event_type = EventType.BUY
        buy.timestamp = 1757800000
        acq = resolve_acquisition([buy], token_info=None, detect_special=False)
        self.assertEqual(acq.acquisition_type, AcquisitionType.BUY)
        self.assertEqual(acq.market_cap, Decimal("411950"))
        self.assertFalse(acq.market_cap_is_estimated)
        self.assertEqual(acq.market_cap_source, "wallet_activity.market_cap")

    def test_transfer_in_with_zero_price_uses_current_mc(self):
        from app.domain.enums import AcquisitionType
        from app.domain.models import TokenInfo
        from app.services.acquisition_resolver import resolve_acquisition

        class FakeTrade:
            event_type = None
            timestamp = 0
            tx_hash = "tx_in"
            token_amount = Decimal("11.87")
            price_usd = Decimal("0")
            cost_usd = Decimal("1790")
            cost_sol = None
            gas_usd = Decimal("0.001")
            gas_sol = None
            total_supply_at_trade = Decimal("1000000000000")
            market_cap = None
            cost_sol_estimated = False

        transfer = FakeTrade()
        transfer.event_type = EventType.TRANSFER_IN
        transfer.timestamp = 1757800000
        info = TokenInfo(
            token_address="MintSTONK",
            chain="sol",
            symbol="STONK",
            name="STONK",
            total_supply=Decimal("1000000000000"),
            market_cap=Decimal("150970000000"),
        )
        acq = resolve_acquisition([transfer], token_info=info, detect_special=False)
        self.assertEqual(acq.acquisition_type, AcquisitionType.TRANSFER_IN)
        self.assertEqual(acq.market_cap, Decimal("150970000000"))
        self.assertTrue(acq.market_cap_is_estimated)
        self.assertEqual(acq.market_cap_source, "token_info.current_market_cap")

    def test_token_info_parses_current_market_cap(self):
        info = parse_token_info(
            {
                "address": "MintX",
                "symbol": "X",
                "name": "X",
                "total_supply": "1000000000",
                "market_cap": "151000000000",
                "fdv": "151200000000",
            },
            "MintX",
            "sol",
        )
        self.assertEqual(info.market_cap, Decimal("151000000000"))
        self.assertEqual(info.fdv, Decimal("151200000000"))


if __name__ == "__main__":
    unittest.main()
