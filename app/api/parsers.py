from __future__ import annotations

from decimal import Decimal
from typing import Any, Optional

from app.domain.enums import EventType, FieldStatus
from app.domain.fingerprint import activity_fingerprint
from app.domain.models import GmgnProfit, GmgnWalletStats, TokenInfo, TokenPoolInfo, TradeRecord
from app.utils.money import to_decimal


def unwrap_payload(payload: Any) -> Any:
    if isinstance(payload, dict) and "code" in payload and "data" in payload:
        return payload.get("data")
    return payload


def as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def first_present(data: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in data and data[key] not in (None, ""):
            return data[key]
    return None


def parse_event_type(raw: Any) -> EventType:
    text = str(raw or "").strip()
    mapping = {
        "buy": EventType.BUY,
        "sell": EventType.SELL,
        "transferin": EventType.TRANSFER_IN,
        "transfer_in": EventType.TRANSFER_IN,
        "transfer": EventType.TRANSFER_IN,
        "transferout": EventType.TRANSFER_OUT,
        "transfer_out": EventType.TRANSFER_OUT,
        "add": EventType.ADD,
        "remove": EventType.REMOVE,
    }
    return mapping.get(text.lower(), EventType.UNKNOWN)


def _status_for(value: Optional[Decimal]) -> FieldStatus:
    return FieldStatus.KNOWN if value is not None else FieldStatus.API_MISSING


def parse_activity_item(item: dict[str, Any], wallet_address: str, chain: str) -> Optional[TradeRecord]:
    if not isinstance(item, dict):
        return None
    token = as_dict(item.get("token"))
    token_address = str(
        first_present(item, "token_address", "base_address")
        or first_present(token, "address", "token_address")
        or ""
    ).strip()
    tx_hash = str(first_present(item, "tx_hash", "transaction_hash", "hash") or "").strip()
    ts_raw = first_present(item, "timestamp", "block_unix_time", "time")
    try:
        timestamp = int(float(ts_raw)) if ts_raw is not None else 0
    except (TypeError, ValueError):
        timestamp = 0
    if timestamp > 10_000_000_000:
        timestamp = timestamp // 1000
    event = parse_event_type(first_present(item, "event_type", "type", "side"))
    if event == EventType.UNKNOWN and str(item.get("type") or "").lower() == "transfer":
        direction = str(item.get("transfer_type") or item.get("direction") or "").lower()
        if direction in ("out", "transferout", "from"):
            event = EventType.TRANSFER_OUT
        else:
            event = EventType.TRANSFER_IN

    token_amount = to_decimal(first_present(item, "token_amount", "amount", "base_amount"))
    price_usd = to_decimal(first_present(item, "price_usd", "priceUsd"))
    cost_usd = to_decimal(first_present(item, "cost_usd", "amount_usd", "usd_value"))
    cost_sol = to_decimal(first_present(item, "cost_sol", "quote_amount", "amount_sol", "sol_amount"))
    gas_usd = to_decimal(first_present(item, "gas_usd"))
    gas_sol = to_decimal(first_present(item, "gas_sol", "gas"))
    cost_sol_estimated = False
    quote_symbol = first_present(item, "quote_symbol") or token.get("quote_symbol")
    price_quote = to_decimal(item.get("price"))
    if cost_sol is None and token_amount is not None and price_quote is not None:
        if quote_symbol in (None, "", "SOL", "WSOL", "W_SOL"):
            cost_sol = token_amount * price_quote
            cost_sol_estimated = True
            quote_symbol = quote_symbol or "SOL"
    if cost_usd is None and price_usd is not None and token_amount is not None:
        cost_usd = price_usd * token_amount

    symbol = str(first_present(token, "symbol") or item.get("symbol") or "").strip() or "未知"
    name = str(first_present(token, "name") or item.get("name") or "").strip() or symbol
    launchpad_platform = first_present(item, "launchpad_platform") or first_present(token, "launchpad_platform", "launchpad")
    supply = to_decimal(first_present(token, "total_supply", "max_supply") or item.get("total_supply"))
    market_cap = to_decimal(
        first_present(item, "market_cap", "marketCap", "market_cap_usd")
        or first_present(token, "market_cap", "marketCap", "market_cap_usd")
    )
    buy_cost_usd = to_decimal(item.get("buy_cost_usd"))

    if not token_address:
        return None
    fingerprint = activity_fingerprint(chain, wallet_address, item)
    return TradeRecord(
        wallet_address=wallet_address,
        chain=chain,
        token_address=token_address,
        token_symbol=symbol,
        token_name=name,
        tx_hash=tx_hash or "未知哈希",
        timestamp=timestamp,
        event_type=event,
        token_amount=token_amount,
        price_usd=price_usd,
        cost_usd=cost_usd,
        cost_sol=cost_sol,
        gas_usd=gas_usd,
        gas_sol=gas_sol,
        launchpad_platform=str(launchpad_platform) if launchpad_platform else None,
        raw=item,
        buy_cost_usd=buy_cost_usd,
        quote_symbol=str(quote_symbol) if quote_symbol else None,
        total_supply_at_trade=supply,
        market_cap=market_cap,
        amount_status=_status_for(token_amount),
        cost_usd_status=_status_for(cost_usd),
        cost_sol_status=FieldStatus.ESTIMATED if cost_sol_estimated else _status_for(cost_sol),
        gas_usd_status=_status_for(gas_usd),
        gas_sol_status=_status_for(gas_sol),
        cost_sol_estimated=cost_sol_estimated,
        activity_fingerprint=fingerprint,
    )


def parse_activity_page(payload: Any, wallet_address: str, chain: str) -> tuple[list[TradeRecord], Optional[str]]:
    data = unwrap_payload(payload)
    data = as_dict(data) if not isinstance(data, list) else {"activities": data}
    rows = data.get("activities")
    if rows is None:
        rows = data.get("list") or []
    trades: list[TradeRecord] = []
    for item in rows:
        parsed = parse_activity_item(item, wallet_address, chain)
        if parsed is not None:
            trades.append(parsed)
    nxt = data.get("next")
    if nxt in (None, "", "null"):
        return trades, None
    return trades, str(nxt)


def parse_token_info(payload: Any, token_address: str, chain: str = "sol") -> TokenInfo:
    data = as_dict(unwrap_payload(payload))
    pool = as_dict(data.get("pool"))
    dev = as_dict(data.get("dev"))
    address = str(data.get("address") or token_address)
    creation = data.get("creation_timestamp")
    open_ts = data.get("open_timestamp")
    pool_created = pool.get("creation_timestamp")
    return TokenInfo(
        token_address=address,
        chain=chain,
        symbol=str(data.get("symbol") or "").strip() or "未知",
        name=str(data.get("name") or data.get("symbol") or "").strip() or "未知",
        creation_timestamp=int(creation) if creation not in (None, "") else None,
        open_timestamp=int(open_ts) if open_ts not in (None, "") else None,
        pool_created_at=int(pool_created) if pool_created not in (None, "") else None,
        total_supply=to_decimal(data.get("total_supply")),
        circulating_supply=to_decimal(data.get("circulating_supply")),
        launchpad=data.get("launchpad") or None,
        launchpad_platform=data.get("launchpad_platform") or None,
        pool_exchange=pool.get("exchange") or None,
        pool_address=pool.get("pool_address") or data.get("biggest_pool_address"),
        creator_address=dev.get("creator_address"),
        raw=data,
        info_status=FieldStatus.KNOWN,
    )


def parse_pool_info(payload: Any, token_address: str) -> TokenPoolInfo:
    data = unwrap_payload(payload)
    if isinstance(data, list) and data:
        data = data[0]
    data = as_dict(data)
    created = data.get("creation_timestamp")
    return TokenPoolInfo(
        token_address=token_address,
        pool_address=data.get("address") or data.get("pool_address"),
        exchange=data.get("exchange"),
        liquidity=to_decimal(data.get("liquidity")),
        base_address=data.get("base_address"),
        quote_address=data.get("quote_address"),
        price=to_decimal(data.get("price")),
        creation_timestamp=int(created) if created not in (None, "") else None,
        raw=data,
        status=FieldStatus.KNOWN if data else FieldStatus.API_MISSING,
        error="" if data else "GMGN 未提供 pool_info",
    )


def parse_wallet_stats(payload: Any, wallet_address: str, period: str) -> GmgnWalletStats:
    data = unwrap_payload(payload)
    if isinstance(data, list):
        match = next((item for item in data if as_dict(item).get("wallet_address") == wallet_address), None)
        data = match if match is not None else (data[0] if data else {})
    data = as_dict(data)
    if not data:
        return GmgnWalletStats(
            wallet_address=wallet_address,
            period=period,
            status=FieldStatus.API_MISSING,
            reason="GMGN 未提供 wallet_stats",
        )
    return GmgnWalletStats(
        wallet_address=wallet_address,
        period=period,
        realized_profit=to_decimal(data.get("realized_profit")),
        unrealized_profit=to_decimal(data.get("unrealized_profit")),
        winrate=to_decimal(data.get("winrate")),
        total_cost=to_decimal(data.get("total_cost")),
        buy_count=int(data["buy_count"]) if data.get("buy_count") not in (None, "") else None,
        sell_count=int(data["sell_count"]) if data.get("sell_count") not in (None, "") else None,
        pnl=to_decimal(data.get("pnl")),
        status=FieldStatus.KNOWN,
        raw=data,
    )


def parse_wallet_profits(payload: Any, wallet_address: str, period: str) -> GmgnProfit:
    data = unwrap_payload(payload)
    data = as_dict(data)
    rows = data.get("list") if isinstance(data.get("list"), list) else None
    if rows is None and isinstance(unwrap_payload(payload), list):
        rows = unwrap_payload(payload)
    item = {}
    if rows:
        item = next((as_dict(r) for r in rows if as_dict(r).get("wallet_address") == wallet_address), as_dict(rows[0]))
    elif data:
        item = data
    if not item:
        return GmgnProfit(
            wallet_address=wallet_address,
            period=period,
            status=FieldStatus.API_MISSING,
            reason="GMGN 未提供 wallet_profits",
        )
    total_profit = to_decimal(item.get("total_profit"))
    total_cost = to_decimal(item.get("total_cost"))
    pnl = None
    if total_profit is not None and total_cost not in (None, Decimal("0")):
        pnl = total_profit / total_cost if total_cost else None
    return GmgnProfit(
        wallet_address=wallet_address,
        period=period,
        realized_profit=to_decimal(item.get("realized_profit")),
        unrealized_profit=to_decimal(item.get("unrealized_profit")),
        total_profit=total_profit,
        total_profit_pnl=to_decimal(item.get("total_profit_pnl")) or pnl,
        total_cost=total_cost,
        buy_count=int(item["buy"]) if item.get("buy") not in (None, "") else None,
        sell_count=int(item["sell"]) if item.get("sell") not in (None, "") else None,
        status=FieldStatus.KNOWN,
        raw=item,
    )
