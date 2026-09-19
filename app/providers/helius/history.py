from __future__ import annotations

from typing import Optional

from app.providers.helius.models import TokenEvents, WalletHistoryIndex
from app.providers.helius.parsers import classify_token_event, parse_history_tx
from app.providers.solana.rpc_client import SolanaRpcProvider
from app.utils.logger import get_logger

logger = get_logger("gmgn.helius.history")
QUOTE = {"SOL", "So11111111111111111111111111111111111111112", "So11111111111111111111111111111111111111111"}
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
QUOTE |= {USDC, USDT}


def build_index(wallet: str, txs, source: str = "helius", pages: int = 0, bottom_complete: bool = False) -> WalletHistoryIndex:
    index = WalletHistoryIndex(wallet=wallet, transactions=list(txs), pages=pages, source=source, bottom_complete=bottom_complete)
    for tx in txs:
        mints = {c.mint for c in tx.balance_changes if c.mint and c.mint not in QUOTE}
        for mint in mints:
            bucket = index.token_events.setdefault(mint, TokenEvents(mint=mint))
            kind = classify_token_event(tx, mint)
            if kind == "buy":
                bucket.swaps.append(tx)
                if bucket.earliest_buy is None or (tx.timestamp and tx.timestamp < (bucket.earliest_buy.timestamp or 10**18)):
                    bucket.earliest_buy = tx
            elif kind == "sell":
                bucket.swaps.append(tx)
            elif kind == "transfer_in":
                bucket.transfers_in.append(tx)
            elif kind == "transfer_out":
                bucket.transfers_out.append(tx)
            if bucket.earliest_activity is None or (tx.timestamp and tx.timestamp < (bucket.earliest_activity.timestamp or 10**18)):
                bucket.earliest_activity = tx
            if bucket.latest_activity is None or (tx.timestamp and tx.timestamp > (bucket.latest_activity.timestamp or 0)):
                bucket.latest_activity = tx
    return index


