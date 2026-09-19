from __future__ import annotations

import json
import threading
import time
import uuid
from collections import defaultdict
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Callable, Optional

from app.api.exceptions import AnalysisCancelledError, GMGNError, GMGNRateLimitError
from app.providers.exceptions import ProviderRateLimitError
from app.providers.orchestrator import DataOrchestrator
from app.providers.moralis.convert import swap_to_trade
from app.providers.moralis.models import TokenMetadata, WalletSwapIndex
from app.resolvers.first_buy import resolve_first_buy, transfer_in_first_buy_fields
from app.resolvers.creation import resolve_creation_times
from app.resolvers.historical_mcap import resolve_entry_market_cap
from app.resolvers.market import resolve_numeric_consensus
from app.resolvers.pool import select_primary_pool
from app.domain.enums import DataSource
from app.api.gmgn_client import GMGNClient
from app.api.parsers import parse_wallet_profits, parse_wallet_stats
from app.domain.enums import (
    AcquisitionType,
    EventType,
    FieldStatus,
    ReportPeriod,
    TaskStatus,
    TokenPositionStatus,
    VerificationMode,
)
from app.domain.models import (
    AnalysisSummary,
    ApiStats,
    AuditedValue,
    CollectionScope,
    GmgnProfit,
    GmgnWalletStats,
    TokenAnalysisResult,
    TokenInfo,
    TradeRecord,
    WalletAnalysisRequest,
    WalletReport,
    error_value,
    known,
    missing,
    not_applicable,
)
from app.services.acquisition_resolver import resolve_acquisition
from app.services.activity_collector import ActivityCollector
from app.services.completeness_service import CompletenessService
from app.services.data_quality_gate import DataQualityGate
from app.services.export_service import ExportService
from app.services.platform_resolver import resolve_platforms, resolve_source_platform
from app.services.pnl_service import apply_fifo
from app.services.token_enricher import TokenEnricher
from app.storage.cache import CacheService
from app.storage.database import Database
from app.storage.repositories import Repositories
from app.utils.logger import get_logger
from app.utils.money import to_decimal
from app.utils.paths import RAW_DIR
from app.utils.time_utils import format_datetime, gmgn_profit_period, gmgn_stats_period, now_ts
from app.version import __version__
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

logger = get_logger("gmgn.analysis")
ProgressCb = Callable[[dict], None]


