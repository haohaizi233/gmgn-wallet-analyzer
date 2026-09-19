from __future__ import annotations

import json
import os
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock

import requests

from app.domain.enums import ReportPeriod, TaskStatus
from app.domain.models import AnalysisOptions, WalletAnalysisRequest
from app.providers.base import Clock, ProviderCircuitBreaker
from app.providers.dexscreener.client import DexScreenerProvider
from app.providers.exceptions import ProviderError
from app.providers.gmgn.adapter import GMGNVerifierProvider
from app.providers.helius.client import HeliusProvider
from app.providers.helius.models import BalanceChange, HistoryTx
from app.providers.helius.parsers import classify_token_event, history_to_swaps
from app.providers.history_cache import merge_history_state, merge_swaps
from app.providers.moralis.client import MoralisProvider
from app.providers.moralis.models import SwapLeg, WalletSwap
from app.providers.orchestrator import DataOrchestrator
from app.providers.solana.rpc_client import SolanaRpcProvider
from app.providers.solana.transaction_parser import parse_verified_transaction
from app.services.data_quality_gate import DataQualityGate
from app.services.wallet_analysis_service import WalletAnalysisService, _in_report_window
from app.storage.database import Database
from app.providers.result import HistoryCoverage
from app.domain.enums import DataSource


