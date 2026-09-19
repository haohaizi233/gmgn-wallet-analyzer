from __future__ import annotations

from decimal import Decimal
from typing import Any, Optional

from app.domain.enums import ProviderCapability, ProviderHealth
from app.providers.base import DataProvider, IndependentRateLimiter
from app.providers.cache import ProviderCache
from app.providers.exceptions import ProviderError
from app.providers.http import HttpProviderMixin
from app.providers.solana.transaction_parser import (
    TOKEN_2022_PROGRAM,
    TOKEN_PROGRAM,
    VerifiedTransaction,
    parse_verified_transaction,
)
from app.utils.money import to_decimal
from app.utils.logger import get_logger

logger = get_logger("gmgn.solana")

TX_TTL = 10 * 365 * 24 * 3600
MAX_SUPPORTED_TX_VERSION = 1
TX_READ_OPTIONS = {
    "encoding": "jsonParsed",
    "maxSupportedTransactionVersion": MAX_SUPPORTED_TX_VERSION,
    "commitment": "confirmed",
}


class SolanaRpcProvider(HttpProviderMixin, DataProvider):
    name = "solana_rpc"
    optional = False
    capabilities = {
        ProviderCapability.TRANSACTION_DETAIL,
        ProviderCapability.WALLET_BALANCES,
        ProviderCapability.TOKEN_CREATION,
        ProviderCapability.TOKEN_METADATA,
    }

    def __init__(self, rpc_url: str = "https://api.mainnet-beta.solana.com", cache: ProviderCache | None = None, session=None, rate: float = 2.0):
        super().__init__()
        self.rpc_url = (rpc_url or "https://api.mainnet-beta.solana.com").rstrip("/")
        self.cache = cache
        self._shared_session = session
        self.limiter = IndependentRateLimiter(rate=rate, capacity=max(2.0, rate), min_spacing=0.15)
        self.health = ProviderHealth.HEALTHY
        self.health_detail = self.rpc_url
        self.enabled = True
        self._rpc_id = 0

    def _call(self, method: str, params: list[Any]) -> Any:
        self._rpc_id += 1
        body = {"jsonrpc": "2.0", "id": self._rpc_id, "method": method, "params": params}
        payload = self.http_request("POST", self.rpc_url, json_body=body, headers={"Content-Type": "application/json"}, provider=self)
        if isinstance(payload, dict) and payload.get("error"):
            err = payload["error"]
            raise ProviderError(f"Solana RPC {method} error: {err}", provider=self.name, status=200)
        return payload.get("result") if isinstance(payload, dict) else payload

    def get_transaction(self, signature: str) -> Optional[dict[str, Any]]:
        if self.cache:
            hit = self.cache.get(self.name, "TRANSACTION_DETAIL", signature)
            if hit is not None:
                self.metrics.cache_hit += 1
                return hit
        result = self._call(
            "getTransaction",
            [signature, dict(TX_READ_OPTIONS)],
        )
        if result and self.cache:
            self.cache.set(self.name, "TRANSACTION_DETAIL", signature, result, TX_TTL)
        return result

    def verify_transaction(self, wallet: str, signature: str, mint: str = "") -> VerifiedTransaction:
        raw = self.get_transaction(signature)
        return parse_verified_transaction(raw, wallet, mint, signature)

    def get_signatures_for_address(self, address: str, *, limit: int = 100, before: str | None = None, until: str | None = None) -> list[dict[str, Any]]:
        cfg: dict[str, Any] = {"limit": max(1, min(1000, int(limit)))}
        if before:
            cfg["before"] = before
        if until:
            cfg["until"] = until
        result = self._call("getSignaturesForAddress", [address, cfg])
        return result if isinstance(result, list) else []

    def get_token_accounts_by_owner(self, owner: str, mint: str | None = None) -> list[dict[str, Any]]:
        filt: dict[str, Any]
        if mint:
            filt = {"mint": mint}
        else:
            filt = {"programId": TOKEN_PROGRAM}
        result = self._call("getTokenAccountsByOwner", [owner, filt, {"encoding": "jsonParsed"}])
        value = result.get("value") if isinstance(result, dict) else result
        rows = value if isinstance(value, list) else []
        if not mint:
            extra = self._call(
                "getTokenAccountsByOwner",
                [owner, {"programId": TOKEN_2022_PROGRAM}, {"encoding": "jsonParsed"}],
            )
            extra_val = extra.get("value") if isinstance(extra, dict) else extra
            if isinstance(extra_val, list):
                rows = list(rows) + extra_val
        return rows

    def list_owner_mints(self, owner: str, *, include_zero: bool = True) -> list[str]:
        from app.providers.token_universe import extract_mints_from_token_accounts

        return extract_mints_from_token_accounts(self.get_token_accounts_by_owner(owner, None), include_zero=include_zero)

    def get_token_balance(self, owner: str, mint: str) -> Optional[Decimal]:
        accounts = self.get_token_accounts_by_owner(owner, mint)
        total = Decimal("0")
        found = False
        for acc in accounts:
            parsed = (((acc.get("account") or {}).get("data") or {}).get("parsed") or {}).get("info") or {}
            tok = parsed.get("tokenAmount") if isinstance(parsed.get("tokenAmount"), dict) else {}
            amount = to_decimal(tok.get("uiAmount") if tok.get("uiAmount") is not None else tok.get("uiAmountString"))
            if amount is not None:
                total += amount
                found = True
        return total if found else None

    def get_token_supply(self, mint: str) -> Optional[Decimal]:
        result = self._call("getTokenSupply", [mint])
        value = result.get("value") if isinstance(result, dict) else {}
        return to_decimal(value.get("uiAmount") if value.get("uiAmount") is not None else value.get("uiAmountString"))

    def get_account_info(self, address: str) -> Optional[dict[str, Any]]:
        result = self._call("getAccountInfo", [address, {"encoding": "jsonParsed"}])
        return result.get("value") if isinstance(result, dict) else result

    def get_block_time(self, slot: int) -> Optional[int]:
        result = self._call("getBlockTime", [slot])
        try:
            return int(result) if result is not None else None
        except (TypeError, ValueError):
            return None

    def test_connection(self) -> dict[str, Any]:
        result = self._call("getHealth", [])
        return {"ok": True, "health": result, "url": self.rpc_url}
