from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Optional

from app.domain.enums import (
    AcquisitionStatus,
    AcquisitionType,
    EventType,
    FieldStatus,
    ReportPeriod,
    TaskStatus,
    TokenPositionStatus,
)
from app.domain.formatters import safe_export_value


@dataclass
class AuditedValue:
    value: Any
    source: str
    status: FieldStatus = FieldStatus.KNOWN
    estimated: bool = False
    reason: str = ""
    field_name: str = ""

    def export(self, kind: str = "text") -> Any:
        return safe_export_value(
            self.value,
            self.status,
            estimated=self.estimated,
            reason=self.reason,
            kind=kind,
        )

    def to_audit_dict(self) -> dict[str, Any]:
        return {
            "value": self.value if not isinstance(self.value, Decimal) else format(self.value, "f"),
            "source": self.source,
            "status": self.status.value,
            "estimated": self.estimated,
            "reason": self.reason,
        }


def known(value: Any, source: str, estimated: bool = False, reason: str = "", field_name: str = "") -> AuditedValue:
    return AuditedValue(
        value=value,
        source=source,
        status=FieldStatus.ESTIMATED if estimated else FieldStatus.KNOWN,
        estimated=estimated,
        reason=reason,
        field_name=field_name,
    )


def missing(source: str, reason: str = "GMGN 未提供", field_name: str = "") -> AuditedValue:
    return AuditedValue(
        value=None,
        source=source,
        status=FieldStatus.API_MISSING,
        reason=reason,
        field_name=field_name,
    )


def not_applicable(source: str, reason: str, field_name: str = "") -> AuditedValue:
    return AuditedValue(
        value=None,
        source=source,
        status=FieldStatus.NOT_APPLICABLE,
        reason=reason,
        field_name=field_name,
    )


def unknown(source: str, reason: str = "未知", field_name: str = "") -> AuditedValue:
    return AuditedValue(
        value=None,
        source=source,
        status=FieldStatus.UNKNOWN,
        reason=reason,
        field_name=field_name,
    )


def error_value(source: str, reason: str, field_name: str = "") -> AuditedValue:
    return AuditedValue(
        value=None,
        source=source,
        status=FieldStatus.ERROR,
        reason=reason,
        field_name=field_name,
    )


@dataclass
class AnalysisOptions:
    fetch_token_created_at: bool = True
    fetch_platform_pool: bool = True
    detect_transfer_bridge: bool = True
    autofill_missing: bool = True
    save_raw_json: bool = True
    pumpfun_only: bool = False
    collection_mode: str = "api"
    browser_headed: bool = True


@dataclass
class WalletAnalysisRequest:
    wallet_address: str
    chain: str = "sol"
    period: ReportPeriod = ReportPeriod.D7
    start_time: int = 0
    end_time: int = 0
    max_transactions: int = 2500
    options: AnalysisOptions = field(default_factory=AnalysisOptions)
    task_id: str = ""
    job_id: str = ""
    wallet_task_id: str = ""


@dataclass
class TradeRecord:
    wallet_address: str
    chain: str
    token_address: str
    token_symbol: str
    token_name: str
    tx_hash: str
    timestamp: int
    event_type: EventType
    token_amount: Optional[Decimal]
    price_usd: Optional[Decimal]
    cost_usd: Optional[Decimal]
    cost_sol: Optional[Decimal]
    gas_usd: Optional[Decimal]
    gas_sol: Optional[Decimal]
    launchpad_platform: Optional[str]
    raw: dict[str, Any] = field(default_factory=dict)
    buy_cost_usd: Optional[Decimal] = None
    quote_symbol: Optional[str] = None
    quote_mint: Optional[str] = None
    actual_quote_asset: Optional[str] = None
    actual_quote_amount: Optional[Decimal] = None
    equivalent_sol_amount: Optional[Decimal] = None
    total_supply_at_trade: Optional[Decimal] = None
    market_cap: Optional[Decimal] = None
    amount_status: FieldStatus = FieldStatus.KNOWN
    cost_usd_status: FieldStatus = FieldStatus.API_MISSING
    cost_sol_status: FieldStatus = FieldStatus.API_MISSING
    gas_usd_status: FieldStatus = FieldStatus.API_MISSING
    gas_sol_status: FieldStatus = FieldStatus.API_MISSING
    cost_sol_estimated: bool = False
    cost_usd_estimated: bool = False
    in_report_range: bool = True
    history_only: bool = False
    single_pnl_display: str = "未实现"
    missing_cost: bool = False
    activity_fingerprint: str = ""

    @property
    def token_key(self) -> tuple[str, str]:
        return (self.chain, self.token_address)


