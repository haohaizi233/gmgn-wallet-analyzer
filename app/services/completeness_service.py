from __future__ import annotations

from decimal import Decimal
from typing import Any, Iterable

from app.domain.enums import AcquisitionType, EventType, FieldStatus, TokenPositionStatus
from app.domain.formatters import format_duration_zh, format_hms_unbounded, safe_export_value
from app.domain.models import AuditedValue, TokenAnalysisResult, TradeRecord, WarningRecord
from app.utils.money import format_usd, to_decimal
from app.utils.text import token_display_labels
from app.utils.time_utils import format_datetime, now_ts


def _is_present(status: FieldStatus) -> bool:
    return status in {
        FieldStatus.KNOWN,
        FieldStatus.VERIFIED,
        FieldStatus.CONSENSUS,
        FieldStatus.DIRECT,
        FieldStatus.DERIVED,
        FieldStatus.ESTIMATED,
    }


def assert_no_empty_export_cells(rows: Iterable[dict[str, Any]]) -> None:
    for index, row in enumerate(rows, start=1):
        for key, value in row.items():
            if value is None:
                raise AssertionError(f"row {index} field {key} is None")
            if isinstance(value, str) and value.strip() == "":
                raise AssertionError(f"row {index} field {key} is empty")
            if isinstance(value, float) and value != value:
                raise AssertionError(f"row {index} field {key} is NaN")


def _audited_or_missing(value: AuditedValue, kind: str) -> str:
    exported = value.export(kind)
    if exported is None or exported == "":
        return "GMGN 未提供"
    return str(exported)


def _drawer(token: TokenAnalysisResult, attr: str, kind: str) -> Any:
    """Read a field directly from the GMGN drawer metrics (trusted as-is)."""
    dm = getattr(token, "drawer_metrics", None)
    if dm is None:
        return "未抓取抽屉"
    value = getattr(dm, attr, None)
    if value is None:
        return "GMGN 未提供"
    if kind == "int":
        return int(value)
    if kind == "percent":
        return f"{value}%"
    if kind == "usd_compact":
        return format_usd(Decimal(value)) if Decimal(value) != 0 else "$0"
    return str(value)


