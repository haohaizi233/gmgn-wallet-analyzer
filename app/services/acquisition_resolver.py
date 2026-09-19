from __future__ import annotations

from decimal import Decimal
from typing import Optional

from app.domain.enums import AcquisitionStatus, AcquisitionType, EventType, FieldStatus
from app.domain.models import AcquisitionInfo, TokenInfo, TradeRecord
from app.services.special_assets import lookup_special_asset
from app.utils.money import to_decimal


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
    if trade.price_usd is None or supply is None:
        return None, False, "GMGN 未提供历史市值所需价格或供给"
    return trade.price_usd * supply, estimated, f"price_usd × {source}"


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