@dataclass
class TokenInfo:
    token_address: str
    chain: str = "sol"
    symbol: str = ""
    name: str = ""
    creation_timestamp: Optional[int] = None
    open_timestamp: Optional[int] = None
    pool_created_at: Optional[int] = None
    total_supply: Optional[Decimal] = None
    circulating_supply: Optional[Decimal] = None
    launchpad: Optional[str] = None
    launchpad_platform: Optional[str] = None
    pool_exchange: Optional[str] = None
    pool_address: Optional[str] = None
    creator_address: Optional[str] = None
    raw: dict[str, Any] = field(default_factory=dict)
    info_status: FieldStatus = FieldStatus.KNOWN
    info_error: str = ""


@dataclass
class TokenPoolInfo:
    token_address: str
    pool_address: Optional[str] = None
    exchange: Optional[str] = None
    liquidity: Optional[Decimal] = None
    base_address: Optional[str] = None
    quote_address: Optional[str] = None
    price: Optional[Decimal] = None
    creation_timestamp: Optional[int] = None
    raw: dict[str, Any] = field(default_factory=dict)
    status: FieldStatus = FieldStatus.KNOWN
    error: str = ""


@dataclass
class AcquisitionInfo:
    acquisition_type: AcquisitionType
    timestamp: Optional[int]
    price_usd: Optional[Decimal]
    amount: Optional[Decimal]
    cost_usd: Optional[Decimal]
    cost_sol: Optional[Decimal]
    gas_usd: Optional[Decimal]
    gas_sol: Optional[Decimal]
    market_cap: Optional[Decimal]
    status: AcquisitionStatus
    market_cap_is_estimated: bool = False
    market_cap_source: str = ""
    cost_sol_estimated: bool = False
    source: str = "wallet_activity"
    reason: str = ""
    tx_hash: str = ""


@dataclass
class GmgnProfit:
    wallet_address: str
    period: str
    realized_profit: Optional[Decimal] = None
    unrealized_profit: Optional[Decimal] = None
    total_profit: Optional[Decimal] = None
    total_profit_pnl: Optional[Decimal] = None
    total_cost: Optional[Decimal] = None
    buy_count: Optional[int] = None
    sell_count: Optional[int] = None
    status: FieldStatus = FieldStatus.KNOWN
    reason: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class GmgnWalletStats:
    wallet_address: str
    period: str
    realized_profit: Optional[Decimal] = None
    unrealized_profit: Optional[Decimal] = None
    winrate: Optional[Decimal] = None
    total_cost: Optional[Decimal] = None
    buy_count: Optional[int] = None
    sell_count: Optional[int] = None
    pnl: Optional[Decimal] = None
    status: FieldStatus = FieldStatus.KNOWN
    reason: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class WarningRecord:
    wallet_address: str
    token_address: str
    token_symbol: str
    field_name: str
    final_value: str
    status: str
    reason: str
    source: str
    estimated: bool = False