class WalletAnalysisService:
    def __init__(
        self,
        client: GMGNClient,
        db: Optional[Database] = None,
        cancel_event: Optional[threading.Event] = None,
        on_progress: Optional[ProgressCb] = None,
        token_ttl: int = 43200,
        pool_ttl: int = 1800,
        token_workers: int = 4,
        orchestrator: Optional[DataOrchestrator] = None,
        deep_gmgn_history: bool = False,
    ) -> None:
        self.client = client
        self.db = db or Database()
        self.repos = Repositories(self.db)
        self.cache = CacheService(self.repos, token_ttl, pool_ttl)
        self.cancel_event = cancel_event
        self.on_progress = on_progress
        self.collector = ActivityCollector(client, self.repos, cancel_event)
        self.enricher = TokenEnricher(client, self.cache)
        self.completeness = CompletenessService()
        self.exporter = ExportService()
        self.token_workers = max(1, min(8, int(token_workers)))
        self._progress_lock = threading.Lock()
        self.orchestrator = orchestrator
        self.deep_gmgn_history = deep_gmgn_history

    def analyze(self, request: WalletAnalysisRequest) -> WalletReport:
        started = time.time()
        job_id = request.job_id or request.task_id or str(uuid.uuid4())
        wallet_task_id = request.wallet_task_id or f"{job_id}:{request.wallet_address}"
        request.job_id = job_id
        request.wallet_task_id = wallet_task_id
        request.task_id = wallet_task_id
        task_id = wallet_task_id
        raw_dir = RAW_DIR / job_id / request.wallet_address[:16]
        raw_paths: list[str] = []
        raw_lock = threading.Lock()
        if request.options.save_raw_json:
            raw_dir.mkdir(parents=True, exist_ok=True)

            def on_raw(path: str, payload: dict) -> None:
                from app.storage.raw_dump import write_raw_json

                file = write_raw_json(job_id, request.wallet_address, path, payload)
                with raw_lock:
                    raw_paths.append(str(file))

            if self.client.on_raw is None:
                self.client.on_raw = on_raw

        self.repos.upsert_task(task_id, request.wallet_address, TaskStatus.RUNNING.value)
        self.repos.save_wallet_task(wallet_task_id, job_id, request.wallet_address, TaskStatus.RUNNING.value, current_stage="history")
        cookie_mode = bool(getattr(self.client, "cookie_mode", False))
        self._progress("activity", "采集报告周期交易", 0, 0, request.wallet_address, "")
        swap_index: WalletSwapIndex | None = None
        metadata_map: dict[str, TokenMetadata] = {}
        pool_map: dict = {}
        extra_mints: list[str] = []
        gmgn_future_stats = None
        gmgn_future_profit = None
        aux_pool: ThreadPoolExecutor | None = None

        if cookie_mode:
            logger.info("自动化采集：跳过 Helius 主历史，先走 GMGN 网页")
            self._progress("browser", "正在打开 GMGN 并采集钱包页", 0, 0, request.wallet_address, "")
            prepare = getattr(self.client, "prepare_wallet", None)
            if prepare:
                prepare(request.chain, request.wallet_address)
        elif self.orchestrator is not None:
            try:
                swap_index = self.orchestrator.collect_wallet_swaps(
                    request.wallet_address,
                    start_ts=request.start_time,
                    end_ts=request.end_time,
                    max_transactions=request.max_transactions,
                )
            except Exception as exc:
                logger.warning("多数据源采集失败: %s", exc)
                swap_index = None

        if cookie_mode:
            stats = self._load_stats(request)
            profit = self._load_profit(request)
        else:
            aux_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="gmgn-aux")
            try:
                gmgn_future_stats = aux_pool.submit(self._load_stats, request)
                gmgn_future_profit = aux_pool.submit(self._load_profit, request)
            except Exception:
                gmgn_future_stats = None
                gmgn_future_profit = None

        if self.orchestrator is not None and not cookie_mode:
            self._progress("universe", "补全当前持仓与官方代币名单", 0, 0, request.wallet_address, "")
            try:
                extra_mints = self.orchestrator.expand_token_universe(request.wallet_address, request.chain)
            except Exception as exc:
                logger.warning("代币宇宙补全失败，继续用流水名单: %s", exc)
                extra_mints = []

        if swap_index is not None:
            lifetime = list(swap_index.swaps)
            if request.period == ReportPeriod.ALL:
                report_swaps = lifetime
            else:
                report_swaps = [
                    item
                    for item in lifetime
                    if _in_report_window(item.block_timestamp, request.start_time, request.end_time)
                ]
            report_trades = [swap_to_trade(item, request.chain) for item in report_swaps]
            pages = swap_index.pages
            truncated = bool(request.max_transactions and len(lifetime) >= request.max_transactions)
            token_ids = list(dict.fromkeys(s.token_address for s in report_swaps if s.token_address))
            if extra_mints:
                token_ids = list(dict.fromkeys(list(token_ids) + extra_mints))
            if swap_index.coverage and swap_index.coverage.verified_empty and not extra_mints:
                token_ids = []
                report_trades = []
            logger.info(
                "钱包级索引 Token=%s extra=%s pages=%s complete=%s",
                len(token_ids),
                len(extra_mints),
                pages,
                bool(swap_index.complete),
            )
        elif cookie_mode or self.orchestrator is None or self.deep_gmgn_history:
            try:
                report_trades, pages, truncated = self.collector.collect_report_window(
                    request.chain,
                    request.wallet_address,
                    request.start_time,
                    request.end_time,
                    request.max_transactions,
                    on_progress=lambda msg: self._progress("activity", msg, 0, 0, request.wallet_address, ""),
                )
            except (GMGNRateLimitError, ProviderRateLimitError) as exc:
                logger.warning("GMGN 活动采集限流，继续无 GMGN 历史: %s", exc)
                report_trades, pages, truncated = [], 0, False
            token_ids = self._token_ids(report_trades)
            if extra_mints:
                token_ids = list(dict.fromkeys(list(token_ids) + extra_mints))
            if cookie_mode:
                logger.info("网页采集 Token=%s trades=%s pages=%s", len(token_ids), len(report_trades), pages)
        else:
            report_trades, pages, truncated = [], 0, False
            token_ids = list(dict.fromkeys(extra_mints))
            logger.warning("无可用核心历史且未启用 GMGN deep history，不把空结果当成 verified empty")
        if self.orchestrator is not None and token_ids:
            self._progress("metadata", f"批量 Token Metadata {len(token_ids)}", 0, len(token_ids), request.wallet_address, "")
            try:
                metadata_map = self.orchestrator.batch_metadata(token_ids)
            except Exception as exc:
                logger.warning("Metadata batch 失败: %s", exc)
            try:
                pool_map = self.orchestrator.batch_pools(token_ids)
            except Exception as exc:
                logger.warning("DEX batch 失败: %s", exc)
        self._stamp_trade_symbols(report_trades, metadata_map, pool_map)
        if not cookie_mode:
            try:
                stats = gmgn_future_stats.result() if gmgn_future_stats else self._load_stats(request)
                profit = gmgn_future_profit.result() if gmgn_future_profit else self._load_profit(request)
            except Exception as exc:
                logger.warning("GMGN 辅助统计失败，主流程继续: %s", exc)
                stats = GmgnWalletStats(request.wallet_address, gmgn_stats_period(request.period), status=FieldStatus.ERROR, reason=str(exc))
                profit = GmgnProfit(request.wallet_address, request.period.value, status=FieldStatus.ERROR, reason=str(exc))
            finally:
                if aux_pool is not None:
                    aux_pool.shutdown(wait=False)
        self.repos.upsert_task(task_id, request.wallet_address, TaskStatus.RUNNING.value, progress_total=len(token_ids))
        self.repos.save_wallet_task(
            wallet_task_id, job_id, request.wallet_address, TaskStatus.RUNNING.value, token_total=len(token_ids), current_stage="tokens"
        )
        workers = 1 if cookie_mode else self.token_workers
        logger.info("获取周期 Token：%s workers=%s cookie_mode=%s", len(token_ids), workers, cookie_mode)

        token_results: list[TokenAnalysisResult] = []
        all_trades: list[TradeRecord] = []
        warnings = []
        failed = 0
        done = 0
        result_map: dict[str, tuple[TokenAnalysisResult, list[TradeRecord]]] = {}
        max_pending = min(16, max(workers * 2, 4))
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="gmgn-token") as pool:
            pending: set = set()
            fut_token: dict = {}
            iterator = iter(token_ids)

            def submit_one() -> bool:
                try:
                    token_address = next(iterator)
                except StopIteration:
                    return False
                fut = pool.submit(
                    self._analyze_token,
                    request,
                    token_address,
                    report_trades,
                    swap_index,
                    metadata_map,
                    pool_map,
                )
                pending.add(fut)
                fut_token[fut] = token_address
                return True

            for _ in range(min(max_pending, len(token_ids) or 0)):
                if not submit_one():
                    break
            while pending:
                self._check_cancel()
                finished, pending = wait(pending, timeout=0.15, return_when=FIRST_COMPLETED)
                if not finished:
                    continue
                for fut in finished:
                    token_address = fut_token.pop(fut, "")
                    done += 1
                    symbol_hint = next((t.token_symbol for t in report_trades if t.token_address == token_address and t.token_symbol and t.token_symbol != "未知"), "")
                    if not symbol_hint:
                        meta = (metadata_map or {}).get(token_address)
                        symbol_hint = (getattr(meta, "symbol", "") if meta else "") or token_address[:6]
                    self._progress("token", f"Token {done}/{len(token_ids)} {symbol_hint}", done, len(token_ids), request.wallet_address, token_address)
                    self.repos.upsert_task(
                        task_id,
                        request.wallet_address,
                        TaskStatus.RUNNING.value,
                        current_token=token_address,
                        progress_done=done,
                        progress_total=len(token_ids),
                    )
                    self.repos.save_wallet_task(
                        wallet_task_id,
                        job_id,
                        request.wallet_address,
                        TaskStatus.RUNNING.value,
                        token_completed=done,
                        token_total=len(token_ids),
                        current_stage="First Buy",
                    )
                    try:
                        result, history = fut.result()
                        if request.options.pumpfun_only and str(result.source_platform.value) != "Pump.fun":
                            submit_one()
                            continue
                        result_map[token_address] = (result, history)
                        self.repos.save_token_result(task_id, request.wallet_address, token_address, result.status.value, {"symbol": result.symbol, "status": result.status.value})
                    except AnalysisCancelledError:
                        pool.shutdown(wait=False, cancel_futures=True)
                        raise
                    except Exception as exc:
                        failed += 1
                        logger.exception("Token 失败 %s: %s", token_address, exc)
                        token_results.append(self._failed_token(request, token_address, symbol_hint, str(exc)))
                        self.repos.save_token_result(task_id, request.wallet_address, token_address, TaskStatus.FAILED.value, {"error": str(exc)})
                    submit_one()

        for token_address in token_ids:
            pair = result_map.get(token_address)
            if not pair:
                continue
            result, history = pair
            token_results.append(result)
            all_trades.extend(history)
            warnings.extend(self.completeness.collect_warnings(result))

        seen_fp = {t.activity_fingerprint for t in all_trades}
        for trade in report_trades:
            if trade.activity_fingerprint not in seen_fp:
                trade.in_report_range = True
                all_trades.append(trade)
                seen_fp.add(trade.activity_fingerprint)

        in_range_keys = {t.activity_fingerprint for t in report_trades}
        for trade in all_trades:
            in_window = True
            if request.start_time and trade.timestamp and trade.timestamp < request.start_time:
                in_window = False
            if request.end_time and trade.timestamp and trade.timestamp > request.end_time:
                in_window = False
            trade.in_report_range = trade.activity_fingerprint in in_range_keys or in_window

        timestamps = [t.timestamp for t in all_trades if t.timestamp and t.in_report_range]
        summary = self._summary(token_results, all_trades, profit)
        coverage = None
        if swap_index is not None:
            coverage = swap_index.coverage
            if coverage is None and self.orchestrator is not None:
                coverage = getattr(self.orchestrator, "last_coverage", None)
        cov_start = "无"
        cov_end = "无"
        if coverage and coverage.actual_start_ts:
            cov_start = format_datetime(coverage.actual_start_ts)
        if coverage and coverage.actual_end_ts:
            cov_end = format_datetime(coverage.actual_end_ts)
        primary = "无"
        fallback = "无"
        if coverage:
            primary = getattr(coverage.provider, "value", None) or str(coverage.provider or "无")
            fallback = coverage.fallback_provider or ("是" if coverage.fallback_used else "无")
        elif swap_index is not None:
            primary = swap_index.provider or "无"
        scope = CollectionScope(
            wallet_address=request.wallet_address,
            chain=request.chain,
            period=request.period.value,
            start_time=request.start_time,
            end_time=request.end_time,
            earliest_trade_ts=min(timestamps) if timestamps else None,
            latest_trade_ts=max(timestamps) if timestamps else None,
            report_trade_count=sum(1 for t in all_trades if t.in_report_range),
            history_trade_count=len(all_trades),
            api_pages=(coverage.pages if coverage else self.collector.page_count) or pages,
            token_count=len(token_results),
            truncated_by_max=truncated,
            generated_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            version=__version__,
            primary_history_provider=primary or "无",
            fallback_provider=fallback or "无",
            coverage_complete="是" if coverage and coverage.complete else "否",
            verified_empty="是" if coverage and coverage.verified_empty else "否",
            actual_coverage_start=cov_start,
            actual_coverage_end=cov_end,
            history_pages=int(coverage.pages if coverage else pages or 0),
            history_transactions=int(coverage.transactions if coverage else len(all_trades)),
            fallback_used="是" if (coverage and coverage.fallback_used) or (swap_index and swap_index.fallback_used) else "否",
            coverage_reason=(coverage.termination_reason if coverage else "无") or "无",
        )
        if self.orchestrator is None:
            status = TaskStatus.SUCCESS
            if failed and token_results:
                status = TaskStatus.PARTIAL
            elif failed and not token_results:
                status = TaskStatus.FAILED
        else:
            status = DataQualityGate.evaluate(coverage, token_count=len(token_results), failed_tokens=failed)
            if failed and token_results and status == TaskStatus.SUCCESS:
                status = TaskStatus.PARTIAL
        report = WalletReport(
            request=request,
            tokens=token_results,
            trades=sorted(all_trades, key=lambda t: t.timestamp, reverse=True),
            warnings=warnings,
            summary=summary,
            scope=scope,
            api_stats=ApiStats(
                requests=self.client.request_count,
                cache_hits=self.cache.hits,
                cache_misses=self.cache.misses,
                retries_429=self.client.retry_429_count,
                errors=self.client.error_count + failed,
            ),
            gmgn_profit=profit,
            gmgn_stats=stats,
            raw_paths=raw_paths,
            elapsed_seconds=time.time() - started,
            status=status,
            task_id=task_id,
            job_id=job_id,
            wallet_task_id=wallet_task_id,
            provider_metrics=self.orchestrator.metrics_snapshot() if self.orchestrator else [],
            provider_health=[s.__dict__ if hasattr(s, "__dict__") else {"name": getattr(s, "name", ""), "health": getattr(s, "health", "")} for s in (self.orchestrator.health_snapshot() if self.orchestrator else [])],
        )
        if self.orchestrator:
            health = self.orchestrator.health_snapshot()
            report.provider_health = [
                {
                    "name": h.name,
                    "health": h.health.value if hasattr(h.health, "value") else str(h.health),
                    "detail": h.detail,
                    "circuit": h.circuit.value if hasattr(h.circuit, "value") else str(h.circuit),
                    "optional": h.optional,
                }
                for h in health
            ]
            report.provider_metrics = self.orchestrator.metrics_snapshot()
            report.coverage = self.completeness.coverage_metrics(token_results)
            self.orchestrator.persist_credits()
        self.exporter.export(report)
        self.repos.save_report(
            wallet_task_id,
            request.wallet_address,
            request.chain,
            request.period.value,
            request.start_time,
            request.end_time,
            status.value,
            {
                "token_count": summary.token_count,
                "buy_count": summary.buy_count,
                "sell_count": summary.sell_count,
                "excel_path": report.excel_path,
                "json_path": report.json_path,
            },
            report.excel_path,
            report.json_path,
            report.elapsed_seconds,
            job_id=job_id,
            wallet_task_id=wallet_task_id,
        )
        self.repos.upsert_task(task_id, request.wallet_address, status.value, progress_done=done, progress_total=len(token_ids))
        self.repos.save_wallet_task(
            wallet_task_id,
            job_id,
            request.wallet_address,
            status.value,
            token_completed=done,
            token_total=len(token_ids),
            finished_at=time.time(),
        )
        logger.info("%s 分析完成 tokens=%s elapsed=%.1fs coverage=%s", status.value, len(token_results), report.elapsed_seconds, scope.coverage_reason)
        return report

    def _analyze_token(
        self,
        request: WalletAnalysisRequest,
        token_address: str,
        report_trades: list[TradeRecord],
        swap_index: WalletSwapIndex | None = None,
        metadata_map: dict | None = None,
        pool_map: dict | None = None,
    ) -> tuple[TokenAnalysisResult, list[TradeRecord]]:
        symbol = next((t.token_symbol for t in report_trades if t.token_address == token_address and t.token_symbol and t.token_symbol != "未知"), "")
        if not symbol:
            meta_early = (metadata_map or {}).get(token_address)
            symbol = (getattr(meta_early, "symbol", "") if meta_early else "") or token_address[:6]
        self._progress("first_buy", f"正在追溯 {symbol} 首次买入", 0, 0, request.wallet_address, token_address)
        use_index = bool(swap_index is not None and not self.deep_gmgn_history)
        history: list[TradeRecord]
        pages = 0
        complete = True
        moralis_buy = None
        if use_index and self.orchestrator is not None:
            bucket = swap_index.by_token.get(token_address)
            if bucket is None or bucket.earliest_buy is None:
                try:
                    self.orchestrator.recover_token_history(request.wallet_address, token_address, swap_index)
                except Exception as exc:
                    logger.warning("token history recover 失败 %s: %s", token_address[:8], exc)
                bucket = swap_index.by_token.get(token_address)
            history = []
            if bucket:
                history = [swap_to_trade(s, request.chain) for s in bucket.buys + bucket.sells + bucket.transfers_in + bucket.transfers_out]
            try:
                moralis_buy = (bucket.earliest_buy if bucket else None) or self.orchestrator.earliest_buy(
                    request.wallet_address, token_address, swap_index
                )
            except Exception as exc:
                logger.warning("earliest buy 失败 %s: %s", token_address[:8], exc)
                moralis_buy = bucket.earliest_buy if bucket else None
            if moralis_buy:
                extra = swap_to_trade(moralis_buy, request.chain)
                if extra.activity_fingerprint not in {t.activity_fingerprint for t in history}:
                    extra.history_only = True
                    history.append(extra)
            pages = 1
            complete = True
        else:
            try:
                history, pages, complete = self.collector.collect_token_history(
                    request.chain,
                    request.wallet_address,
                    token_address,
                    on_progress=lambda msg: self._progress("first_buy", msg, 0, 0, request.wallet_address, token_address),
                )
            except (GMGNRateLimitError, ProviderRateLimitError) as exc:
                logger.warning("GMGN token history 限流，使用已有索引: %s", exc)
                history, pages, complete = [], 0, False
        merged = {t.activity_fingerprint for t in history}
        for trade in report_trades:
            if trade.token_address != token_address:
                continue
            if trade.activity_fingerprint not in merged:
                history.append(trade)
                merged.add(trade.activity_fingerprint)
        if not history:
            history = [t for t in report_trades if t.token_address == token_address]
        self._stamp_trade_symbols(history, metadata_map, pool_map)

        token_info = None
        pool_info = None
        meta = (metadata_map or {}).get(token_address)
        if meta is not None:
            token_info = _token_info_from_metadata(meta, token_address, request.chain)
        elif request.options.fetch_token_created_at or request.options.fetch_platform_pool:
            try:
                token_info = self.enricher.get_token_info(request.chain, token_address)
            except (GMGNRateLimitError, ProviderRateLimitError) as exc:
                logger.warning("GMGN token_info 限流，继续: %s", exc)
                token_info = TokenInfo(token_address=token_address, chain=request.chain, symbol=symbol, name=symbol, info_status=FieldStatus.ERROR, info_error="GMGN限流，已使用其它数据源验证")
        pairs = list((pool_map or {}).get(token_address) or [])
        if not pairs and request.options.fetch_platform_pool and self.orchestrator is not None:
            try:
                extra = self.orchestrator.token_pairs(token_address)
                if extra:
                    pairs = list(extra)
            except Exception as exc:
                logger.warning("DexScreener token pairs 失败 %s: %s", token_address[:8], exc)
        if pairs:
            primary = select_primary_pool(pairs, token_address)
            if primary:
                pool_info = _pool_info_from_pair(token_address, primary)

        current_price = _derive_current_price(pool_info, pairs, token_info, meta, token_address)
        sol_usd = self.orchestrator.sol_usd_price() if self.orchestrator is not None else None
        from app.resolvers.historical_price import fill_trade_mark_usd

        for trade in history:
            fill_trade_mark_usd(trade, current_price, sol_usd)

        acq = resolve_acquisition(
            history,
            token_info,
            detect_special=request.options.detect_transfer_bridge and request.options.autofill_missing,
        )
        if acq.acquisition_type == AcquisitionType.UNKNOWN and self.orchestrator is not None:
            try:
                bal = self.orchestrator.official_balance(request.wallet_address, token_address)
            except Exception:
                bal = None
            if bal is not None and bal > 0:
                from app.domain.enums import AcquisitionStatus
                from app.domain.models import AcquisitionInfo

                acq = AcquisitionInfo(
                    acquisition_type=AcquisitionType.TRANSFER_IN,
                    timestamp=None,
                    price_usd=None,
                    amount=bal,
                    cost_usd=None,
                    cost_sol=None,
                    gas_usd=None,
                    gas_sol=None,
                    market_cap=None,
                    status=AcquisitionStatus.NO_BUY_HISTORY,
                    source="solana_rpc",
                    reason="未发现 Buy，链上余额增加，判定为转入获得",
                )
        activity_platform = next((t.launchpad_platform for t in history if t.launchpad_platform), None)
        platforms = resolve_platforms(token_info, pool_info, activity_platform, autofill=request.options.autofill_missing)
        platform = platforms.display
        fifo = apply_fifo(history)
        if request.period == ReportPeriod.ALL:
            report_token_trades = history
        else:
            report_token_trades = [t for t in history if (not request.start_time or t.timestamp >= request.start_time) and (not request.end_time or t.timestamp <= request.end_time)]

        all_buys = [t for t in history if t.event_type == EventType.BUY]
        all_sells = [t for t in history if t.event_type == EventType.SELL]
        buy_total = _sum_optional([t.cost_usd for t in all_buys]) if all_buys else Decimal("0")
        sell_total = _sum_optional([t.cost_usd for t in all_sells]) if all_sells else Decimal("0")
        buy_total_estimated = bool(all_buys) and any(getattr(t, "cost_usd_estimated", False) for t in all_buys)
        sell_total_estimated = bool(all_sells) and any(getattr(t, "cost_usd_estimated", False) for t in all_sells)
        cost_est_any = any(getattr(t, "cost_usd_estimated", False) for t in history if t.cost_usd is not None)

        created = (
            known(token_info.creation_timestamp, "token_info.creation_timestamp")
            if token_info and token_info.creation_timestamp
            else missing("token_info.creation_timestamp", "GMGN 未提供")
        )
        open_at = (
            known(token_info.open_timestamp, "token_info.open_timestamp")
            if token_info and token_info.open_timestamp
            else missing("token_info.open_timestamp", "GMGN 未提供")
        )
        pool_created = missing("token_info.pool.creation_timestamp", "GMGN 未提供")
        if token_info and token_info.pool_created_at:
            pool_created = known(token_info.pool_created_at, "token_info.pool.creation_timestamp")
        elif pool_info and pool_info.creation_timestamp:
            pool_created = known(pool_info.creation_timestamp, "dexscreener.pairCreatedAt")
        times = resolve_creation_times(
            mint_created_at=int(created.value) if created.value not in (None, "") else None,
            pool_created_at=int(pool_created.value) if pool_created.value not in (None, "") else None,
        )
        if times["token_created_at"].value is not None:
            created = times["token_created_at"].to_audited()
        if times["pool_created_at"].value is not None:
            pool_created = times["pool_created_at"].to_audited()
        if created.value is None and pool_info and pool_info.creation_timestamp:
            created = known(int(pool_info.creation_timestamp), "dexscreener.pairCreatedAt", estimated=True, reason="用池创建时间近似代币创建时间")
        if created.value is None and self.orchestrator is not None and self.orchestrator.should_verify("creation"):
            try:
                from app.providers.solana.mint_resolver import MintCreationFinder
                from app.providers.solana.rpc_client import SolanaRpcProvider

                rpc = self.orchestrator.providers.get("solana_rpc")
                if isinstance(rpc, SolanaRpcProvider):
                    found = MintCreationFinder(rpc, self.orchestrator.cache).find(token_address, max_pages=1)
                    if found and found.get("creation_time"):
                        created = AuditedValue(
                            int(found["creation_time"]),
                            "solana_rpc.mint_creation",
                            FieldStatus.VERIFIED if found.get("verified") else FieldStatus.DIRECT,
                        )
            except Exception as exc:
                logger.warning("Mint creation 查找失败: %s", exc)
        if open_at.value is None and pool_created.value is not None:
            open_at = known(pool_created.value, pool_created.source, estimated=True, reason="用池创建时间近似开盘时间")
        elif open_at.value is None and created.value is not None:
            open_at = known(created.value, created.source, estimated=True, reason="用创建时间近似开盘时间")

        if acq.acquisition_type == AcquisitionType.BUY:
            first_buy_trade = min(
                (t for t in history if t.event_type == EventType.BUY and t.timestamp > 0),
                key=lambda t: (t.timestamp, t.tx_hash),
                default=None,
            )
            cost_est = bool(first_buy_trade and getattr(first_buy_trade, "cost_usd_estimated", False))
            first_buy_display = (
                known(
                    acq.cost_usd,
                    "wallet_activity.first_buy.cost_usd",
                    estimated=cost_est,
                    reason="SOL × 当前 SOL/USD 估算" if cost_est else "",
                )
                if acq.cost_usd is not None
                else missing("wallet_activity", "无法验证历史美元成本")
            )
            first_amount = known(acq.amount, "wallet_activity.first_buy.token_amount") if acq.amount is not None else missing("wallet_activity", "无法验证")
            first_time = known(acq.timestamp, "wallet_activity.first_buy.timestamp") if acq.timestamp else missing("wallet_activity", "无法验证")
            missing_core = acq.cost_usd is None or not acq.timestamp
            if moralis_buy and self.orchestrator is not None and (self.orchestrator.should_verify("first_buy") or missing_core):
                try:
                    verified = self.orchestrator.verify_tx(request.wallet_address, token_address, moralis_buy.transaction_hash)
                    sol_usd = self.orchestrator.sol_usd_price() if self.orchestrator is not None else None
                    fields = resolve_first_buy(moralis_buy, verified, sol_usd=sol_usd)
                    if "first_buy_time" in fields and fields["first_buy_time"].value:
                        first_time = fields["first_buy_time"].to_audited()
                        acq.timestamp = int(fields["first_buy_time"].value)
                    if "first_buy_amount" in fields and fields["first_buy_amount"].value is not None:
                        first_amount = fields["first_buy_amount"].to_audited()
                    if "first_buy_usd" in fields:
                        first_buy_display = fields["first_buy_usd"].to_audited()
                except Exception as exc:
                    logger.warning("链上核验 First Buy 失败: %s", exc)
        else:
            if acq.cost_usd is not None:
                first_buy_display = known(
                    acq.cost_usd,
                    "wallet_activity.transferIn.cost_usd",
                    estimated=True,
                    reason="转入按现价估值",
                )
            else:
                first_buy_display = missing("wallet_activity", "无法验证首次获得金额")
            first_amount = known(acq.amount, "wallet_activity.transferIn.token_amount") if acq.amount is not None else missing("wallet_activity", "无法验证")
            first_time = known(acq.timestamp, "wallet_activity.transferIn.timestamp") if acq.timestamp else missing("wallet_activity", "无首次获得时间")

        if acq.market_cap is not None:
            market_cap = known(
                acq.market_cap,
                acq.market_cap_source,
                estimated=acq.market_cap_is_estimated,
                reason="使用当前供给估算" if acq.market_cap_is_estimated else "",
            )
        else:
            price = acq.price_usd
            if price is None and moralis_buy is not None:
                price = moralis_buy.bought.usd_price
                if price is None and moralis_buy.bought.amount and (moralis_buy.bought.usd_amount or moralis_buy.total_value_usd):
                    usd = moralis_buy.bought.usd_amount or moralis_buy.total_value_usd
                    if usd is not None and moralis_buy.bought.amount:
                        price = abs(usd) / abs(moralis_buy.bought.amount)
            if price is None:
                price = current_price
            allow_rpc = bool(self.orchestrator and self.orchestrator.verification_mode == VerificationMode.STRICT)
            supply_now = _resolve_supply(token_info, meta, pool_info, pairs, token_address, self.orchestrator if allow_rpc else None)
            entry = resolve_entry_market_cap(price, None, supply_now)
            market_cap = entry.to_audited()
            if not _audited_filled(market_cap):
                primary_pair = select_primary_pool(pairs, token_address) if pairs else None
                live_mcap = None
                if primary_pair is not None:
                    live_mcap = primary_pair.market_cap or primary_pair.fdv
                if live_mcap not in (None, 0):
                    market_cap = known(
                        live_mcap,
                        getattr(primary_pair, "source", "") or "market",
                        estimated=True,
                        reason="无历史入场市值，用当前市值",
                    )

        fifo_profit = (
            known(fifo.realized_profit, "local_fifo", estimated=cost_est_any, reason="无已实现卖出" if fifo.realized_profit == 0 and not fifo.missing_cost_count else "")
            if fifo.realized_profit is not None
            else not_applicable("local_fifo", "无法验证历史成本" if fifo.missing_cost_count else "无已实现卖出")
        )
        realized_profit = fifo_profit
        if current_price is None:
            current_price = _derive_current_price(pool_info, pairs, token_info, meta, token_address)
        if fifo.current_balance <= 0:
            unrealized_profit = known(Decimal("0"), "local_fifo", reason="已清仓")
        elif current_price is not None:
            remaining = fifo.remaining_cost_usd if fifo.remaining_cost_usd is not None else Decimal("0")
            unrl = current_price * fifo.current_balance - remaining
            unrealized_profit = known(unrl, "local_fifo", estimated=True, reason="当前价 × 余额 − 剩余成本")
        else:
            unrealized_profit = missing("wallet_holdings", "当前价或剩余成本不足，无法估算未实现盈亏")
        realized_value = to_decimal(realized_profit.value) if _audited_filled(realized_profit) else None
        unrealized_value = to_decimal(unrealized_profit.value) if _audited_filled(unrealized_profit) else None
        if realized_value is not None and unrealized_value is not None:
            total_value = realized_value + unrealized_value
            total_profit = known(total_value, "local_fifo", estimated=unrealized_profit.estimated or cost_est_any)
            if buy_total not in (None, 0):
                total_profit_pnl = known(total_value / buy_total, "local_fifo", estimated=True)
            elif acq.cost_usd not in (None, 0):
                total_profit_pnl = known(total_value / acq.cost_usd, "local_fifo", estimated=True, reason="用首次获得金额作分母")
            else:
                total_profit_pnl = known(Decimal("0"), "local_fifo", estimated=True, reason="无成本基数，收益率记 0")
        else:
            total_profit = not_applicable("local_fifo", "无法验证历史成本" if fifo.missing_cost_count else "盈亏构成不完整")
            total_profit_pnl = not_applicable("local_fifo", "无法验证历史成本" if fifo.missing_cost_count else "盈亏构成不完整")
        position = TokenPositionStatus.OPEN if fifo.current_balance > 0 else TokenPositionStatus.CLOSED
        status = TaskStatus.SUCCESS
        notes = []
        if not complete:
            notes.append("历史分页未标记完成")
            status = TaskStatus.WARNING
        if fifo.missing_cost_count:
            notes.append(f"缺失成本 {fifo.missing_cost_count} 笔")
            status = TaskStatus.WARNING
        if acq.acquisition_type != AcquisitionType.BUY:
            notes.append(acq.reason)
        if token_info and token_info.info_status == FieldStatus.ERROR:
            notes.append(token_info.info_error)
            status = TaskStatus.WARNING

        from app.utils.text import token_display_labels

        pair_symbol = ""
        if pairs:
            primary_for_label = select_primary_pool(pairs, token_address)
            if primary_for_label is not None:
                pair_symbol = getattr(primary_for_label, "base_symbol", "") or ""
        symbol, name = token_display_labels(
            token_info.symbol if token_info else "",
            token_info.name if token_info else "",
            getattr(meta, "symbol", "") if meta is not None else "",
            getattr(meta, "name", "") if meta is not None else "",
            pair_symbol,
            symbol,
            fallback=token_address[:6] if token_address else "未知",
        )
        result = TokenAnalysisResult(
            wallet_address=request.wallet_address,
            token_address=token_address,
            chain=request.chain,
            symbol=symbol,
            name=name,
            source_platform=platform,
            launchpad_platform=platforms.launchpad_platform,
            asset_source=platforms.asset_source,
            liquidity_platform=platforms.liquidity_platform,
            primary_pool=platforms.primary_pool,
            acquisition=acq,
            created_at=created,
            open_at=open_at,
            pool_created_at=pool_created,
            time_diff_seconds=missing("local", "未计算"),
            buy_count=len(all_buys),
            buy_total_usd=buy_total,
            sell_count=len(all_sells),
            sell_total_usd=sell_total,
            buy_total_estimated=buy_total_estimated,
            sell_total_estimated=sell_total_estimated,
            realized_profit=realized_profit,
            unrealized_profit=unrealized_profit,
            total_profit=total_profit,
            total_profit_pnl=total_profit_pnl,
            fifo_realized_profit=fifo_profit,
            current_balance=fifo.current_balance,
            calculated_balance=fifo.current_balance,
            balance_authority="DERIVED_FROM_ACTIVITY",
            holding_duration_seconds=missing("local", "未计算"),
            missing_cost_count=fifo.missing_cost_count,
            missing_cost_sell_count=fifo.missing_cost_sell_count,
            missing_cost_token_amount=fifo.missing_cost_token_amount,
            status=status,
            warnings=notes or ["无"],
            position_status=position,
            first_buy_display=first_buy_display,
            first_buy_amount=first_amount,
            first_buy_time=first_time,
            market_cap=market_cap,
            report_trade_count=len(report_token_trades),
            history_page_count=pages,
            history_complete=complete,
            first_buy_verify_status=first_time.status.value,
            first_buy_source=first_time.source,
            created_verify_status=created.status.value,
            created_source=created.source,
            market_verify_status=market_cap.status.value,
            market_source=market_cap.source,
            platform_verify_status=platform.status.value,
            platform_source=platform.source,
            pnl_verify_status=fifo_profit.status.value,
        )
        official_balance = None
        strict_mode = bool(self.orchestrator and self.orchestrator.verification_mode == VerificationMode.STRICT)
        if self.orchestrator is not None and strict_mode:
            try:
                official_balance = self.orchestrator.official_balance(request.wallet_address, token_address)
            except Exception:
                official_balance = None
        if official_balance is not None:
            result.current_balance = official_balance
            result.balance_authority = "SOLANA_RPC"
            result.balance_verify_status = "VERIFIED"
            if fifo.current_balance != official_balance:
                result.warnings = [*(result.warnings or []), "BALANCE_MISMATCH"]
        else:
            result.balance_verify_status = "DERIVED"
        primary_pair = select_primary_pool(pairs, token_address) if pairs else None
        if meta is not None or pairs:
            from app.domain.enums import DataSource

            candidates = {}
            if meta is not None and meta.market_cap is not None:
                candidates[DataSource.HELIUS if (meta.raw or {}).get("token_info") else DataSource.MORALIS] = meta.market_cap
            if primary_pair and primary_pair.market_cap is not None:
                src = {
                    "jupiter": DataSource.JUPITER,
                    "geckoterminal": DataSource.GECKOTERMINAL,
                    "pumpfun": DataSource.PUMPFUN,
                    "defillama": DataSource.DEFILLAMA,
                    "raydium": DataSource.RAYDIUM,
                }.get(getattr(primary_pair, "source", "") or "", DataSource.DEXSCREENER)
                candidates[src] = primary_pair.market_cap
            if candidates:
                mcap_field = resolve_numeric_consensus("market_cap", candidates)
                result.current_market_cap = mcap_field.to_audited()
                result.market_verify_status = mcap_field.status.value
                result.market_source = mcap_field.primary_source.value
            fdv_cands = {}
            if meta is not None and meta.fully_diluted_value is not None:
                fdv_cands[DataSource.MORALIS] = meta.fully_diluted_value
            if primary_pair and primary_pair.fdv is not None:
                src = {
                    "jupiter": DataSource.JUPITER,
                    "geckoterminal": DataSource.GECKOTERMINAL,
                    "pumpfun": DataSource.PUMPFUN,
                    "defillama": DataSource.DEFILLAMA,
                    "raydium": DataSource.RAYDIUM,
                }.get(getattr(primary_pair, "source", "") or "", DataSource.DEXSCREENER)
                fdv_cands[src] = primary_pair.fdv
            if fdv_cands:
                result.fdv = resolve_numeric_consensus("fdv", fdv_cands).to_audited()
            result.audit_rows = _audit_rows(request.wallet_address, token_address, result, meta, primary_pair)
        if not _audited_filled(result.current_market_cap):
            supply_now = _resolve_supply(token_info, meta, pool_info, pairs, token_address, None)
            if current_price not in (None, 0) and supply_now not in (None, 0):
                result.current_market_cap = known(
                    current_price * supply_now,
                    "price × supply",
                    estimated=True,
                    reason="Dex 未提供 marketCap，用现价 × 供给估算",
                )
            elif primary_pair and primary_pair.fdv not in (None, 0):
                result.current_market_cap = known(
                    primary_pair.fdv,
                    "dexscreener.fdv",
                    estimated=True,
                    reason="Dex 未提供 marketCap，用 FDV 近似",
                )
        if not _audited_filled(result.fdv):
            if primary_pair and primary_pair.fdv not in (None, 0):
                result.fdv = known(primary_pair.fdv, "dexscreener.fdv", estimated=True)
            elif _audited_filled(result.current_market_cap):
                result.fdv = known(
                    result.current_market_cap.value,
                    result.current_market_cap.source,
                    estimated=True,
                    reason="用当前市值近似 FDV",
                )
        self.completeness.apply_time_fields(result, fifo.last_sell_ts)
        if result.warnings == ["无"] and notes:
            result.warnings = notes
        return result, history

    def _failed_token(self, request: WalletAnalysisRequest, token_address: str, symbol: str, error: str) -> TokenAnalysisResult:
        from app.domain.enums import AcquisitionStatus
        from app.domain.models import AcquisitionInfo

        acq = AcquisitionInfo(
            acquisition_type=AcquisitionType.UNKNOWN,
            timestamp=None,
            price_usd=None,
            amount=None,
            cost_usd=None,
            cost_sol=None,
            gas_usd=None,
            gas_sol=None,
            market_cap=None,
            status=AcquisitionStatus.API_UNAVAILABLE,
            source="wallet_activity",
            reason=error,
        )
        result = TokenAnalysisResult(
            wallet_address=request.wallet_address,
            token_address=token_address,
            chain=request.chain,
            symbol=symbol or "未知",
            name=symbol or "未知",
            source_platform=error_value("token_info", "该 Token 分析失败，不影响其他 Token"),
            acquisition=acq,
            created_at=error_value("token_info", "该 Token 分析失败"),
            open_at=error_value("token_info", "该 Token 分析失败"),
            pool_created_at=error_value("token_info", "该 Token 分析失败"),
            time_diff_seconds=error_value("local", "该 Token 分析失败"),
            buy_count=0,
            buy_total_usd=None,
            sell_count=0,
            sell_total_usd=None,
            realized_profit=error_value("local", "该 Token 分析失败"),
            unrealized_profit=error_value("local", "该 Token 分析失败"),
            total_profit=error_value("local", "该 Token 分析失败"),
            total_profit_pnl=error_value("local", "该 Token 分析失败"),
            fifo_realized_profit=error_value("local", "该 Token 分析失败"),
            current_balance=None,
            holding_duration_seconds=error_value("local", "该 Token 分析失败"),
            missing_cost_count=0,
            status=TaskStatus.FAILED,
            warnings=[error],
            first_buy_display=error_value("wallet_activity", "该 Token 分析失败"),
            first_buy_amount=error_value("wallet_activity", "该 Token 分析失败"),
            first_buy_time=error_value("wallet_activity", "该 Token 分析失败"),
            market_cap=error_value("wallet_activity", "该 Token 分析失败"),
        )
        return result

    def _load_stats(self, request: WalletAnalysisRequest) -> GmgnWalletStats:
        period = gmgn_stats_period(request.period)
        try:
            payload = self.client.get_wallet_stats(request.chain, request.wallet_address, period)
            stats = parse_wallet_stats(payload, request.wallet_address, period)
            logger.info("获取 wallet_stats 成功")
            return stats
        except AnalysisCancelledError:
            raise
        except (GMGNRateLimitError, ProviderRateLimitError) as exc:
            logger.warning("wallet_stats 限流，跳过 GMGN: %s", exc)
            return GmgnWalletStats(request.wallet_address, period, status=FieldStatus.ERROR, reason="GMGN限流，已使用其它数据源验证")
        except GMGNError as exc:
            logger.warning("wallet_stats 失败: %s", exc)
            return GmgnWalletStats(request.wallet_address, period, status=FieldStatus.ERROR, reason=str(exc))

    def _load_profit(self, request: WalletAnalysisRequest) -> GmgnProfit:
        native = gmgn_profit_period(request.period)
        if native is None:
            reason = "接口不支持该周期（仅 1d/7d/30d/all），官方利润见此说明，不使用伪造值"
            logger.warning(reason)
            return GmgnProfit(request.wallet_address, request.period.value, status=FieldStatus.API_MISSING, reason=reason)
        try:
            payload = self.client.get_wallet_profits(request.chain, [request.wallet_address], native)
            profit = parse_wallet_profits(payload, request.wallet_address, native)
            logger.info("获取 wallet_profits 成功 period=%s", native)
            return profit
        except AnalysisCancelledError:
            raise
        except (GMGNRateLimitError, ProviderRateLimitError) as exc:
            logger.warning("wallet_profits 限流，跳过 GMGN: %s", exc)
            return GmgnProfit(request.wallet_address, native, status=FieldStatus.ERROR, reason="GMGN限流，已使用其它数据源验证")
        except GMGNError as exc:
            logger.warning("wallet_profits 失败: %s", exc)
            return GmgnProfit(request.wallet_address, native, status=FieldStatus.ERROR, reason=str(exc))

    def _token_ids(self, trades: list[TradeRecord]) -> list[str]:
        ordered: list[str] = []
        seen: set[str] = set()
        for trade in trades:
            if trade.token_address in seen:
                continue
            seen.add(trade.token_address)
            ordered.append(trade.token_address)
        return ordered

    def _summary(self, tokens: list[TokenAnalysisResult], trades: list[TradeRecord], profit: GmgnProfit) -> AnalysisSummary:
        buy_count = sum(1 for t in trades if t.event_type == EventType.BUY and t.in_report_range)
        sell_count = sum(1 for t in trades if t.event_type == EventType.SELL and t.in_report_range)
        gas_values = [t.gas_usd for t in trades if t.in_report_range and t.gas_usd is not None]
        gas = known(sum(gas_values, Decimal("0")), "wallet_activity.gas_usd") if gas_values else missing("wallet_activity.gas_usd", "GMGN 未提供 Gas")
        special = sum(1 for t in tokens if t.acquisition.acquisition_type != AcquisitionType.BUY)
        missing_cost = sum(t.missing_cost_count for t in tokens)
        open_pos = sum(1 for t in tokens if t.position_status == TokenPositionStatus.OPEN)
        realized = known(profit.realized_profit, "wallet_profits.realized_profit") if profit.status == FieldStatus.KNOWN and profit.realized_profit is not None else (
            missing("wallet_profits", profit.reason or "GMGN 未提供") if profit.status != FieldStatus.ERROR else error_value("wallet_profits", profit.reason)
        )
        total = known(profit.total_profit, "wallet_profits.total_profit") if profit.status == FieldStatus.KNOWN and profit.total_profit is not None else (
            missing("wallet_profits", profit.reason or "GMGN 未提供") if profit.status != FieldStatus.ERROR else error_value("wallet_profits", profit.reason)
        )
        fifo_realized = [t.fifo_realized_profit.value for t in tokens if t.fifo_realized_profit.value is not None]
        fifo_total = [t.total_profit.value for t in tokens if t.total_profit.value is not None]
        if realized.value is None and fifo_realized:
            realized = known(sum((Decimal(str(v)) for v in fifo_realized), Decimal("0")), "local_fifo", estimated=True, reason="各 Token FIFO 合计")
        if total.value is None and fifo_total:
            total = known(sum((Decimal(str(v)) for v in fifo_total), Decimal("0")), "local_fifo", estimated=True, reason="各 Token FIFO 合计")
        return AnalysisSummary(
            token_count=len(tokens),
            buy_count=buy_count,
            sell_count=sell_count,
            gmgn_realized_profit=realized,
            gmgn_total_profit=total,
            open_positions=open_pos,
            gas_total_usd=gas,
            special_acquisition_count=special,
            missing_cost_count=missing_cost,
        )

    def _stamp_trade_symbols(self, trades: list[TradeRecord], metadata_map: dict | None, pool_map: dict | None) -> None:
        from app.utils.text import token_display_labels, visible_text

        for trade in trades or []:
            meta = (metadata_map or {}).get(trade.token_address)
            pair_symbol = ""
            for pair in (pool_map or {}).get(trade.token_address) or []:
                if getattr(pair, "base_address", "") == trade.token_address:
                    pair_symbol = getattr(pair, "base_symbol", "") or ""
                    break
            fallback = (trade.token_address or "")[:6] or "未知"
            symbol, name = token_display_labels(
                trade.token_symbol,
                trade.token_name,
                getattr(meta, "symbol", "") if meta is not None else "",
                getattr(meta, "name", "") if meta is not None else "",
                pair_symbol,
                fallback=fallback,
            )
            if not visible_text(trade.token_symbol) or trade.token_symbol == "未知":
                trade.token_symbol = symbol
            else:
                trade.token_symbol = visible_text(trade.token_symbol) or symbol
            trade.token_name = visible_text(trade.token_name) or name

    def _progress(self, stage: str, message: str, done: int, total: int, wallet: str, token: str) -> None:
        if stage not in {"first_buy", "token"}:
            logger.info("%s", message)
        if self.on_progress:
            with self._progress_lock:
                self.on_progress(
                    {
                        "stage": stage,
                        "message": message,
                        "done": done,
                        "total": total,
                        "wallet": wallet,
                        "token": token,
                    }
                )

    def _check_cancel(self) -> None:
        if self.cancel_event is not None and self.cancel_event.is_set():
            raise AnalysisCancelledError()


