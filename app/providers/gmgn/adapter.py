from __future__ import annotations

from typing import Any, Optional

from app.api.exceptions import GMGNRateLimitError
from app.api.gmgn_client import GMGNClient
from app.domain.enums import ProviderCapability, ProviderHealth
from app.providers.base import DataProvider
from app.providers.exceptions import ProviderRateLimitError
from app.utils.logger import get_logger

logger = get_logger("gmgn.verifier")


class GMGNVerifierProvider(DataProvider):
    """辅助验证源。429 只打开本 Provider 熔断，不得影响其它数据源。"""

    name = "gmgn"
    optional = True
    capabilities = {
        ProviderCapability.WALLET_SWAPS,
        ProviderCapability.PNL,
        ProviderCapability.TOKEN_METADATA,
        ProviderCapability.TOKEN_POOLS,
        ProviderCapability.TOKEN_MARKET,
        ProviderCapability.LAUNCHPAD,
        ProviderCapability.HISTORICAL_PRICE,
        ProviderCapability.TOKEN_CREATION,
        ProviderCapability.WALLET_HOLDINGS,
    }

    def __init__(self, client: Optional[GMGNClient] = None, deep_history: bool = False):
        super().__init__()
        self.client = client
        self.deep_history = deep_history
        if client and getattr(client, "api_key", ""):
            self.health = ProviderHealth.HEALTHY
            self.health_detail = "辅助验证源"
            self.enabled = True
        else:
            self.health = ProviderHealth.NOT_CONFIGURED
            self.health_detail = "未配置 GMGN"
            self.enabled = False

    def _guard(self) -> None:
        if not self.circuit.allow():
            self.metrics.circuit_open += 1
            self.health = ProviderHealth.CIRCUIT_OPEN
            raise ProviderRateLimitError("GMGN circuit open", provider=self.name)

    def _wrap(self, fn, *args, **kwargs):
        self._guard()
        self.metrics.request_count += 1
        try:
            result = fn(*args, **kwargs)
            self.circuit.record_success()
            self.metrics.success += 1
            if self.health == ProviderHealth.CIRCUIT_OPEN:
                self.health = ProviderHealth.HEALTHY
            return result
        except GMGNRateLimitError as exc:
            self.metrics.count_429 += 1
            self.circuit.record_failure(429)
            self.health = ProviderHealth.RATE_LIMITED
            self.health_detail = "GMGN_UNAVAILABLE_RATE_LIMIT"
            logger.warning("GMGN 429，跳过并继续其它数据源: %s", exc)
            raise ProviderRateLimitError(str(exc), provider=self.name, reset_at=exc.reset_at) from exc
        except Exception:
            self.metrics.errors += 1
            self.circuit.record_failure(500)
            raise

    def get_wallet_stats(self, chain: str, wallet: str, period: str) -> dict[str, Any]:
        if not self.client:
            raise ProviderRateLimitError("GMGN 未配置", provider=self.name)
        return self._wrap(self.client.get_wallet_stats, chain, wallet, period)

    def get_wallet_profits(self, chain: str, wallets: list[str], period: str) -> dict[str, Any]:
        if not self.client:
            raise ProviderRateLimitError("GMGN 未配置", provider=self.name)
        return self._wrap(self.client.get_wallet_profits, chain, wallets, period)

    def get_token_info(self, chain: str, address: str) -> dict[str, Any]:
        if not self.client:
            raise ProviderRateLimitError("GMGN 未配置", provider=self.name)
        return self._wrap(self.client.get_token_info, chain, address)

    def get_token_pool_info(self, chain: str, address: str) -> dict[str, Any]:
        if not self.client:
            raise ProviderRateLimitError("GMGN 未配置", provider=self.name)
        return self._wrap(self.client.get_token_pool_info, chain, address)

    def get_wallet_activity(self, *args, **kwargs) -> dict[str, Any]:
        if not self.client:
            raise ProviderRateLimitError("GMGN 未配置", provider=self.name)
        if not self.deep_history:
            logger.info("跳过 GMGN 深历史（ENABLE_GMGN_DEEP_HISTORY_FALLBACK=false）")
        return self._wrap(self.client.get_wallet_activity, *args, **kwargs)

    def get_wallet_activity_for_token(self, *args, **kwargs) -> dict[str, Any]:
        """Token-scoped activity for first-buy recovery. Not a full-wallet deep crawl."""
        if not self.client:
            raise ProviderRateLimitError("GMGN 未配置", provider=self.name)
        return self._wrap(self.client.get_wallet_activity, *args, **kwargs)

    def get_wallet_holdings(self, *args, **kwargs) -> dict[str, Any]:
        if not self.client:
            raise ProviderRateLimitError("GMGN 未配置", provider=self.name)
        return self._wrap(self.client.get_wallet_holdings, *args, **kwargs)