class FakeClock(Clock):
    def __init__(self) -> None:
        self.t = 0.0

    def monotonic(self) -> float:
        return self.t

    def time(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds


USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
WALLET = "7EcDhSYGxXyscszYEp35KHN8vvw3svAuLKTzXwCFLtV"


def _dex() -> DexScreenerProvider:
    session = MagicMock()
    session.request.side_effect = lambda *a, **k: _resp([])
    dex = DexScreenerProvider(session=session)
    dex._shared_session = session
    return dex


def _resp(payload, status=200):
    resp = MagicMock()
    resp.status_code = status
    resp.headers = {}
    resp.json.return_value = payload
    resp.content = json.dumps(payload).encode()
    return resp


def _rpc_empty_session():
    session = MagicMock()

    def request(method, url, params=None, json=None, headers=None, timeout=None, **kwargs):
        body = json or {}
        m = body.get("method")
        if m == "getSignaturesForAddress":
            payload = {"jsonrpc": "2.0", "result": []}
        elif m == "getTokenAccountsByOwner":
            payload = {"jsonrpc": "2.0", "result": {"value": []}}
        else:
            payload = {"jsonrpc": "2.0", "result": None}
        return _resp(payload)

    session.request.side_effect = request
    return session


def _rpc_ok_session(sig="rpcSig"):
    session = MagicMock()

    def request(method, url, params=None, json=None, headers=None, timeout=None, **kwargs):
        body = json or {}
        m = body.get("method")
        if m == "getSignaturesForAddress":
            return _resp({"jsonrpc": "2.0", "result": [{"signature": sig, "blockTime": 1_700_000_000}]})
        if m == "getTransaction":
            return _resp(
                {
                    "jsonrpc": "2.0",
                    "result": {
                        "blockTime": 1_700_000_000,
                        "meta": {
                            "fee": 5000,
                            "preBalances": [1_000_000_000],
                            "postBalances": [990_000_000],
                            "preTokenBalances": [],
                            "postTokenBalances": [
                                {
                                    "mint": "MintAAA1111111111111111111111111111111111111",
                                    "owner": WALLET,
                                    "uiTokenAmount": {"uiAmount": 10, "amount": "10", "decimals": 0},
                                }
                            ],
                        },
                        "transaction": {"message": {"accountKeys": [WALLET], "instructions": []}},
                    },
                }
            )
        if m == "getTokenAccountsByOwner":
            return _resp({"jsonrpc": "2.0", "result": {"value": []}})
        return _resp({"jsonrpc": "2.0", "result": None})

    session.request.side_effect = request
    return session


class DummyGMGN:
    def __init__(self) -> None:
        self.api_key = "k"
        self.request_count = 0
        self.retry_429_count = 0
        self.error_count = 0
        self.on_raw = None
        self.activity_calls = 0

    def get_wallet_stats(self, *args, **kwargs):
        return {}

    def get_wallet_profits(self, *args, **kwargs):
        return {}

    def get_token_info(self, *args, **kwargs):
        return {}

    def get_token_pool_info(self, *args, **kwargs):
        return {}

    def get_wallet_activity(self, *args, **kwargs):
        self.activity_calls += 1
        return {"activities": []}


class MoralisRetryTests(unittest.TestCase):
    def test_10054_retries_then_succeeds_no_fallback(self):
        calls = {"n": 0}
        session = MagicMock()

        def request(method, url, params=None, json=None, headers=None, timeout=None, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise requests.exceptions.ConnectionError("10054 remote host closed")
            return _resp({"result": [], "cursor": None})

        session.request.side_effect = request
        moralis = MoralisProvider("mk", session=session, enabled_flag=True)
        moralis._shared_session = session
        moralis.circuit = ProviderCircuitBreaker(clock=FakeClock(), open_seconds=30)
        rpc = SolanaRpcProvider(session=_rpc_empty_session())
        rpc._shared_session = rpc._shared_session
        orch = DataOrchestrator(
            [
                moralis,
                rpc,
                HeliusProvider(""),
                _dex(),
                GMGNVerifierProvider(DummyGMGN(), deep_history=False),
            ]
        )
        index = orch.collect_wallet_swaps(WALLET, start_ts=1, end_ts=2)
        self.assertEqual(calls["n"], 2)
        self.assertTrue(index.success)
        self.assertTrue(index.coverage.verified_empty)
        self.assertEqual(index.provider, "moralis")
        self.assertFalse(index.fallback_used)

    def test_three_network_failures_use_rpc_fallback(self):
        session = MagicMock()

        def request(method, url, params=None, json=None, headers=None, timeout=None, **kwargs):
            raise requests.exceptions.ConnectionError("10054")

        session.request.side_effect = request
        moralis = MoralisProvider("mk", session=session, enabled_flag=True)
        moralis._shared_session = session
        moralis.circuit = ProviderCircuitBreaker(clock=FakeClock(), open_seconds=30)
        rpc_session = _rpc_ok_session()
        rpc = SolanaRpcProvider(session=rpc_session)
        rpc._shared_session = rpc_session
        orch = DataOrchestrator(
            [
                moralis,
                rpc,
                HeliusProvider(""),
                _dex(),
                GMGNVerifierProvider(DummyGMGN(), deep_history=False),
            ]
        )
        index = orch.collect_wallet_swaps(WALLET)
        self.assertGreaterEqual(moralis.metrics.network_error, 3)
        self.assertEqual(index.provider, "solana_rpc")
        self.assertTrue(index.fallback_used)
        self.assertTrue(index.by_token or index.swaps is not None)


class EmptySemanticsTests(unittest.TestCase):
    def test_false_empty_is_not_success(self):
        moralis_session = MagicMock()
        moralis_session.request.side_effect = lambda *a, **k: (_ for _ in ()).throw(requests.exceptions.ConnectionError("10054"))
        moralis = MoralisProvider("mk", session=moralis_session, enabled_flag=True)
        moralis._shared_session = moralis_session
        moralis.circuit = ProviderCircuitBreaker(clock=FakeClock(), open_seconds=30)
        rpc = SolanaRpcProvider(session=_rpc_empty_session())
        rpc._shared_session = rpc._shared_session
        gmgn = DummyGMGN()
        orch = DataOrchestrator(
            [
                moralis,
                rpc,
                HeliusProvider(""),
                _dex(),
                GMGNVerifierProvider(gmgn, deep_history=False),
            ]
        )
        fd, name = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        svc = WalletAnalysisService(gmgn, db=Database(path=Path(name)), orchestrator=orch, deep_gmgn_history=False)
        req = WalletAnalysisRequest(
            wallet_address=WALLET,
            period=ReportPeriod.D7,
            start_time=1,
            end_time=2_000_000_000,
            options=AnalysisOptions(save_raw_json=False),
        )
        report = svc.analyze(req)
        self.assertNotEqual(report.status, TaskStatus.SUCCESS)
        self.assertEqual(len(report.tokens), 0)
        self.assertEqual(gmgn.activity_calls, 0)

    def test_verified_empty_success_tokens_zero(self):
        session = MagicMock()
        session.request.side_effect = lambda *a, **k: _resp({"result": [], "cursor": None})
        moralis = MoralisProvider("mk", session=session, enabled_flag=True)
        moralis._shared_session = session
        orch = DataOrchestrator(
            [
                moralis,
                SolanaRpcProvider(session=_rpc_empty_session()),
                HeliusProvider(""),
                _dex(),
                GMGNVerifierProvider(DummyGMGN(), deep_history=False),
            ]
        )
        fd, name = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        svc = WalletAnalysisService(DummyGMGN(), db=Database(path=Path(name)), orchestrator=orch)
        req = WalletAnalysisRequest(
            wallet_address=WALLET,
            period=ReportPeriod.D7,
            start_time=1,
            end_time=2_000_000_000,
            options=AnalysisOptions(save_raw_json=False),
        )
        report = svc.analyze(req)
        self.assertEqual(report.status, TaskStatus.SUCCESS)
        self.assertEqual(len(report.tokens), 0)
        self.assertEqual(report.scope.verified_empty, "是")


class DirectionAndQuoteTests(unittest.TestCase):
    def test_sell_token_negative_usdc_positive(self):
        tx = HistoryTx(
            signature="sell1",
            timestamp=10,
            tx_type="SWAP",
            balance_changes=[
                BalanceChange(mint="TokenA", amount=Decimal("-10"), decimals=6),
                BalanceChange(mint=USDC, amount=Decimal("100"), decimals=6),
            ],
        )
        self.assertEqual(classify_token_event(tx, "TokenA"), "sell")
        swaps = history_to_swaps(WALLET, [tx])
        self.assertEqual(swaps[0].transaction_type, "sell")
        self.assertEqual(swaps[0].bought.symbol, "USDC")
        self.assertNotEqual((swaps[0].bought.symbol or "").upper(), "SOL")

    def test_usdc_quote_not_collapsed_to_sol(self):
        tx = HistoryTx(
            signature="buy1",
            timestamp=10,
            tx_type="SWAP",
            balance_changes=[
                BalanceChange(mint="TokenA", amount=Decimal("100"), decimals=6),
                BalanceChange(mint=USDC, amount=Decimal("-50"), decimals=6),
            ],
        )
        swaps = history_to_swaps(WALLET, [tx])
        self.assertEqual(swaps[0].transaction_type, "buy")
        self.assertEqual(swaps[0].sold.symbol, "USDC")
        self.assertEqual(swaps[0].total_value_usd, Decimal("50"))

    def test_jupiter_wrap_leftover_wsol_is_buy(self):
        wsol = "So11111111111111111111111111111111111111112"
        tx = HistoryTx(
            signature="jup1",
            timestamp=10,
            tx_type="SWAP",
            balance_changes=[
                BalanceChange(mint="TokenA", amount=Decimal("100"), decimals=6),
                BalanceChange(mint="SOL", amount=Decimal("-0.2"), decimals=9),
                BalanceChange(mint=wsol, amount=Decimal("1.5"), decimals=9),
            ],
        )
        self.assertEqual(classify_token_event(tx, "TokenA"), "buy")
        swaps = history_to_swaps(WALLET, [tx])
        self.assertEqual(len(swaps), 1)
        self.assertEqual(swaps[0].transaction_type, "buy")
        self.assertEqual(swaps[0].sold.symbol, "SOL")


class WarmCacheTests(unittest.TestCase):
    def test_merge_keeps_old_and_new(self):
        old1 = WalletSwap("old1", "buy", 1, WALLET, bought=SwapLeg(address="A"))
        old2 = WalletSwap("old2", "buy", 2, WALLET, bought=SwapLeg(address="A"))
        new3 = WalletSwap("new3", "buy", 3, WALLET, bought=SwapLeg(address="A"))
        merged = merge_swaps([new3], [old1, old2])
        self.assertEqual({s.transaction_hash for s in merged}, {"old1", "old2", "new3"})

    def test_history_state_oldest_not_overwritten(self):
        prev = {
            "oldest_signature": "A",
            "oldest_block_time": 100,
            "newest_signature": "B",
            "newest_block_time": 200,
            "bottom_complete": True,
        }
        merged = merge_history_state(prev, [("C", 300)], True)
        self.assertEqual(merged["oldest_signature"], "A")
        self.assertEqual(merged["newest_signature"], "C")
        self.assertTrue(merged["bottom_complete"])


class ReportWindowTests(unittest.TestCase):
    def test_7d_filter_and_lifetime_first_buy(self):
        now = 1_800_000_000
        old_ts = now - 100 * 86400
        new_ts = now - 3 * 86400
        self.assertFalse(_in_report_window(old_ts, now - 7 * 86400, now))
        self.assertTrue(_in_report_window(new_ts, now - 7 * 86400, now))
        old = WalletSwap("oldBuy", "buy", old_ts, WALLET, bought=SwapLeg(address="TokenA", amount=Decimal("1")))
        new = WalletSwap("newSell", "sell", new_ts, WALLET, sold=SwapLeg(address="TokenA", amount=Decimal("1")), bought=SwapLeg(address=USDC, symbol="USDC", amount=Decimal("5")))
        from app.providers.moralis.models import TokenSwapIndex, WalletSwapIndex
        from app.providers.orchestrator import _index_swaps

        swaps = [old, new]
        index = WalletSwapIndex(wallet=WALLET, swaps=swaps, pages=1, by_token=_index_swaps(WALLET, swaps), success=True, complete=True)
        index.coverage = HistoryCoverage(complete=True, verified_empty=False, provider=DataSource.MORALIS, termination_reason="complete")
        moralis = MoralisProvider("mk", enabled_flag=True)
        moralis.enabled = True
        orch = DataOrchestrator([moralis, SolanaRpcProvider(session=_rpc_empty_session()), HeliusProvider(""), _dex(), GMGNVerifierProvider(DummyGMGN())])
        orch.collect_wallet_swaps = lambda *a, **k: index
        fd, name = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        svc = WalletAnalysisService(DummyGMGN(), db=Database(path=Path(name)), orchestrator=orch)
        req = WalletAnalysisRequest(
            wallet_address=WALLET,
            period=ReportPeriod.D7,
            start_time=now - 7 * 86400,
            end_time=now,
            options=AnalysisOptions(save_raw_json=False, fetch_token_created_at=False, fetch_platform_pool=False),
        )
        report = svc.analyze(req)
        self.assertEqual(len(report.tokens), 1)
        report_hashes = {t.tx_hash for t in report.trades if not t.history_only}
        self.assertIn("newSell", report_hashes)
        self.assertNotIn("oldBuy", {t.tx_hash for t in report.trades if t.in_report_range and t.event_type.value.lower() == "buy"})


class MultiAtaTests(unittest.TestCase):
    def test_multiple_token_accounts_sum_delta(self):
        payload = {
            "blockTime": 10,
            "meta": {
                "fee": 5000,
                "preTokenBalances": [],
                "postTokenBalances": [
                    {"mint": "MintA", "owner": WALLET, "uiTokenAmount": {"uiAmount": 5, "amount": "5", "decimals": 0}},
                    {"mint": "MintA", "owner": WALLET, "uiTokenAmount": {"uiAmount": 3, "amount": "3", "decimals": 0}},
                ],
                "preBalances": [1, 1],
                "postBalances": [1, 1],
            },
            "transaction": {"message": {"accountKeys": [WALLET], "instructions": []}},
        }
        parsed = parse_verified_transaction(payload, WALLET, "MintA", "sig")
        self.assertEqual(parsed.token_delta, Decimal("8"))


class RoutingTests(unittest.TestCase):
    def test_moralis_then_rpc_not_gmgn_history(self):
        session = MagicMock()
        session.request.side_effect = lambda *a, **k: (_ for _ in ()).throw(requests.exceptions.ConnectionError("10054"))
        moralis = MoralisProvider("mk", session=session, enabled_flag=True)
        moralis._shared_session = session
        moralis.circuit = ProviderCircuitBreaker(clock=FakeClock(), open_seconds=30)
        gmgn = DummyGMGN()
        rpc_session = _rpc_ok_session()
        rpc = SolanaRpcProvider(session=rpc_session)
        rpc._shared_session = rpc_session
        orch = DataOrchestrator(
            [
                moralis,
                rpc,
                HeliusProvider(""),
                _dex(),
                GMGNVerifierProvider(gmgn, deep_history=False),
            ]
        )
        index = orch.collect_wallet_swaps(WALLET)
        self.assertEqual(index.provider, "solana_rpc")
        self.assertEqual(gmgn.activity_calls, 0)

    def test_helius_missing_key_still_runs(self):
        session = MagicMock()
        session.request.side_effect = lambda *a, **k: _resp({"result": [{"transactionHash": "t1", "transactionType": "buy", "blockTimestamp": 10, "bought": {"address": "MintA", "amount": "1"}, "sold": {"address": USDC, "symbol": "USDC", "amount": "2"}}], "cursor": None})
        moralis = MoralisProvider("mk", session=session, enabled_flag=True)
        moralis._shared_session = session
        helius = HeliusProvider("")
        self.assertFalse(helius.enabled)
        orch = DataOrchestrator(
            [
                moralis,
                SolanaRpcProvider(session=_rpc_empty_session()),
                helius,
                _dex(),
                GMGNVerifierProvider(DummyGMGN(), deep_history=False),
            ]
        )
        index = orch.collect_wallet_swaps(WALLET)
        self.assertEqual(index.provider, "moralis")
        self.assertTrue(index.success)

    def test_gmgn_429_does_not_fail_moralis_success(self):
        from app.api.exceptions import GMGNRateLimitError

        class RateGMGN(DummyGMGN):
            def get_wallet_stats(self, *args, **kwargs):
                raise GMGNRateLimitError("429")

            def get_wallet_profits(self, *args, **kwargs):
                raise GMGNRateLimitError("429")

        session = MagicMock()
        session.request.side_effect = lambda *a, **k: _resp({"result": [{"transactionHash": "t1", "transactionType": "buy", "blockTimestamp": 10, "bought": {"address": "MintA", "amount": "1"}, "sold": {"symbol": "USDC", "address": USDC, "amount": "2"}}], "cursor": None})
        moralis = MoralisProvider("mk", session=session, enabled_flag=True)
        moralis._shared_session = session
        orch = DataOrchestrator(
            [
                moralis,
                SolanaRpcProvider(session=_rpc_empty_session()),
                HeliusProvider(""),
                _dex(),
                GMGNVerifierProvider(RateGMGN(), deep_history=False),
            ]
        )
        fd, name = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        svc = WalletAnalysisService(RateGMGN(), db=Database(path=Path(name)), orchestrator=orch)
        req = WalletAnalysisRequest(
            wallet_address=WALLET,
            period=ReportPeriod.D7,
            start_time=1,
            end_time=2_000_000_000,
            options=AnalysisOptions(save_raw_json=False, fetch_token_created_at=False, fetch_platform_pool=False),
        )
        report = svc.analyze(req)
        self.assertIn(report.status, {TaskStatus.SUCCESS, TaskStatus.PARTIAL})
        self.assertGreaterEqual(len(report.tokens), 1)

    def test_priority_order_is_moralis_rpc_helius(self):
        from app.domain.enums import ProviderCapability
        from app.providers.priority import DEFAULT_PRIORITY

        self.assertEqual(DEFAULT_PRIORITY[ProviderCapability.WALLET_SWAPS][:3], ["moralis", "solana_rpc", "helius"])

    def test_quality_gate_unknown_empty_failed(self):
        cov = HistoryCoverage(complete=False, verified_empty=False, termination_reason="unknown_empty")
        self.assertEqual(DataQualityGate.evaluate(cov, token_count=0), TaskStatus.FAILED)

    def test_quality_gate_verified_empty_success(self):
        cov = HistoryCoverage(complete=True, verified_empty=True, termination_reason="verified_empty")
        self.assertEqual(DataQualityGate.evaluate(cov, token_count=0), TaskStatus.SUCCESS)


if __name__ == "__main__":
    unittest.main()