def _sum_optional(values: list[Optional[Decimal]]) -> Optional[Decimal]:
    present = [v for v in values if v is not None]
    if not present:
        return None
    return sum(present, Decimal("0"))


def _audited_filled(value: AuditedValue) -> bool:
    return value.value is not None and value.status in {
        FieldStatus.KNOWN,
        FieldStatus.VERIFIED,
        FieldStatus.CONSENSUS,
        FieldStatus.DIRECT,
        FieldStatus.DERIVED,
        FieldStatus.ESTIMATED,
    }


def _derive_current_price(pool_info, pairs, token_info, meta, token_address):
    if pool_info is not None and getattr(pool_info, "price", None) not in (None, 0):
        return pool_info.price
    primary = select_primary_pool(pairs, token_address) if pairs else None
    if primary is not None and getattr(primary, "price_usd", None) not in (None, 0):
        return primary.price_usd
    supply_now = _resolve_supply(token_info, meta, pool_info, pairs, token_address, None)
    mcap_now = None
    if primary is not None:
        mcap_now = getattr(primary, "market_cap", None) or getattr(primary, "fdv", None)
    if mcap_now not in (None, 0) and supply_now not in (None, 0):
        return mcap_now / supply_now
    return None


def _pool_info_from_pair(token_address: str, primary) -> TokenPoolInfo:
    from app.domain.models import TokenPoolInfo

    return TokenPoolInfo(
        token_address=token_address,
        pool_address=primary.pair_address,
        exchange=primary.dex_id,
        liquidity=primary.liquidity_usd,
        base_address=primary.base_address,
        quote_address=primary.quote_address,
        price=primary.price_usd,
        creation_timestamp=primary.pair_created_at,
        raw=primary.raw,
    )


