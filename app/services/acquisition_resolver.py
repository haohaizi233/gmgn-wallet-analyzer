from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any, Optional

from app.domain.enums import AcquisitionStatus, AcquisitionType, EventType, FieldStatus
from app.domain.models import AcquisitionInfo, TokenInfo, TradeRecord
from app.services.special_assets import lookup_special_asset
from app.utils.money import to_decimal

logger = logging.getLogger(__name__)


def _dump_raw_fields(trade: TradeRecord) -> None:
    """Emit the raw activity-item field names + MC-relevant values once per first trade,
    so we can see exactly what GMGN provided vs. what the frontend displays."""
    raw = getattr(trade, "raw", None)
    if not isinstance(raw, dict):
        return
    token = raw.get("token") if isinstance(raw.get("token"), dict) else {}
    keys = sorted(raw.keys())
    mc_candidates = {k: raw.get(k) for k in keys if "cap" in k.lower() or "mc" in k.lower() or k in ("price_usd", "price", "cost_usd", "token_amount")}
    token_mc = {k: token.get(k) for k in ("market_cap", "marketCap", "market_cap_usd", "marketcap", "total_supply", "max_supply", "circulating_supply", "supply") if k in token}
    logger.warning(
        "入场市值[RAW] event=%s keys=%s mc_fields=%s token_mc_fields=%s price_usd=%s total_supply_at_trade=%s row_market_cap=%s",
        getattr(trade, "event_type", None),
        keys,
        mc_candidates,
        token_mc,
        trade.price_usd,
        trade.total_supply_at_trade,
        trade.market_cap,
    )


def _min_trade(trades: list[TradeRecord], event: EventType) -> Optional[TradeRecord]:
    matched = [t for t in trades if t.event_type == event and t.timestamp > 0]
    if not matched:
        return None
    return min(matched, key=lambda t: (t.timestamp, t.tx_hash))


def _market_cap(trade: TradeRecord, info: Optional[TokenInfo]) -> tuple[Optional[Decimal], bool, str]:
    if trade.market_cap is not None and trade.market_cap > 0:
        return trade.market_cap, False, "wallet_activity.market_cap"
    supply = trade.total_supply_at_trade
    source = "activity.token.total_supply"
    estimated = False
    if supply is None and info is not None:
        supply = info.circulating_supply or info.total_supply
        source = "token_info.current_supply"
        estimated = True
    if trade.price_usd is not None and trade.price_usd > 0 and supply is not None:
        return trade.price_usd * supply, estimated, f"price_usd × {source}"
    # Transfer-in rows often have price_usd = 0 (no trade price). GMGN's drawer shows the
    # token's CURRENT market cap on every row; mirror that by falling back to the current
    # MC from token_info when no historical price is available.
    if info is not None and info.market_cap is not None and info.market_cap > 0:
        return info.market_cap, True, "token_info.current_market_cap"
    if info is not None and info.fdv is not None and info.fdv > 0:
        return info.fdv, True, "token_info.fdv"
    return None, False, "GMGN 未提供历史市值所需价格或供给"


