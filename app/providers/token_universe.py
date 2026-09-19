from __future__ import annotations

from typing import Any, Iterable

from app.domain.enums import DataSource, EventType
from app.domain.models import TradeRecord
from app.providers.helius.parsers import QUOTE_MINTS, SOL_ALIASES
from app.providers.moralis.models import SwapLeg, WalletSwap
from app.providers.solana.transaction_parser import WSOL_MINT
from app.utils.money import to_decimal

MAX_UNIVERSE_TOKENS = 400
HOLDINGS_MAX_PAGES = 8


def is_quote_mint(mint: str) -> bool:
    text = str(mint or "").strip()
    if not text:
        return True
    if text.upper() == "SOL":
        return True
    return text in QUOTE_MINTS or text in SOL_ALIASES


def extract_mints_from_token_accounts(accounts: Any, *, include_zero: bool = True) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    rows = accounts if isinstance(accounts, list) else []
    for acc in rows:
        if not isinstance(acc, dict):
            continue
        parsed = (((acc.get("account") or {}).get("data") or {}).get("parsed") or {}).get("info") or {}
        if not isinstance(parsed, dict):
            continue
        mint = str(parsed.get("mint") or "").strip()
        if not mint or mint in seen or is_quote_mint(mint):
            continue
        tok = parsed.get("tokenAmount") if isinstance(parsed.get("tokenAmount"), dict) else {}
        amount = to_decimal(tok.get("uiAmount") if tok.get("uiAmount") is not None else tok.get("uiAmountString"))
        if not include_zero and (amount is None or amount == 0):
            continue
        seen.add(mint)
        out.append(mint)
    return out


def extract_mints_from_helius_balances(payload: Any) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    data = payload if isinstance(payload, dict) else {}
    tokens = data.get("tokens")
    if not isinstance(tokens, list):
        tokens = data.get("tokenBalances") if isinstance(data.get("tokenBalances"), list) else []
    for row in tokens:
        if not isinstance(row, dict):
            continue
        mint = str(row.get("mint") or row.get("tokenAddress") or row.get("token_address") or "").strip()
        if not mint or mint in seen or is_quote_mint(mint):
            continue
        seen.add(mint)
        out.append(mint)
    return out


def extract_mints_from_gmgn_holdings(payload: Any) -> tuple[list[str], str]:
    data = payload
    if isinstance(payload, dict) and isinstance(payload.get("data"), (dict, list)):
        data = payload.get("data")
    rows: list[Any] = []
    cursor = ""
    if isinstance(data, list):
        rows = data
    elif isinstance(data, dict):
        for key in ("list", "holdings", "tokens", "items"):
            if isinstance(data.get(key), list):
                rows = data.get(key) or []
                break
        cursor = str(data.get("next") or data.get("cursor") or data.get("next_cursor") or "").strip()
    out: list[str] = []
    seen: set[str] = set()
    for row in rows:
        mint = _holding_mint(row)
        if not mint or mint in seen or is_quote_mint(mint):
            continue
        seen.add(mint)
        out.append(mint)
    return out, cursor


def _holding_mint(row: Any) -> str:
    if not isinstance(row, dict):
        return ""
    token = row.get("token") if isinstance(row.get("token"), dict) else {}
    return str(
        token.get("address")
        or token.get("token_address")
        or row.get("token_address")
        or row.get("address")
        or row.get("mint")
        or ""
    ).strip()


def merge_token_universe(*groups: Iterable[str], limit: int = MAX_UNIVERSE_TOKENS) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for group in groups:
        for mint in group or []:
            text = str(mint or "").strip()
            if not text or text in seen or is_quote_mint(text):
                continue
            seen.add(text)
            out.append(text)
            if len(out) >= limit:
                return out
    return out


def trade_to_swap(trade: TradeRecord) -> WalletSwap:
    kind_map = {
        EventType.BUY: "buy",
        EventType.SELL: "sell",
        EventType.TRANSFER_IN: "transfer_in",
        EventType.TRANSFER_OUT: "transfer_out",
    }
    kind = kind_map.get(trade.event_type, "buy")
    token = SwapLeg(
        address=trade.token_address,
        symbol=trade.token_symbol,
        name=trade.token_name,
        amount=trade.token_amount,
        usd_amount=trade.cost_usd,
        usd_price=trade.price_usd,
    )
    quote_symbol = "SOL" if trade.cost_sol is not None else ""
    quote = SwapLeg(
        address=WSOL_MINT if quote_symbol == "SOL" else "",
        symbol=quote_symbol,
        amount=abs(trade.cost_sol) if trade.cost_sol is not None else None,
        usd_amount=trade.cost_usd,
    )
    if kind in {"buy", "transfer_in"}:
        bought, sold = token, quote
    else:
        bought, sold = quote, token
    return WalletSwap(
        transaction_hash=trade.tx_hash,
        transaction_type=kind,
        block_timestamp=int(trade.timestamp or 0),
        wallet_address=trade.wallet_address,
        bought=bought,
        sold=sold,
        total_value_usd=trade.cost_usd,
        source=DataSource.GMGN,
        raw=dict(trade.raw or {}),
    )