@dataclass
class TokenAnalysisResult:
    wallet_address: str
    token_address: str
    chain: str
    symbol: str
    name: str
    source_platform: AuditedValue
    acquisition: AcquisitionInfo
    created_at: AuditedValue
    open_at: AuditedValue
    pool_created_at: AuditedValue
    time_diff_seconds: AuditedValue
    buy_count: int
    buy_total_usd: Optional[Decimal]
    sell_count: int
    sell_total_usd: Optional[Decimal]
    realized_profit: AuditedValue
    unrealized_profit: AuditedValue
    total_profit: AuditedValue
    total_profit_pnl: AuditedValue
    fifo_realized_profit: AuditedValue
    current_balance: Optional[Decimal]
    holding_duration_seconds: AuditedValue
    missing_cost_count: int
    status: TaskStatus
    warnings: list[str] = field(default_factory=list)
    position_status: TokenPositionStatus = TokenPositionStatus.UNKNOWN
    first_buy_display: AuditedValue = field(default_factory=lambda: missing("local", "未计算"))
    first_buy_amount: AuditedValue = field(default_factory=lambda: missing("local", "未计算"))
    first_buy_time: AuditedValue = field(default_factory=lambda: missing("local", "未计算"))
    market_cap: AuditedValue = field(default_factory=lambda: missing("local", "未计算"))
    report_trade_count: int = 0
    history_page_count: int = 0
    history_complete: bool = False
    launchpad_platform: AuditedValue = field(default_factory=lambda: missing("token_info", "GMGN 未提供"))
    asset_source: AuditedValue = field(default_factory=lambda: missing("special_asset", "GMGN 未提供"))
    liquidity_platform: AuditedValue = field(default_factory=lambda: missing("token_pool_info", "GMGN 未提供"))
    primary_pool: AuditedValue = field(default_factory=lambda: missing("token_pool_info", "GMGN 未提供"))
    calculated_balance: Optional[Decimal] = None
    balance_authority: str = "DERIVED_FROM_ACTIVITY"
    missing_cost_sell_count: int = 0
    missing_cost_token_amount: Optional[Decimal] = None
    current_market_cap: AuditedValue = field(default_factory=lambda: missing("market", "无法验证"))
    fdv: AuditedValue = field(default_factory=lambda: missing("market", "无法验证"))
    first_buy_verify_status: str = "UNRESOLVED"
    first_buy_source: str = "无"
    created_verify_status: str = "UNRESOLVED"
    created_source: str = "无"
    market_verify_status: str = "UNRESOLVED"
    market_source: str = "无"
    platform_verify_status: str = "UNRESOLVED"
    platform_source: str = "无"
    balance_verify_status: str = "UNRESOLVED"
    pnl_verify_status: str = "UNRESOLVED"
    audit_rows: list[dict[str, Any]] = field(default_factory=list)
    buy_total_estimated: bool = False
    sell_total_estimated: bool = False


@dataclass
class AnalysisSummary:
    token_count: int = 0
    buy_count: int = 0
    sell_count: int = 0
    gmgn_realized_profit: AuditedValue = field(default_factory=lambda: missing("wallet_profits", "尚未获取"))
    gmgn_total_profit: AuditedValue = field(default_factory=lambda: missing("wallet_profits", "尚未获取"))
    open_positions: int = 0
    gas_total_usd: AuditedValue = field(default_factory=lambda: missing("wallet_activity", "尚未统计"))
    special_acquisition_count: int = 0
    missing_cost_count: int = 0


@dataclass
class CollectionScope:
    wallet_address: str
    chain: str
    period: str
    start_time: int
    end_time: int
    earliest_trade_ts: Optional[int] = None
    latest_trade_ts: Optional[int] = None
    report_trade_count: int = 0
    history_trade_count: int = 0
    api_pages: int = 0
    token_count: int = 0
    truncated_by_max: bool = False
    generated_at: str = ""
    version: str = ""
    primary_history_provider: str = "无"
    fallback_provider: str = "无"
    coverage_complete: str = "否"
    verified_empty: str = "否"
    actual_coverage_start: str = "无"
    actual_coverage_end: str = "无"
    history_pages: int = 0
    history_transactions: int = 0
    fallback_used: str = "否"
    coverage_reason: str = "无"


@dataclass
class ApiStats:
    requests: int = 0
    cache_hits: int = 0
    cache_misses: int = 0
    retries_429: int = 0
    errors: int = 0

    @property
    def cache_hit_rate(self) -> str:
        total = self.cache_hits + self.cache_misses
        if total <= 0:
            return "0%"
        return f"{(self.cache_hits / total) * 100:.1f}%"


@dataclass
class WalletReport:
    request: WalletAnalysisRequest
    tokens: list[TokenAnalysisResult]
    trades: list[TradeRecord]
    warnings: list[WarningRecord]
    summary: AnalysisSummary
    scope: CollectionScope
    api_stats: ApiStats
    gmgn_profit: GmgnProfit
    gmgn_stats: GmgnWalletStats
    raw_paths: list[str] = field(default_factory=list)
    elapsed_seconds: float = 0
    status: TaskStatus = TaskStatus.SUCCESS
    excel_path: str = ""
    json_path: str = ""
    task_id: str = ""
    job_id: str = ""
    wallet_task_id: str = ""
    provider_metrics: list[dict[str, Any]] = field(default_factory=list)
    coverage: dict[str, Any] = field(default_factory=dict)
    provider_health: list[dict[str, Any]] = field(default_factory=list)
