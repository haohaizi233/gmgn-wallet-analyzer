from __future__ import annotations

import threading
from typing import Any, Callable, Optional

from app.api.gmgn_client import GMGNClient
from app.domain.enums import CircuitState, DataSource, HeliusCapStatus, ProviderCapability, ProviderHealth, VerificationMode
from app.providers.base import DataProvider, ProviderMetrics, ProviderStatus
from app.providers.cache import ProviderCache
from app.providers.dexscreener.client import DexScreenerProvider
from app.providers.exceptions import ProviderError, ProviderPlanError, ProviderRateLimitError
from app.providers.defillama.client import DefiLlamaProvider
from app.providers.geckoterminal.client import GeckoTerminalProvider
from app.providers.gmgn.adapter import GMGNVerifierProvider
from app.providers.helius.client import HeliusProvider, OFFICIAL_RPC
from app.providers.helius.history import collect_rpc_fallback, recover_mint_history
from app.providers.helius.parsers import history_to_swaps
from app.providers.jupiter.client import JupiterProvider
from app.providers.raydium.client import RaydiumProvider
from app.providers.moralis.client import MoralisProvider
from app.providers.moralis.models import TokenMetadata, TokenSwapIndex, WalletSwap, WalletSwapIndex
from app.providers.history_cache import merge_history_state, merge_swaps, row_to_swap, swap_to_row
from app.providers.priority import ProviderPriorityRegistry
from app.providers.pumpfun.client import PumpFunProvider
from app.providers.result import HistoryCoverage, coverage_from_swaps
from app.providers.solana.rpc_client import SolanaRpcProvider
from app.providers.solana.transaction_parser import parse_verified_transaction
from app.providers.token_universe import (
    HOLDINGS_MAX_PAGES,
    MAX_UNIVERSE_TOKENS,
    extract_mints_from_gmgn_holdings,
    extract_mints_from_helius_balances,
    is_quote_mint,
    merge_token_universe,
    trade_to_swap,
)
from app.storage.database import Database
from app.storage.repositories import Repositories
from app.utils.logger import get_logger

logger = get_logger("gmgn.orchestrator")
PUBLIC_RPC = "https://api.mainnet-beta.solana.com"


def resolve_solana_rpc_url(config) -> str:
    explicit = (getattr(config, "solana_rpc_url", "") or "").strip().rstrip("/")
    helius_key = (getattr(config, "helius_api_key", "") or "").strip()
    helius_rpc = (getattr(config, "helius_rpc_url", "") or "").strip()
    if helius_key and not helius_rpc:
        helius_rpc = f"{OFFICIAL_RPC}/?api-key={helius_key}"
    if explicit and explicit != PUBLIC_RPC:
        return explicit
    if helius_rpc:
        return helius_rpc
    return explicit or PUBLIC_RPC