def resolve_acquisition(
    trades: list[TradeRecord],
    token_info: Optional[TokenInfo] = None,
    detect_special: bool = True,
) -> AcquisitionInfo:
    buys = [t for t in trades if t.event_type == EventType.BUY]
    first_buy = _min_trade(trades, EventType.BUY)
    first_in = _min_trade(trades, EventType.TRANSFER_IN)
    special = lookup_special_asset(trades[0].token_address if trades else (token_info.token_address if token_info else ""), token_info) if detect_special else None

    if first_buy is not None:
        market_cap, estimated, source = _market_cap(first_buy, token_info)
        _dump_raw_fields(first_buy)
        try:
            logger.info(
                "入场市值[BUY] token=%s tx=%s price_usd=%s supply=%s row_mc=%s info_mc=%s -> mc=%s via %s",
                str(getattr(first_buy, "token_address", ""))[:8],
                str(getattr(first_buy, "tx_hash", ""))[:10],
                first_buy.price_usd,
                first_buy.total_supply_at_trade,
                first_buy.market_cap,
                getattr(token_info, "market_cap", None),
                market_cap,
                source,
            )
        except Exception:
            pass
        return AcquisitionInfo(
            acquisition_type=AcquisitionType.BUY,
            timestamp=first_buy.timestamp,
            price_usd=first_buy.price_usd,
            amount=first_buy.token_amount,
            cost_usd=first_buy.cost_usd,
            cost_sol=first_buy.cost_sol,
            gas_usd=first_buy.gas_usd,
            gas_sol=first_buy.gas_sol,
            market_cap=market_cap,
            status=AcquisitionStatus.VERIFIED,
            market_cap_is_estimated=estimated,
            market_cap_source=source if market_cap is not None else "GMGN 未提供",
            cost_sol_estimated=first_buy.cost_sol_estimated,
            source="wallet_activity",
            reason="完整历史中 timestamp 最小的 Buy",
            tx_hash=first_buy.tx_hash,
        )

    if first_in is not None:
        market_cap, estimated, source = _market_cap(first_in, token_info)
        _dump_raw_fields(first_in)
        try:
            logger.info(
                "入场市值[TRANSFER_IN] token=%s tx=%s price_usd=%s supply=%s row_mc=%s info_mc=%s info_fdv=%s -> mc=%s via %s",
                str(getattr(first_in, "token_address", ""))[:8],
                str(getattr(first_in, "tx_hash", ""))[:10],
                first_in.price_usd,
                first_in.total_supply_at_trade,
                first_in.market_cap,
                getattr(token_info, "market_cap", None),
                getattr(token_info, "fdv", None),
                market_cap,
                source,
            )
        except Exception:
            pass
        acq_type = AcquisitionType.TRANSFER_IN
        status = AcquisitionStatus.NO_BUY_HISTORY
        reason = "未发现 Buy，存在 TransferIn"
        if special:
            if special.acquisition_type in (AcquisitionType.BRIDGE, AcquisitionType.WRAPPED):
                acq_type = special.acquisition_type
                status = AcquisitionStatus.INFERRED
                reason = f"{reason}；{special.reason}"
            elif special.platform == "xStocks":
                acq_type = AcquisitionType.TRANSFER_IN
                status = AcquisitionStatus.INFERRED
                reason = f"{reason}；{special.reason}"
        if not buys and any(t.event_type == EventType.SELL for t in trades) is False:
            # 仅转入也可能是空投
            if first_in.cost_usd in (None, Decimal("0")) and first_in.price_usd in (None, Decimal("0")):
                # 不把空投硬判为 AIRDROP，除非没有卖出且金额为 0。保持 TRANSFER_IN。
                pass
        cap_reason = "首次转入时估值" if market_cap is not None else "无可验证历史市值"
        return AcquisitionInfo(
            acquisition_type=acq_type,
            timestamp=first_in.timestamp,
            price_usd=first_in.price_usd,
            amount=first_in.token_amount,
            cost_usd=first_in.cost_usd,
            cost_sol=first_in.cost_sol,
            gas_usd=first_in.gas_usd,
            gas_sol=first_in.gas_sol,
            market_cap=market_cap,
            status=status,
            market_cap_is_estimated=True if market_cap is not None else False,
            market_cap_source=source if market_cap is not None else cap_reason,
            cost_sol_estimated=first_in.cost_sol_estimated,
            source="wallet_activity",
            reason=reason,
            tx_hash=first_in.tx_hash,
        )

    if special and special.acquisition_type in (AcquisitionType.BRIDGE, AcquisitionType.WRAPPED):
        return AcquisitionInfo(
            acquisition_type=special.acquisition_type,
            timestamp=None,
            price_usd=None,
            amount=None,
            cost_usd=None,
            cost_sol=None,
            gas_usd=None,
            gas_sol=None,
            market_cap=None,
            status=AcquisitionStatus.INFERRED,
            market_cap_source="无可验证历史市值",
            source="special_asset_resolver",
            reason=f"无 Buy/TransferIn；{special.reason}",
        )

    return AcquisitionInfo(
        acquisition_type=AcquisitionType.UNKNOWN,
        timestamp=None,
        price_usd=None,
        amount=None,
        cost_usd=None,
        cost_sol=None,
        gas_usd=None,
        gas_sol=None,
        market_cap=None,
        status=AcquisitionStatus.NO_BUY_HISTORY,
        market_cap_source="无可验证历史市值",
        source="wallet_activity",
        reason="完整历史中未发现 Buy 或 TransferIn",
    )


def first_buy_in_range(trades: list[TradeRecord], start_ts: int, end_ts: int) -> Optional[TradeRecord]:
    """仅用于对照，不得当作真正 First Buy。"""
    matched = [t for t in trades if t.event_type == EventType.BUY and start_ts <= t.timestamp <= end_ts]
    if not matched:
        return None
    return min(matched, key=lambda t: t.timestamp)