def collect_rpc_fallback(
    rpc: SolanaRpcProvider,
    wallet: str,
    *,
    start_ts: int = 0,
    end_ts: int = 0,
    max_signatures: int = 800,
    max_tx_verify: int = 400,
) -> WalletHistoryIndex:
    """Two-phase: wallet signatures in report window, then limited ATA first-activity."""
    sigs = _collect_wallet_signatures(rpc, wallet, start_ts=start_ts, end_ts=end_ts, limit=max_signatures)
    pages = max(1, (len(sigs) + 999) // 1000) if sigs else 1
    txs = []
    seen = set()
    for item in sigs[:max_tx_verify]:
        signature = str(item.get("signature") or "")
        if not signature or signature in seen:
            continue
        seen.add(signature)
        raw = rpc.get_transaction(signature)
        parsed = _tx_from_rpc(raw, wallet, signature, item.get("blockTime"))
        if parsed:
            txs.append(parsed)
    accounts = rpc.get_token_accounts_by_owner(wallet, None)
    atas = []
    for acc in accounts[:120]:
        pubkey = acc.get("pubkey") if isinstance(acc, dict) else None
        if pubkey:
            atas.append(str(pubkey))
    extra_sigs = []
    for ata in atas[:60]:
        extra_sigs.extend(rpc.get_signatures_for_address(ata, limit=30))
        if len(extra_sigs) >= 600:
            break
    extra_budget = max(80, max_tx_verify)
    for item in extra_sigs[:extra_budget]:
        signature = str(item.get("signature") or "")
        if not signature or signature in seen:
            continue
        seen.add(signature)
        raw = rpc.get_transaction(signature)
        parsed = _tx_from_rpc(raw, wallet, signature, item.get("blockTime"))
        if parsed:
            txs.append(parsed)
    return build_index(wallet, txs, source="solana_rpc", pages=pages, bottom_complete=False)


def recover_mint_history(
    rpc: SolanaRpcProvider,
    wallet: str,
    mint: str,
    *,
    max_sigs: int = 400,
    max_tx_verify: int = 60,
) -> list:
    """Pull ATA signatures for one mint and decode oldest+newest txs (first-buy recovery)."""
    if not mint:
        return []
    accounts = rpc.get_token_accounts_by_owner(wallet, mint)
    atas: list[str] = []
    for acc in accounts:
        pubkey = acc.get("pubkey") if isinstance(acc, dict) else None
        if pubkey:
            atas.append(str(pubkey))
    if not atas:
        return []
    sigs: list[dict] = []
    seen: set[str] = set()
    for ata in atas[:4]:
        before = None
        while len(sigs) < max_sigs:
            batch = rpc.get_signatures_for_address(ata, limit=min(100, max_sigs - len(sigs)), before=before)
            if not batch:
                break
            for item in batch:
                signature = str(item.get("signature") or "")
                if not signature or signature in seen:
                    continue
                seen.add(signature)
                sigs.append(item)
            if len(batch) < 100:
                break
            before = batch[-1].get("signature")
    if not sigs:
        return []
    newest_n = min(20, max_tx_verify // 3)
    oldest_n = max_tx_verify - newest_n
    if len(sigs) <= max_tx_verify:
        selected = sigs
    else:
        selected = list(sigs[:newest_n]) + list(sigs[-oldest_n:])
    txs = []
    verified = set()
    for item in selected:
        signature = str(item.get("signature") or "")
        if not signature or signature in verified:
            continue
        verified.add(signature)
        raw = rpc.get_transaction(signature)
        parsed = _tx_from_rpc(raw, wallet, signature, item.get("blockTime"))
        if parsed:
            txs.append(parsed)
    logger.info("mint ATA recover wallet=%s mint=%s atas=%s sigs=%s txs=%s", wallet[:8], mint[:8], len(atas), len(sigs), len(txs))
    return txs


def _collect_wallet_signatures(rpc: SolanaRpcProvider, wallet: str, start_ts: int, end_ts: int, limit: int) -> list[dict]:
    out: list[dict] = []
    before = None
    while len(out) < limit:
        batch = rpc.get_signatures_for_address(wallet, limit=min(1000, limit - len(out)), before=before)
        if not batch:
            break
        for item in batch:
            ts = int(item.get("blockTime") or 0)
            if end_ts and ts and ts > end_ts:
                continue
            if start_ts and ts and ts < start_ts:
                return out
            out.append(item)
        if len(batch) < 1000:
            break
        before = batch[-1].get("signature")
    return out


def _tx_from_rpc(raw, wallet: str, signature: str, block_time) -> Optional[object]:
    from collections import defaultdict
    from decimal import Decimal

    from app.providers.solana.transaction_parser import parse_verified_transaction

    verified = parse_verified_transaction(raw, wallet, "", signature)
    if not verified.found:
        return None
    payload = {
        "signature": signature,
        "timestamp": verified.block_time or block_time or 0,
        "slot": verified.slot,
        "fee": float(verified.fee_sol) if verified.fee_sol is not None else None,
        "error": None if verified.success else "failed",
        "type": "SWAP",
        "balanceChanges": [],
    }
    meta = (raw or {}).get("meta") or {}
    pre_sum: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    post_sum: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    for row in meta.get("preTokenBalances") or []:
        if not isinstance(row, dict):
            continue
        owner = str(row.get("owner") or "")
        if wallet and owner and owner != wallet:
            continue
        mint = str(row.get("mint") or "")
        ui = ((row.get("uiTokenAmount") or {}) if isinstance(row.get("uiTokenAmount"), dict) else {}).get("uiAmount")
        if mint:
            pre_sum[mint] += Decimal(str(ui or 0))
    for row in meta.get("postTokenBalances") or []:
        if not isinstance(row, dict):
            continue
        owner = str(row.get("owner") or "")
        if wallet and owner and owner != wallet:
            continue
        mint = str(row.get("mint") or "")
        ui = ((row.get("uiTokenAmount") or {}) if isinstance(row.get("uiTokenAmount"), dict) else {}).get("uiAmount")
        if mint:
            post_sum[mint] += Decimal(str(ui or 0))
    for mint in set(pre_sum) | set(post_sum):
        delta = post_sum[mint] - pre_sum[mint]
        if delta != 0:
            payload["balanceChanges"].append({"mint": mint, "amount": str(delta), "decimals": 0})
    if verified.wallet_sol_delta is not None and verified.wallet_sol_delta != 0:
        payload["balanceChanges"].append({"mint": "SOL", "amount": str(verified.wallet_sol_delta), "decimals": 9})
    return parse_history_tx(payload)