class DataOrchestrator:
    def __init__(
        self,
        providers: list[DataProvider],
        db: Database | None = None,
        verification_mode: VerificationMode = VerificationMode.BALANCED,
        cancel_event=None,
        on_health: Callable[[list[ProviderStatus]], None] | None = None,
    ) -> None:
        self.providers = {p.name: p for p in providers}
        for p in providers:
            p.cancel_event = cancel_event
        self.priority = ProviderPriorityRegistry()
        self.db = db
        self.repos = Repositories(db) if db else None
        self.cache = ProviderCache(self.repos) if self.repos else None
        self.verification_mode = verification_mode
        self.on_health = on_health
        self.metadata_calls = 0
        self.dex_calls = 0
        self.last_history = None
        self.history_call_count = 0
        self.last_coverage: HistoryCoverage | None = None
        self.last_swap_index: WalletSwapIndex | None = None
        self._sol_usd = None
        self._sol_usd_loaded = False
        self._index_lock = threading.Lock()

    @classmethod
    def from_config(cls, config, gmgn_client: GMGNClient | None, db: Database | None, cancel_event=None, on_health=None) -> "DataOrchestrator":
        repos = Repositories(db) if db else None
        cache = ProviderCache(repos) if repos else None
        providers: list[DataProvider] = []
        enable_moralis = bool(getattr(config, "enable_moralis", False))
        moralis_keys = list(getattr(config, "moralis_api_keys", None) or [])
        if not moralis_keys:
            single = (getattr(config, "moralis_api_key", "") or "").strip()
            if single:
                moralis_keys = [single]
        providers.append(
            MoralisProvider(
                api_key=moralis_keys[0] if moralis_keys else "",
                api_keys=moralis_keys,
                base_url=getattr(config, "moralis_base", "https://solana-gateway.moralis.io"),
                cache=cache,
                enabled_flag=enable_moralis,
            )
        )
        rpc_url = resolve_solana_rpc_url(config)
        helius_keys = list(getattr(config, "helius_api_keys", None) or [])
        helius_key = (getattr(config, "helius_api_key", "") or "").strip()
        if helius_key and helius_key not in helius_keys:
            helius_keys.insert(0, helius_key)
        using_helius_rpc = "helius-rpc.com" in rpc_url
        providers.append(
            SolanaRpcProvider(
                rpc_url=rpc_url,
                cache=cache,
                rate=float(getattr(config, "solana_rpc_rate", 8.0 if using_helius_rpc else 2.0) or 2.0),
            )
        )
        providers.append(
            HeliusProvider(
                api_key=helius_keys[0] if helius_keys else "",
                api_keys=helius_keys,
                rpc_url=getattr(config, "helius_rpc_url", "") or "",
                cache=cache,
                target_rps=float(getattr(config, "helius_target_rps", 8.0) or 8.0),
                monthly_budget=int(getattr(config, "helius_monthly_credit_budget", 1_000_000) or 1_000_000),
            )
        )
        providers.append(DexScreenerProvider(cache=cache))
        providers.append(JupiterProvider(cache=cache))
        providers.append(GeckoTerminalProvider(cache=cache))
        providers.append(DefiLlamaProvider(cache=cache))
        providers.append(RaydiumProvider(cache=cache))
        providers.append(PumpFunProvider(cache=cache))
        enable_gmgn = bool(getattr(config, "enable_gmgn", True)) or bool(getattr(gmgn_client, "cookie_mode", False))
        deep = bool(getattr(config, "enable_gmgn_deep_history_fallback", False))
        gmgn = GMGNVerifierProvider(gmgn_client if enable_gmgn else None, deep_history=deep)
        if not enable_gmgn:
            gmgn.health = ProviderHealth.DISABLED
            gmgn.health_detail = "ENABLE_GMGN=false"
            gmgn.enabled = False
        providers.append(gmgn)
        mode_raw = str(getattr(config, "verification_mode", "BALANCED") or "BALANCED").upper()
        try:
            mode = VerificationMode[mode_raw]
        except KeyError:
            mode = VerificationMode.BALANCED
        orch = cls(providers, db=db, verification_mode=mode, cancel_event=cancel_event, on_health=on_health)
        helius = orch.providers.get("helius")
        rpc = orch.providers.get("solana_rpc")
        if using_helius_rpc and isinstance(helius, HeliusProvider) and helius.enabled and isinstance(rpc, SolanaRpcProvider):
            rpc.limiter = helius.rpc_limiter
        if isinstance(helius, HeliusProvider) and helius.enabled and orch.repos:
            helius.credits.load_month(orch.repos.get_helius_credit_usage())
        return orch

    @classmethod
    def gmgn_only(cls, client: GMGNClient, db: Database | None = None, cancel_event=None) -> "DataOrchestrator":
        return cls(
            [
                GMGNVerifierProvider(client, deep_history=True),
                DexScreenerProvider(),
                JupiterProvider(),
                GeckoTerminalProvider(),
                DefiLlamaProvider(),
                RaydiumProvider(),
                PumpFunProvider(),
                SolanaRpcProvider(),
                MoralisProvider(api_key="", enabled_flag=False),
                HeliusProvider(api_key=""),
            ],
            db=db,
            cancel_event=cancel_event,
        )

    def provider(self, name: str) -> Optional[DataProvider]:
        return self.providers.get(name)

    def health_snapshot(self) -> list[ProviderStatus]:
        rows = [p.status() for p in self.providers.values()]
        if self.on_health:
            try:
                self.on_health(rows)
            except Exception:
                pass
        return rows

    def metrics_snapshot(self) -> list[dict[str, Any]]:
        rows = []
        for p in self.providers.values():
            snap = p.metrics.snapshot()
            if isinstance(p, HeliusProvider):
                credits = p.credits.snapshot()
                snap["estimated_credits"] = credits.get("estimated_credits", 0)
                snap["unpriced_requests"] = credits.get("unpriced_requests", 0)
                snap["wallet_history_pages"] = p.history_pages
                snap["credit_note"] = "LOCAL ESTIMATE, not official remaining credits"
            else:
                snap["estimated_credits"] = "不适用"
            rows.append(snap)
        return rows

    def request_capability(self, capability: ProviderCapability) -> Optional[DataProvider]:
        for name in self.priority.order(capability):
            provider = self.providers.get(name)
            if provider is None:
                continue
            if not provider.has_capability(capability):
                continue
            if not provider.circuit.allow():
                provider.metrics.circuit_open += 1
                continue
            return provider
        return None

    def collect_wallet_swaps(self, wallet: str, start_ts: int = 0, end_ts: int = 0, max_transactions: int = 0) -> WalletSwapIndex:
        moralis = self.providers.get("moralis")
        rpc = self.providers.get("solana_rpc")
        helius = self.providers.get("helius")
        gmgn = self.gmgn()
        moralis_on = isinstance(moralis, MoralisProvider) and moralis.enabled and moralis.has_capability(ProviderCapability.WALLET_SWAPS)
        helius_on = isinstance(helius, HeliusProvider) and helius.enabled
        gmgn_deep = bool(gmgn and gmgn.enabled and getattr(gmgn, "deep_history", False))
        primary = "MORALIS" if moralis_on else ("HELIUS" if helius_on else "SOLANA_RPC")
        logger.info(
            "history.primary=%s history.fallback=SOLANA_RPC moralis.enabled=%s helius.optional=%s gmgn.deep_history=%s",
            primary,
            str(moralis_on).lower(),
            str(helius_on).lower(),
            str(gmgn_deep).lower(),
        )
        errors: list[str] = []
        fallback_used = False
        fallback_name = ""

        if moralis_on:
            circuit_blocked = False
            if moralis.circuit.state == CircuitState.OPEN:
                opened = float(getattr(moralis.circuit, "opened_at", 0) or 0)
                clock = getattr(moralis.circuit, "_clock", None)
                now = clock.monotonic() if clock else 0.0
                if now and now - opened < moralis.circuit.open_seconds:
                    circuit_blocked = True
            if circuit_blocked:
                logger.warning("moralis circuit OPEN, fallback=SOLANA_RPC")
                errors.append("CIRCUIT_OPEN")
                fallback_used = True
                fallback_name = "SOLANA_RPC"
            else:
                try:
                    index = self._collect_moralis_swaps(wallet, start_ts=start_ts, end_ts=end_ts, max_transactions=max_transactions)
                    if index.success:
                        return self._finish_index(index, fallback_used=False)
                    errors.append(index.coverage.termination_reason if index.coverage else "moralis_failed")
                    fallback_used = True
                    fallback_name = "SOLANA_RPC"
                    logger.warning("moralis failed after retries fallback=SOLANA_RPC")
                except ProviderError as exc:
                    errors.append(str(exc))
                    fallback_used = True
                    fallback_name = "SOLANA_RPC"
                    moralis.metrics.fallback_count += 1
                    logger.warning("moralis wallet swaps failed fallback=SOLANA_RPC: %s", exc)

        helius_index = None
        try:
            helius_index = self._try_helius_wallet_history(
                wallet,
                start_ts=start_ts,
                end_ts=end_ts,
                max_transactions=max_transactions,
                as_fallback=bool(moralis_on or fallback_used),
            )
        except Exception as exc:
            errors.append(str(exc))
            fallback_used = True
            fallback_name = "SOLANA_RPC"
            logger.warning("Helius wallet history 未处理异常，fallback=SOLANA_RPC: %s", exc)
        if helius_index is not None:
            try:
                return self._finish_index(helius_index, fallback_used=helius_index.fallback_used)
            except Exception as exc:
                errors.append(str(exc))
                fallback_used = True
                fallback_name = "SOLANA_RPC"
                logger.warning("Helius index 收尾失败，fallback=SOLANA_RPC: %s", exc)

        if isinstance(rpc, SolanaRpcProvider):
            try:
                self.history_call_count += 1
                hist = collect_rpc_fallback(rpc, wallet, start_ts=start_ts, end_ts=end_ts)
                if hist.transactions:
                    index = _index_from_history(
                        wallet,
                        hist,
                        provider="solana_rpc",
                        source=DataSource.SOLANA_RPC,
                        sol_usd=self.sol_usd_price(),
                    )
                    cov = coverage_from_swaps(
                        provider=DataSource.SOLANA_RPC,
                        start_ts=start_ts,
                        end_ts=end_ts,
                        timestamps=[s.block_timestamp for s in index.swaps],
                        pages=hist.pages,
                        complete=bool(hist.bottom_complete),
                        success=True,
                    )
                    cov.fallback_used = True
                    cov.fallback_provider = fallback_name or "SOLANA_RPC"
                    index.coverage = cov
                    index.success = True
                    index.complete = cov.complete
                    index.fallback_used = True
                    index.provider = "solana_rpc"
                    logger.info("RPC wallet history fallback wallet=%s tokens=%s", wallet[:8], len(index.by_token))
                    return self._finish_index(index, fallback_used=True)
                errors.append("rpc_empty")
            except Exception as exc:
                errors.append(str(exc))
                logger.warning("RPC wallet history fallback 失败: %s", exc)

        if gmgn_deep:
            logger.info("GMGN deep history fallback enabled, still not treating empty as verified")
            errors.append("gmgn_deep_history_not_authoritative")

        cov = HistoryCoverage(
            requested_start_ts=start_ts,
            requested_end_ts=end_ts,
            complete=False,
            verified_empty=False,
            provider=DataSource.MORALIS,
            termination_reason="unknown_empty",
            errors=errors,
            fallback_used=fallback_used,
            fallback_provider=fallback_name,
        )
        empty = WalletSwapIndex(
            wallet=wallet,
            coverage=cov,
            success=False,
            complete=False,
            fallback_used=fallback_used,
            provider="none",
        )
        logger.warning("history unknown_empty wallet=%s errors=%s", wallet[:8], errors)
        return self._finish_index(empty, fallback_used=fallback_used)

    def sol_usd_price(self):
        if self._sol_usd_loaded:
            return self._sol_usd
        self._sol_usd_loaded = True
        jup = self.providers.get("jupiter")
        if isinstance(jup, JupiterProvider) and jup.enabled:
            try:
                self._sol_usd = jup.get_sol_usd()
            except Exception as exc:
                logger.warning("Jupiter SOL/USD 获取失败: %s", exc)
                self._sol_usd = None
        if self._sol_usd is None:
            llama = self.providers.get("defillama")
            if isinstance(llama, DefiLlamaProvider) and llama.enabled:
                try:
                    self._sol_usd = llama.get_sol_usd()
                except Exception as exc:
                    logger.warning("DefiLlama SOL/USD 获取失败: %s", exc)
        if self._sol_usd is None:
            ray = self.providers.get("raydium")
            if isinstance(ray, RaydiumProvider) and ray.enabled:
                try:
                    self._sol_usd = ray.get_sol_usd()
                except Exception as exc:
                    logger.warning("Raydium SOL/USD 获取失败: %s", exc)
        if self._sol_usd is None:
            dex = self.providers.get("dexscreener")
            if isinstance(dex, DexScreenerProvider) and dex.enabled:
                try:
                    self._sol_usd = dex.get_sol_usd()
                except Exception as exc:
                    logger.warning("DEX SOL/USD 获取失败: %s", exc)
                    self._sol_usd = None
        return self._sol_usd

    def _try_helius_wallet_history(
        self,
        wallet: str,
        *,
        start_ts: int,
        end_ts: int,
        max_transactions: int,
        as_fallback: bool,
    ) -> WalletSwapIndex | None:
        helius = self.providers.get("helius")
        if not isinstance(helius, HeliusProvider) or not helius.enabled:
            return None
        try:
            helius.ensure_probed(self.repos)
        except Exception as exc:
            logger.warning("Helius capability probe 失败，跳过 wallet history: %s", exc)
            return None
        if not helius.cap_flags.get("WALLET_HISTORY"):
            return None
        try:
            until = None
            prev = self.repos.get_wallet_history_state(wallet, "helius") if self.repos else None
            if prev and prev.get("bottom_complete") and prev.get("newest_signature"):
                until = prev.get("newest_signature")
            self.history_call_count += 1
            hist = helius.collect_history(
                wallet,
                start_ts=start_ts,
                end_ts=end_ts,
                max_transactions=max_transactions,
                until_signature=until,
            )
            if not hist.transactions and not hist.bottom_complete:
                return None
            index = _index_from_history(
                wallet,
                hist,
                provider="helius",
                source=DataSource.HELIUS,
                sol_usd=self.sol_usd_price(),
            )
            cached = self._load_cached_swaps(wallet, "helius")
            if cached:
                index.swaps = merge_swaps(index.swaps, cached)
                index.by_token = _index_swaps(wallet, index.swaps)
                index.from_cache = True
            self._persist_swaps(wallet, "helius", index.swaps)
            self._persist_history_state(wallet, "helius", index.swaps, bottom_complete=bool(hist.bottom_complete))
            cov = coverage_from_swaps(
                provider=DataSource.HELIUS,
                start_ts=start_ts,
                end_ts=end_ts,
                timestamps=[s.block_timestamp for s in index.swaps],
                pages=hist.pages,
                complete=bool(hist.bottom_complete or cached),
                success=True,
            )
            cov.fallback_used = as_fallback
            cov.fallback_provider = "HELIUS" if as_fallback else ""
            index.coverage = cov
            index.success = True
            index.complete = cov.complete
            index.fallback_used = as_fallback
            index.provider = "helius"
            self.last_history = hist
            logger.info("Helius wallet history wallet=%s tokens=%s pages=%s", wallet[:8], len(index.by_token), hist.pages)
            return index
        except ProviderPlanError as exc:
            logger.warning("Helius Wallet History PLAN_UNAVAILABLE, skip: %s", exc)
            helius.cap_flags["WALLET_HISTORY"] = False
            helius.cap_status["WALLET_HISTORY"] = HeliusCapStatus.PLAN_UNAVAILABLE.value
            helius.metrics.fallback_count += 1
            return None
        except ProviderError as exc:
            logger.warning("Helius wallet history 失败: %s", exc)
            helius.metrics.fallback_count += 1
            return None
        except Exception as exc:
            logger.warning("Helius wallet history 解析失败，skip: %s", exc)
            helius.metrics.fallback_count += 1
            return None

    def _collect_moralis_swaps(self, wallet: str, start_ts: int, end_ts: int, max_transactions: int) -> WalletSwapIndex:
        moralis: MoralisProvider = self.providers["moralis"]  # type: ignore[assignment]
        kwargs: dict[str, Any] = {"limit": 100, "order": "DESC", "transaction_types": "buy,sell"}
        if end_ts:
            kwargs["to_date"] = end_ts
        until = None
        prev = self.repos.get_wallet_history_state(wallet, "moralis") if self.repos else None
        cached = self._load_cached_swaps(wallet, "moralis")
        if prev and prev.get("bottom_complete") and prev.get("newest_signature"):
            until = str(prev.get("newest_signature") or "") or None
        self.history_call_count += 1
        before = moralis.metrics.request_count
        swaps, pages, complete = moralis.iter_wallet_swaps(wallet, until_signature=until, **kwargs)
        if max_transactions and len(swaps) > max_transactions:
            swaps = swaps[:max_transactions]
            complete = False
        merged = merge_swaps(swaps, cached)
        from_cache = bool(cached)
        if cached or swaps:
            self._persist_swaps(wallet, "moralis", merged)
            self._persist_history_state(wallet, "moralis", merged, bottom_complete=complete or bool(prev and prev.get("bottom_complete")))
        cov = coverage_from_swaps(
            provider=DataSource.MORALIS,
            start_ts=start_ts,
            end_ts=end_ts,
            timestamps=[s.block_timestamp for s in merged],
            pages=pages,
            complete=complete or (bool(prev and prev.get("bottom_complete")) and bool(until)),
            success=True,
        )
        index = WalletSwapIndex(
            wallet=wallet,
            swaps=merged,
            pages=pages,
            by_token=_index_swaps(wallet, merged),
            coverage=cov,
            provider="moralis",
            success=True,
            complete=cov.complete,
            from_cache=from_cache,
        )
        logger.info(
            "Moralis wallet swaps wallet=%s tokens=%s pages=%s complete=%s verified_empty=%s requests=%s",
            wallet[:8],
            len(index.by_token),
            pages,
            cov.complete,
            cov.verified_empty,
            moralis.metrics.request_count - before,
        )
        return index

    def _load_cached_swaps(self, wallet: str, provider: str) -> list[WalletSwap]:
        if not self.repos:
            return []
        rows = self.repos.load_wallet_history_events(wallet, provider)
        return [row_to_swap(row) for row in rows if row.get("signature")]

    def _persist_swaps(self, wallet: str, provider: str, swaps: list[WalletSwap]) -> None:
        if not self.repos:
            return
        events = [swap_to_row(wallet, provider, item, idx) for idx, item in enumerate(swaps)]
        try:
            self.repos.save_wallet_history_events(wallet, provider, events)
        except Exception as exc:
            logger.warning("persist wallet_history_events 失败: %s", exc)

    def _finish_index(self, index: WalletSwapIndex, fallback_used: bool) -> WalletSwapIndex:
        index.fallback_used = index.fallback_used or fallback_used
        sol_usd = self.sol_usd_price()
        if sol_usd is not None and index.swaps:
            from app.resolvers.historical_price import enrich_swap_usd

            for item in index.swaps:
                enrich_swap_usd(item, sol_usd)
            index.by_token = _index_swaps(index.wallet, index.swaps)
        self.last_swap_index = index
        self.last_coverage = index.coverage
        return index

    def expand_token_universe(self, wallet: str, chain: str = "sol") -> list[str]:
        groups: list[list[str]] = []
        rpc = self.providers.get("solana_rpc")
        if isinstance(rpc, SolanaRpcProvider):
            try:
                mints = rpc.list_owner_mints(wallet, include_zero=True)
                groups.append(mints)
                logger.info("token universe spl_accounts=%s wallet=%s", len(mints), wallet[:8])
            except Exception as exc:
                logger.warning("SPL token accounts 枚举失败: %s", exc)
        helius = self.providers.get("helius")
        if isinstance(helius, HeliusProvider) and helius.enabled:
            try:
                helius.ensure_probed(self.repos)
            except Exception:
                pass
            if helius.cap_flags.get("WALLET_BALANCES"):
                try:
                    payload = helius.get_wallet_balances(wallet)
                    mints = extract_mints_from_helius_balances(payload)
                    groups.append(mints)
                    logger.info("token universe helius_balances=%s wallet=%s", len(mints), wallet[:8])
                except Exception as exc:
                    logger.warning("Helius balances 枚举失败: %s", exc)
        gmgn = self.gmgn()
        if gmgn and gmgn.enabled:
            try:
                mints = self._gmgn_holding_mints(chain, wallet)
                groups.append(mints)
                if mints:
                    logger.info("token universe gmgn_holdings=%s wallet=%s", len(mints), wallet[:8])
            except Exception as exc:
                logger.info("GMGN holdings 跳过（官方名单补全失败不影响主流程）: %s", exc)
        merged = merge_token_universe(*groups, limit=MAX_UNIVERSE_TOKENS)
        logger.info("token universe merged=%s wallet=%s", len(merged), wallet[:8])
        return merged

    def recover_token_history(self, wallet: str, mint: str, index: WalletSwapIndex | None = None) -> list[WalletSwap]:
        if not mint or is_quote_mint(mint):
            return []
        with self._index_lock:
            if index and mint in index.by_token and index.by_token[mint].earliest_buy:
                return []
        extra: list[WalletSwap] = []
        rpc = self.providers.get("solana_rpc")
        if isinstance(rpc, SolanaRpcProvider):
            try:
                txs = recover_mint_history(rpc, wallet, mint)
                extra.extend(history_to_swaps(wallet, txs, sol_usd=self.sol_usd_price()))
            except Exception as exc:
                logger.warning("ATA first-buy recover 失败 %s: %s", mint[:8], exc)
        has_buy = any((s.transaction_type or "").lower() == "buy" for s in extra)
        if not has_buy:
            extra.extend(self._gmgn_token_activity_swaps(wallet, mint))
        if not extra:
            return []
        if index is not None:
            with self._index_lock:
                index.swaps = merge_swaps(extra, index.swaps)
                index.by_token = _index_swaps(index.wallet, index.swaps)
        logger.info(
            "token recover mint=%s extra_swaps=%s buys=%s",
            mint[:8],
            len(extra),
            sum(1 for s in extra if (s.transaction_type or "").lower() == "buy"),
        )
        return extra

    def _gmgn_holding_mints(self, chain: str, wallet: str) -> list[str]:
        gmgn = self.gmgn()
        if not gmgn or not gmgn.enabled or not gmgn.client or not hasattr(gmgn.client, "get_wallet_holdings"):
            return []
        found: list[str] = []
        cursor = None
        for _ in range(HOLDINGS_MAX_PAGES):
            payload = gmgn.get_wallet_holdings(
                chain,
                wallet,
                cursor=cursor,
                limit=50,
                hide_airdrop=False,
                hide_closed=False,
                hide_abnormal=False,
            )
            page, nxt = extract_mints_from_gmgn_holdings(payload)
            found.extend(page)
            if not nxt or nxt == cursor:
                break
            cursor = nxt
        return found

    def _gmgn_token_activity_swaps(self, wallet: str, mint: str, *, chain: str = "sol", max_pages: int = 2) -> list[WalletSwap]:
        gmgn = self.gmgn()
        if not gmgn or not gmgn.enabled:
            return []
        from app.api.parsers import parse_activity_page

        cursor = None
        trades = []
        try:
            for _ in range(max_pages):
                payload = gmgn.get_wallet_activity_for_token(chain, wallet, token_address=mint, cursor=cursor, limit=50)
                page_trades, nxt = parse_activity_page(payload, wallet, chain)
                trades.extend(page_trades)
                if not nxt:
                    break
                cursor = nxt
        except Exception as exc:
            logger.info("GMGN token activity recover 跳过 %s: %s", mint[:8], exc)
            return []
        return [trade_to_swap(item) for item in trades if item.token_address == mint]

    def earliest_buy(self, wallet: str, token: str, index: WalletSwapIndex | None = None) -> Optional[WalletSwap]:
        if index and token in index.by_token and index.by_token[token].earliest_buy:
            return index.by_token[token].earliest_buy
        if self.last_history and getattr(self.last_history, "token_events", None):
            bucket = self.last_history.token_events.get(token)
            if bucket and bucket.earliest_buy:
                swaps = history_to_swaps(wallet, [bucket.earliest_buy], sol_usd=self.sol_usd_price())
                return swaps[0] if swaps else None
        return None

    def batch_metadata(self, mints: list[str]) -> dict[str, TokenMetadata]:
        out: dict[str, TokenMetadata] = {}
        helius = self.providers.get("helius")
        if isinstance(helius, HeliusProvider) and helius.enabled:
            try:
                helius.ensure_probed(self.repos)
            except Exception:
                pass
            if helius.cap_flags.get("DAS"):
                before = helius.metrics.request_count
                try:
                    assets = helius.get_asset_batch(mints)
                    self.metadata_calls = helius.metrics.request_count - before
                    for asset in assets:
                        item = helius.parse_das_metadata(asset)
                        if item:
                            out[item.mint] = item
                except ProviderError as exc:
                    logger.warning("Helius DAS metadata 失败: %s", exc)
                    helius.metrics.fallback_count += 1
        missing = [mint for mint in mints if mint and (mint not in out or not (out[mint].symbol or out[mint].name))]
        moralis = self.providers.get("moralis")
        if isinstance(moralis, MoralisProvider) and moralis.has_capability(ProviderCapability.TOKEN_METADATA) and missing:
            before = moralis.metrics.request_count
            try:
                items = moralis.get_metadata_batch(missing)
                self.metadata_calls = moralis.metrics.request_count - before
                for item in items:
                    if item.mint and (item.mint not in out or not (out[item.mint].symbol or out[item.mint].name)):
                        out[item.mint] = item
            except ProviderError as exc:
                logger.warning("Moralis metadata batch 失败: %s", exc)
        missing = [mint for mint in mints if mint and (mint not in out or not (out[mint].symbol or out[mint].name))]
        jup = self.providers.get("jupiter")
        if isinstance(jup, JupiterProvider) and jup.enabled and missing:
            try:
                for quote in jup.get_token_quotes(missing):
                    if not quote.mint:
                        continue
                    current = out.get(quote.mint)
                    if current is None:
                        out[quote.mint] = TokenMetadata(
                            mint=quote.mint,
                            name=quote.name or quote.symbol,
                            symbol=quote.symbol or quote.name,
                            market_cap=quote.market_cap,
                            fully_diluted_value=quote.fdv,
                            raw=quote.raw,
                        )
                        continue
                    if not current.symbol and quote.symbol:
                        current.symbol = quote.symbol
                    if not current.name and quote.name:
                        current.name = quote.name
                    if current.market_cap in (None, 0) and quote.market_cap not in (None, 0):
                        current.market_cap = quote.market_cap
                    if current.fully_diluted_value in (None, 0) and quote.fdv not in (None, 0):
                        current.fully_diluted_value = quote.fdv
            except ProviderError as exc:
                logger.warning("Jupiter metadata 失败: %s", exc)
        return out

    def batch_pools(self, mints: list[str]) -> dict[str, list]:
        grouped: dict[str, list] = {}
        dex = self.providers.get("dexscreener")
        if isinstance(dex, DexScreenerProvider) and dex.has_capability(ProviderCapability.TOKEN_POOLS):
            before = dex.metrics.request_count
            try:
                grouped = dex.get_tokens_batch(mints)
                self.dex_calls = dex.metrics.request_count - before
            except ProviderError as exc:
                logger.warning("DEX Screener batch 失败: %s", exc)
        return self._enrich_pools(grouped, mints)

    def token_pairs(self, mint: str) -> list:
        pairs: list = []
        dex = self.providers.get("dexscreener")
        if isinstance(dex, DexScreenerProvider) and dex.has_capability(ProviderCapability.TOKEN_POOLS):
            try:
                pairs = dex.get_token_pairs(mint) or []
            except ProviderError as exc:
                logger.warning("DEX Screener pairs 失败 %s: %s", mint[:8], exc)
        return self._enrich_pools({mint: pairs}, [mint]).get(mint, [])

    def _enrich_pools(self, grouped: dict[str, list], mints: list[str]) -> dict[str, list]:
        from app.providers.market_quotes import merge_quotes, pair_needs_fill

        for pairs in grouped.values():
            for pair in pairs or []:
                if getattr(pair, "fdv", None) in (None, 0) and getattr(pair, "market_cap", None) not in (None, 0):
                    pair.fdv = pair.market_cap
                if getattr(pair, "market_cap", None) in (None, 0) and getattr(pair, "fdv", None) not in (None, 0):
                    pair.market_cap = pair.fdv

        def still_need() -> list[str]:
            return [mint for mint in mints if mint and pair_needs_fill(grouped.get(mint) or [], mint)]

        def apply_quotes(label: str, quotes) -> None:
            nonlocal grouped
            if not quotes:
                return
            grouped = merge_quotes(grouped, quotes)
            logger.info("%s 补齐行情 %s/%s", label, len(quotes), len(still_need()) + len(quotes))

        need = still_need()
        if not need:
            return grouped

        jup = self.providers.get("jupiter")
        if isinstance(jup, JupiterProvider) and jup.enabled and need:
            try:
                apply_quotes("jupiter search/price", jup.get_token_quotes(need))
            except ProviderError as exc:
                logger.warning("Jupiter quotes 失败: %s", exc)

        need = still_need()
        gecko = self.providers.get("geckoterminal")
        if isinstance(gecko, GeckoTerminalProvider) and gecko.enabled and need:
            try:
                apply_quotes("geckoterminal", gecko.get_token_quotes(need))
            except ProviderError as exc:
                logger.warning("GeckoTerminal quotes 失败: %s", exc)

        need = still_need()
        llama = self.providers.get("defillama")
        if isinstance(llama, DefiLlamaProvider) and llama.enabled and need:
            try:
                apply_quotes("defillama", llama.get_token_quotes(need))
            except ProviderError as exc:
                logger.warning("DefiLlama quotes 失败: %s", exc)

        need = still_need()
        ray = self.providers.get("raydium")
        if isinstance(ray, RaydiumProvider) and ray.enabled and need:
            try:
                apply_quotes("raydium", ray.get_token_quotes(need))
            except ProviderError as exc:
                logger.warning("Raydium quotes 失败: %s", exc)

        need = still_need()
        dex = self.providers.get("dexscreener")
        if isinstance(dex, DexScreenerProvider) and dex.enabled and need:
            filled = 0
            for mint in list(need):
                if not pair_needs_fill(grouped.get(mint) or [], mint):
                    continue
                try:
                    extra = dex.get_token_pairs(mint) or []
                except ProviderError as exc:
                    logger.warning("DEX token-pairs 失败 %s: %s", mint[:8], exc)
                    extra = []
                if extra:
                    current = list(grouped.get(mint) or [])
                    grouped[mint] = current + extra if current else extra
                    filled += 1
            if filled:
                logger.info("dexscreener token-pairs 补齐 %s", filled)

        need = still_need()
        pump = self.providers.get("pumpfun")
        if isinstance(pump, PumpFunProvider) and pump.enabled and need:
            try:
                apply_quotes("pump.fun", pump.get_token_quotes(need))
            except ProviderError as exc:
                logger.warning("Pump.fun quotes 失败: %s", exc)

        need = still_need()
        if isinstance(jup, JupiterProvider) and jup.enabled and need:
            quotes = []
            for mint in need:
                try:
                    extra = jup.quote_usd_as_market(mint)
                except ProviderError as exc:
                    logger.warning("Jupiter swap quote 失败 %s: %s", mint[:8], exc)
                    extra = None
                if extra:
                    quotes.append(extra)
            apply_quotes("jupiter swap-quote", quotes)
        still = still_need()
        if still:
            logger.info("行情仍缺 %s：%s", len(still), ",".join(item[:8] for item in still[:12]))
        return grouped

    def verify_tx(self, wallet: str, mint: str, signature: str):
        helius = self.providers.get("helius")
        if isinstance(helius, HeliusProvider) and helius.enabled:
            try:
                raw = helius.get_transaction(signature)
                if raw:
                    return parse_verified_transaction(raw, wallet, mint, signature)
            except ProviderError as exc:
                logger.warning("Helius verify 失败 %s: %s", signature[:8], exc)
        rpc = self.providers.get("solana_rpc")
        if isinstance(rpc, SolanaRpcProvider) and rpc.has_capability(ProviderCapability.TRANSACTION_DETAIL):
            try:
                return rpc.verify_transaction(wallet, signature, mint)
            except ProviderError as exc:
                logger.warning("RPC verify 失败 %s: %s", signature[:8], exc)
        return None

    def official_balance(self, wallet: str, mint: str):
        rpc = self.providers.get("solana_rpc")
        if isinstance(rpc, SolanaRpcProvider):
            try:
                return rpc.get_token_balance(wallet, mint)
            except ProviderError as exc:
                logger.warning("RPC balance 失败: %s", exc)
        helius = self.providers.get("helius")
        if isinstance(helius, HeliusProvider) and helius.cap_flags.get("WALLET_BALANCES"):
            try:
                payload = helius.get_wallet_balances(wallet)
                tokens = payload.get("tokens") if isinstance(payload, dict) else None
                if isinstance(tokens, list):
                    for row in tokens:
                        if str(row.get("mint") or "") == mint:
                            return row.get("amount") or row.get("uiAmount")
            except ProviderError:
                pass
        moralis = self.providers.get("moralis")
        if isinstance(moralis, MoralisProvider) and moralis.has_capability(ProviderCapability.WALLET_BALANCES):
            try:
                port = moralis.get_portfolio(wallet)
                return port.tokens.get(mint)
            except ProviderError:
                return None
        return None

    def token_supply(self, mint: str):
        rpc = self.providers.get("solana_rpc")
        if isinstance(rpc, SolanaRpcProvider):
            try:
                return rpc.get_token_supply(mint)
            except ProviderError:
                return None
        return None

    def gmgn(self) -> Optional[GMGNVerifierProvider]:
        p = self.providers.get("gmgn")
        return p if isinstance(p, GMGNVerifierProvider) else None

    def should_verify(self, kind: str, rng=None) -> bool:
        mode = self.verification_mode
        helius = self.providers.get("helius")
        if isinstance(helius, HeliusProvider) and not helius.credits.allow_optional() and kind == "sample":
            return False
        if mode == VerificationMode.STRICT:
            return True
        if mode == VerificationMode.FAST:
            return False
        if kind in {"first_buy", "creation"}:
            return False
        if kind == "transfer":
            return True
        if kind == "conflict":
            return True
        if kind == "sample":
            import random

            return (rng or random).random() < 0.05
        return False

    def persist_credits(self) -> None:
        helius = self.providers.get("helius")
        if isinstance(helius, HeliusProvider) and self.repos:
            try:
                self.repos.save_helius_credit_usage(helius.credits.snapshot())
            except Exception as exc:
                logger.warning("保存 Helius credit 估算失败: %s", exc)

    def _persist_history_state(self, wallet: str, provider: str, swaps: list[WalletSwap] | None = None, bottom_complete: bool = False, hist=None) -> None:
        if not self.repos:
            return
        rows: list[tuple[str, int]] = []
        if swaps:
            rows = [(s.transaction_hash, int(s.block_timestamp or 0)) for s in swaps if s.transaction_hash]
        elif hist is not None and getattr(hist, "transactions", None):
            rows = [(tx.signature, int(tx.timestamp or 0)) for tx in hist.transactions]
            bottom_complete = bool(getattr(hist, "bottom_complete", False))
            provider = provider or "helius"
        prev = self.repos.get_wallet_history_state(wallet, provider)
        merged = merge_history_state(prev, rows, bottom_complete)
        self.repos.save_wallet_history_state(
            wallet,
            provider,
            bottom_complete=bool(merged["bottom_complete"]),
            oldest_signature=str(merged["oldest_signature"] or ""),
            oldest_block_time=int(merged["oldest_block_time"] or 0),
            newest_signature=str(merged["newest_signature"] or ""),
            newest_block_time=int(merged["newest_block_time"] or 0),
        )