def _resolve_supply(token_info, meta, pool_info, pairs, token_address: str, orchestrator) -> Optional[Decimal]:
    supply_now = token_info.total_supply if token_info else None
    if (supply_now is None or supply_now <= 0) and token_info and getattr(token_info, "circulating_supply", None):
        supply_now = token_info.circulating_supply
    if (supply_now is None or supply_now <= 0) and meta is not None:
        supply_now = getattr(meta, "total_supply_formatted", None) or getattr(meta, "total_supply", None)
    if (supply_now is None or supply_now <= 0) and orchestrator is not None:
        try:
            rpc_supply = orchestrator.token_supply(token_address)
            if rpc_supply is not None:
                supply_now = rpc_supply
        except Exception:
            pass
    if (supply_now is None or supply_now <= 0) and pool_info and pool_info.price not in (None, 0):
        primary_for_supply = select_primary_pool(pairs, token_address) if pairs else None
        mcap_now = None
        if primary_for_supply is not None:
            mcap_now = getattr(primary_for_supply, "market_cap", None) or getattr(primary_for_supply, "fdv", None)
        if mcap_now not in (None, 0):
            supply_now = mcap_now / pool_info.price
    return supply_now


def _token_info_from_metadata(meta: TokenMetadata, token_address: str, chain: str) -> TokenInfo:
    from app.utils.text import token_display_labels

    symbol, name = token_display_labels(meta.symbol, meta.name, fallback=token_address[:6] if token_address else "未知")
    return TokenInfo(
        token_address=token_address,
        chain=chain,
        symbol=symbol,
        name=name,
        total_supply=meta.total_supply_formatted or meta.total_supply,
        circulating_supply=meta.circulating_supply,
        raw=meta.raw,
        info_status=FieldStatus.KNOWN,
    )


def _audit_rows(wallet: str, mint: str, token: TokenAnalysisResult, meta, pair) -> list[dict]:
    rows = []
    fields = [
        ("首次买入时间", token.first_buy_time),
        ("创建时间", token.created_at),
        ("入场市值", token.market_cap),
        ("当前市值", token.current_market_cap),
        ("来源平台", token.source_platform),
        ("本地FIFO", token.fifo_realized_profit),
    ]
    for name, audited in fields:
        rows.append(
            {
                "钱包": wallet,
                "Mint": mint,
                "字段": name,
                "最终值": str(audited.export("text")),
                "状态": audited.status.value,
                "主数据源": audited.source,
                "其它数据源": "无",
                "Source A Value": str(audited.value) if audited.value is not None else "无",
                "Source B Value": "无",
                "差异": audited.reason or "无",
                "证据": audited.source,
                "备注": audited.reason or "无",
            }
        )
    return rows


def _in_report_window(ts: int, start_ts: int, end_ts: int) -> bool:
    if start_ts and ts and ts < start_ts:
        return False
    if end_ts and ts and ts > end_ts:
        return False
    return True
