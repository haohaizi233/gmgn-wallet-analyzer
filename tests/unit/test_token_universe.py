from __future__ import annotations

import unittest
from decimal import Decimal

from app.domain.enums import EventType
from app.domain.models import TradeRecord
from app.providers.helius.history import recover_mint_history
from app.providers.helius.parsers import classify_token_event, history_to_swaps
from app.providers.helius.models import BalanceChange, HistoryTx
from app.providers.token_universe import (
    extract_mints_from_gmgn_holdings,
    extract_mints_from_helius_balances,
    extract_mints_from_token_accounts,
    is_quote_mint,
    merge_token_universe,
    trade_to_swap,
)

WALLET = "WalletOwner1111111111111111111111111111111"
MINT = "MintAAA11111111111111111111111111111111111"
WSOL = "So11111111111111111111111111111111111111112"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


def _account(mint: str, amount: str, pubkey: str = "ATA1") -> dict:
    return {
        "pubkey": pubkey,
        "account": {
            "data": {
                "parsed": {
                    "info": {
                        "mint": mint,
                        "tokenAmount": {"uiAmount": float(amount), "uiAmountString": amount},
                    }
                }
            }
        },
    }


class UniverseParseTests(unittest.TestCase):
    def test_token_accounts_skip_quotes_and_keep_zero(self):
        rows = [
            _account(MINT, "0"),
            _account(WSOL, "2"),
            _account(USDC, "10"),
            _account("MintBBB", "1.5", pubkey="ATA2"),
        ]
        mints = extract_mints_from_token_accounts(rows, include_zero=True)
        self.assertEqual(mints, [MINT, "MintBBB"])
        self.assertEqual(extract_mints_from_token_accounts(rows, include_zero=False), ["MintBBB"])

    def test_helius_balances_and_holdings(self):
        helius = {"tokens": [{"mint": MINT}, {"mint": WSOL}, {"tokenAddress": "MintCCC"}]}
        self.assertEqual(extract_mints_from_helius_balances(helius), [MINT, "MintCCC"])
        payload = {
            "data": {
                "list": [
                    {"token": {"address": MINT, "symbol": "AAA"}},
                    {"token_address": "MintDDD"},
                    {"address": USDC},
                ],
                "next": "cur2",
            }
        }
        mints, cursor = extract_mints_from_gmgn_holdings(payload)
        self.assertEqual(mints, [MINT, "MintDDD"])
        self.assertEqual(cursor, "cur2")

    def test_merge_preserves_order_and_limit(self):
        merged = merge_token_universe([MINT, WSOL, "A"], ["A", "B"], limit=3)
        self.assertEqual(merged, [MINT, "A", "B"])
        self.assertTrue(is_quote_mint(USDC))
        self.assertFalse(is_quote_mint(MINT))


class JupiterClassifyTests(unittest.TestCase):
    def test_wrap_leftover_not_dropped(self):
        tx = HistoryTx(
            signature="jup-wrap",
            timestamp=50,
            balance_changes=[
                BalanceChange(mint=MINT, amount=Decimal("12")),
                BalanceChange(mint="SOL", amount=Decimal("-0.05")),
                BalanceChange(mint=WSOL, amount=Decimal("2")),
            ],
        )
        self.assertEqual(classify_token_event(tx, MINT), "buy")
        swaps = history_to_swaps(WALLET, [tx])
        self.assertEqual(len(swaps), 1)
        self.assertEqual(swaps[0].transaction_type, "buy")

    def test_plain_transfer_in_stays_transfer(self):
        tx = HistoryTx(
            signature="xfer",
            timestamp=50,
            balance_changes=[BalanceChange(mint=MINT, amount=Decimal("3"))],
        )
        self.assertEqual(classify_token_event(tx, MINT), "transfer_in")


class RecoverMintTests(unittest.TestCase):
    def test_recover_decodes_ata_buy(self):
        class FakeRpc:
            def get_token_accounts_by_owner(self, owner, mint=None):
                return [{"pubkey": "ATA1"}]

            def get_signatures_for_address(self, address, limit=100, before=None, until=None):
                return [{"signature": "sigBuy", "blockTime": 99}]

            def get_transaction(self, signature):
                return {
                    "blockTime": 99,
                    "slot": 8,
                    "meta": {
                        "err": None,
                        "fee": 5000,
                        "preBalances": [10**9],
                        "postBalances": [10**9 - 10**8],
                        "preTokenBalances": [],
                        "postTokenBalances": [
                            {
                                "owner": WALLET,
                                "mint": MINT,
                                "uiTokenAmount": {"uiAmount": 4, "uiAmountString": "4", "amount": "4", "decimals": 0},
                            }
                        ],
                    },
                    "transaction": {"message": {"accountKeys": [WALLET]}},
                }

        txs = recover_mint_history(FakeRpc(), WALLET, MINT)
        self.assertEqual(len(txs), 1)
        self.assertEqual(classify_token_event(txs[0], MINT), "buy")
        swaps = history_to_swaps(WALLET, txs)
        self.assertEqual(swaps[0].transaction_type, "buy")

    def test_trade_to_swap_keeps_buy(self):
        trade = TradeRecord(
            wallet_address=WALLET,
            chain="sol",
            token_address=MINT,
            token_symbol="AAA",
            token_name="AAA",
            tx_hash="t1",
            timestamp=10,
            event_type=EventType.BUY,
            token_amount=Decimal("1"),
            price_usd=Decimal("2"),
            cost_usd=Decimal("2"),
            cost_sol=Decimal("0.01"),
            gas_usd=None,
            gas_sol=None,
            launchpad_platform=None,
        )
        swap = trade_to_swap(trade)
        self.assertEqual(swap.transaction_type, "buy")
        self.assertEqual(swap.token_address, MINT)


if __name__ == "__main__":
    unittest.main()