def _index_from_history(wallet: str, hist, provider: str = "helius", source: DataSource | None = None, sol_usd=None) -> WalletSwapIndex:
    swaps = history_to_swaps(wallet, hist.transactions, sol_usd=sol_usd)
    if source is not None:
        for item in swaps:
            item.source = source
    return WalletSwapIndex(wallet=wallet, swaps=swaps, pages=hist.pages, by_token=_index_swaps(wallet, swaps), provider=provider)


def _index_swaps(wallet: str, swaps: list[WalletSwap]) -> dict[str, TokenSwapIndex]:
    by_token: dict[str, TokenSwapIndex] = {}
    for swap in swaps:
        token = swap.token_address
        if not token:
            continue
        bucket = by_token.setdefault(token, TokenSwapIndex(token_address=token))
        kind = (swap.transaction_type or "").lower()
        if kind == "buy":
            bucket.buys.append(swap)
        elif kind == "sell":
            bucket.sells.append(swap)
        elif kind in {"transfer_in", "transferin"}:
            bucket.transfers_in.append(swap)
        elif kind in {"transfer_out", "transferout"}:
            bucket.transfers_out.append(swap)
        else:
            bucket.buys.append(swap)
    for bucket in by_token.values():
        if bucket.buys:
            bucket.earliest_buy = min(bucket.buys, key=lambda s: (s.block_timestamp or 0, s.transaction_hash))
            bucket.latest_buy = max(bucket.buys, key=lambda s: (s.block_timestamp or 0, s.transaction_hash))
        buy_usd = [s.bought.usd_amount or s.total_value_usd for s in bucket.buys]
        sell_usd = [s.sold.usd_amount or s.total_value_usd for s in bucket.sells]
        present_b = [v for v in buy_usd if v is not None]
        present_s = [v for v in sell_usd if v is not None]
        bucket.total_buy_usd = sum(present_b, start=present_b[0] * 0) if present_b else None
        bucket.total_sell_usd = sum((abs(v) for v in present_s), start=present_s[0] * 0) if present_s else None
    return by_token