class CompletenessService:
    def token_export_row(self, token: TokenAnalysisResult) -> dict[str, Any]:
        acq = token.acquisition
        first_buy = token.first_buy_display.export("usd_compact")
        buy_time = token.first_buy_time.export("datetime")
        created = token.created_at.export("datetime")
        time_diff = token.time_diff_seconds
        time_diff_seconds = time_diff.export("int") if time_diff.status == FieldStatus.KNOWN else time_diff.export("text")
        time_diff_text = (
            format_duration_zh(int(time_diff.value))
            if time_diff.status == FieldStatus.KNOWN and time_diff.value is not None
            else time_diff.export("text")
        )
        if acq.acquisition_type != AcquisitionType.BUY and time_diff.status == FieldStatus.KNOWN:
            time_diff_text = f"{time_diff_text}（转入时间差）"
        holding = token.holding_duration_seconds
        holding_text = (
            format_duration_zh(int(holding.value))
            if holding.status == FieldStatus.KNOWN and holding.value is not None
            else holding.export("text")
        )
        if token.buy_count == 0:
            buy_total = safe_export_value(Decimal("0"), FieldStatus.KNOWN, kind="usd")
        else:
            buy_total = safe_export_value(
                token.buy_total_usd,
                FieldStatus.ESTIMATED if getattr(token, "buy_total_estimated", False) and token.buy_total_usd is not None else FieldStatus.KNOWN if token.buy_total_usd is not None else FieldStatus.UNRESOLVED,
                estimated=bool(getattr(token, "buy_total_estimated", False)),
                kind="usd",
                reason="无法验证买入总额",
            )
        if token.sell_count == 0:
            sell_total = safe_export_value(Decimal("0"), FieldStatus.KNOWN, kind="usd")
        else:
            sell_total = safe_export_value(
                token.sell_total_usd,
                FieldStatus.ESTIMATED if getattr(token, "sell_total_estimated", False) and token.sell_total_usd is not None else FieldStatus.KNOWN if token.sell_total_usd is not None else FieldStatus.UNRESOLVED,
                estimated=bool(getattr(token, "sell_total_estimated", False)),
                kind="usd",
                reason="无法验证卖出总额",
            )
        symbol, name = token_display_labels(token.symbol, token.name, fallback=(token.token_address or "")[:6] or "未知")
        row = {
            "#": 0,
            "币种": symbol,
            "代币名称": name,
            "代币合约": token.token_address or "未知地址",
            "来源平台": _audited_or_missing(token.source_platform, "text"),
            "获得方式": acq.acquisition_type.value,
            "获得状态": acq.status.value,
            "入场市值": token.market_cap.export("usd_compact"),
            "首笔买入": first_buy,
            "首买数量": token.first_buy_amount.export("amount"),
            "买入时间": buy_time,
            "创建时间": created,
            "开盘时间": token.open_at.export("datetime"),
            "池子创建时间": token.pool_created_at.export("datetime"),
            "时差": str(time_diff_text),
            "时差HMS": format_hms_unbounded(int(time_diff.value)) if time_diff.status == FieldStatus.KNOWN and time_diff.value is not None else "无法计算",
            "时差秒数": time_diff_seconds,
            "持仓时长": holding_text,
            "持仓时长秒数": holding.export("int") if holding.status == FieldStatus.KNOWN else holding.export("text"),
            "买入笔数": token.buy_count,
            "买入总额": buy_total,
            "卖出笔数": token.sell_count,
            "卖出总额": sell_total,
            "总盈亏": token.total_profit.export("usd"),
            "总盈亏%": token.total_profit_pnl.export("percent"),
            "平均买入MC": _drawer(token, "avg_mc_buy", "usd_compact"),
            "平均卖出MC": _drawer(token, "avg_mc_sell", "usd_compact"),
            "转入总额": _drawer(token, "transfers_in_usd", "usd_compact"),
            "转入笔数": _drawer(token, "transfers_in_txs", "int"),
            "转出总额": _drawer(token, "transfers_out_usd", "usd_compact"),
            "转出笔数": _drawer(token, "transfers_out_txs", "int"),
            "余额": _drawer(token, "balance_usd", "usd_compact"),
            "Launchpad": _audited_or_missing(token.launchpad_platform, "text"),
            "资产来源": _audited_or_missing(token.asset_source, "text"),
            "流动性平台": _audited_or_missing(token.liquidity_platform, "text"),
            "当前市值": token.current_market_cap.export("usd_compact"),
            "FDV": token.fdv.export("usd_compact"),
            "持仓状态": token.position_status.value,
            "状态": token.status.value,
            "钱包": token.wallet_address,
            "警告": "；".join(token.warnings) if token.warnings else "无",
            "首买验证状态": token.first_buy_verify_status or "无",
            "首买来源": token.first_buy_source or "无",
            "创建时间验证状态": token.created_verify_status or "无",
            "创建时间来源": token.created_source or "无",
            "市值验证状态": token.market_verify_status or "无",
            "市值来源": token.market_source or "无",
            "平台验证状态": token.platform_verify_status or "无",
            "平台来源": token.platform_source or "无",
            "余额验证状态": token.balance_verify_status or "无",
            "余额来源": token.balance_authority or "无",
            "PnL验证状态": token.pnl_verify_status or "无",
        }
        assert_no_empty_export_cells([row])
        return row

    def trade_export_row(self, trade: TradeRecord, platform: str) -> dict[str, Any]:
        if trade.event_type == EventType.BUY:
            pnl = "未实现"
        elif trade.event_type == EventType.SELL:
            pnl = trade.single_pnl_display or "无法验证历史成本"
        elif trade.event_type == EventType.TRANSFER_IN:
            pnl = format_usd(Decimal("0"))
        elif trade.event_type == EventType.TRANSFER_OUT:
            pnl = format_usd(Decimal("0"))
        else:
            pnl = "未实现"
        row = {
            "时间": format_datetime(trade.timestamp) if trade.timestamp else "GMGN 未提供",
            "类型": trade.event_type.value,
            "币种": token_display_labels(trade.token_symbol, trade.token_name, fallback=(trade.token_address or "")[:6] or "未知")[0],
            "代币合约": trade.token_address or "未知地址",
            "数量": safe_export_value(trade.token_amount, trade.amount_status, kind="amount"),
            "USD金额": safe_export_value(
                trade.cost_usd,
                trade.cost_usd_status,
                estimated=getattr(trade, "cost_usd_estimated", False),
                kind="usd",
            ),
            "SOL金额": safe_export_value(
                trade.cost_sol,
                FieldStatus.ESTIMATED if trade.cost_sol_estimated else trade.cost_sol_status,
                estimated=trade.cost_sol_estimated,
                kind="amount",
            ),
            "价格USD": safe_export_value(
                trade.price_usd,
                FieldStatus.ESTIMATED if getattr(trade, "cost_usd_estimated", False) and trade.price_usd is not None else FieldStatus.KNOWN if trade.price_usd is not None else FieldStatus.API_MISSING,
                estimated=getattr(trade, "cost_usd_estimated", False),
                kind="usd",
            ),
            "Gas USD": safe_export_value(trade.gas_usd, trade.gas_usd_status, kind="usd"),
            "Gas SOL": safe_export_value(trade.gas_sol, trade.gas_sol_status, kind="amount"),
            "单笔盈亏": pnl,
            "来源平台": platform or "未知来源",
            "钱包": trade.wallet_address,
            "TxHash": trade.tx_hash or "未知哈希",
            "是否报告范围": "是" if trade.in_report_range else "否（历史追溯）",
        }
        assert_no_empty_export_cells([row])
        return row

    def collect_warnings(self, token: TokenAnalysisResult) -> list[WarningRecord]:
        records: list[WarningRecord] = []
        fields = [
            ("来源平台", token.source_platform),
            ("创建时间", token.created_at),
            ("入场市值", token.market_cap),
            ("首笔买入", token.first_buy_display),
            ("买入时间", token.first_buy_time),
            ("时差", token.time_diff_seconds),
            ("已实现盈亏", token.realized_profit),
            ("总盈亏%", token.total_profit_pnl),
        ]
        for name, audited in fields:
            if not _is_present(audited.status) or audited.estimated:
                records.append(
                    WarningRecord(
                        wallet_address=token.wallet_address,
                        token_address=token.token_address,
                        token_symbol=token.symbol,
                        field_name=name,
                        final_value=str(audited.export("text")),
                        status=audited.status.value,
                        reason=audited.reason or token.acquisition.reason or "见数据来源",
                        source=audited.source,
                        estimated=audited.estimated,
                    )
                )
        if token.acquisition.acquisition_type != AcquisitionType.BUY:
            records.append(
                WarningRecord(
                    wallet_address=token.wallet_address,
                    token_address=token.token_address,
                    token_symbol=token.symbol,
                    field_name="首笔买入",
                    final_value=str(token.first_buy_display.export("usd_compact")),
                    status=token.acquisition.status.value,
                    reason=token.acquisition.reason,
                    source=token.acquisition.source,
                    estimated=False,
                )
            )
        if token.missing_cost_count:
            records.append(
                WarningRecord(
                    wallet_address=token.wallet_address,
                    token_address=token.token_address,
                    token_symbol=token.symbol,
                    field_name="缺失成本",
                    final_value=str(token.missing_cost_count),
                    status="UNKNOWN",
                    reason="Sell 数量超过可验证 Buy 成本",
                    source="local_fifo",
                    estimated=False,
                )
            )
        return records

    def apply_time_fields(self, token: TokenAnalysisResult, last_sell_ts: int | None) -> None:
        created = token.created_at.value if _is_present(token.created_at.status) else None
        acq_ts = token.acquisition.timestamp
        if acq_ts and created:
            token.time_diff_seconds = AuditedValue(
                acq_ts - int(created),
                "first_acquisition - token_created_at",
                FieldStatus.KNOWN,
                reason="转入时间差" if token.acquisition.acquisition_type != AcquisitionType.BUY else "",
            )
        elif not created:
            token.time_diff_seconds = AuditedValue(None, "token_info.creation_timestamp", FieldStatus.API_MISSING, reason="GMGN 未提供创建时间，无法计算时差")
        else:
            token.time_diff_seconds = AuditedValue(None, "wallet_activity", FieldStatus.UNKNOWN, reason="无首次获得时间，无法计算时差")

        if not acq_ts:
            token.holding_duration_seconds = AuditedValue(None, "wallet_activity", FieldStatus.UNKNOWN, reason="无首次获得时间")
            return
        if token.position_status == TokenPositionStatus.CLOSED and last_sell_ts:
            seconds = last_sell_ts - acq_ts
            source = "last_sell - first_acquisition"
        else:
            seconds = now_ts() - acq_ts
            source = "now - first_acquisition"
        token.holding_duration_seconds = AuditedValue(seconds, source, FieldStatus.KNOWN)

    def coverage_metrics(self, tokens: list[TokenAnalysisResult]) -> dict[str, Any]:
        from app.domain.evidence import CoverageMetrics

        metrics = CoverageMetrics()
        for token in tokens:
            fields = [
                token.source_platform,
                token.created_at,
                token.first_buy_time,
                token.first_buy_amount,
                token.first_buy_display,
                token.market_cap,
                token.fifo_realized_profit,
                token.current_market_cap,
                token.fdv,
            ]
            for audited in fields:
                metrics.total_fields += 1
                st = audited.status
                if st == FieldStatus.VERIFIED:
                    metrics.verified_fields += 1
                elif st == FieldStatus.CONSENSUS:
                    metrics.consensus_fields += 1
                elif st == FieldStatus.DIRECT:
                    metrics.direct_fields += 1
                elif st == FieldStatus.DERIVED:
                    metrics.derived_fields += 1
                elif st == FieldStatus.ESTIMATED or audited.estimated:
                    metrics.estimated_fields += 1
                elif st == FieldStatus.UNRESOLVED:
                    metrics.unresolved_fields += 1
                elif st == FieldStatus.CONFLICT:
                    metrics.conflict_fields += 1
                elif st == FieldStatus.NOT_APPLICABLE:
                    metrics.not_applicable_fields += 1
                elif st == FieldStatus.KNOWN:
                    metrics.direct_fields += 1
                else:
                    metrics.unresolved_fields += 1
        return metrics.to_dict()


class CompletenessAuditor(CompletenessService):
    """V4 名称别名。"""
