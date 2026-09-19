from __future__ import annotations

from app.domain.enums import ProviderCapability

DEFAULT_PRIORITY: dict[ProviderCapability, list[str]] = {
    ProviderCapability.WALLET_SWAPS: ["moralis", "solana_rpc", "helius", "gmgn"],
    ProviderCapability.WALLET_HISTORY: ["moralis", "solana_rpc", "helius"],
    ProviderCapability.WALLET_TRANSFERS: ["solana_rpc", "helius", "gmgn"],
    ProviderCapability.WALLET_BALANCES: ["solana_rpc", "helius", "moralis"],
    ProviderCapability.WALLET_HOLDINGS: ["gmgn", "solana_rpc", "helius"],
    ProviderCapability.TRANSACTION_DETAIL: ["solana_rpc", "helius"],
    ProviderCapability.TOKEN_METADATA: ["moralis", "helius", "solana_rpc", "gmgn"],
    ProviderCapability.TOKEN_MARKET: [
        "dexscreener",
        "jupiter",
        "geckoterminal",
        "defillama",
        "raydium",
        "pumpfun",
        "moralis",
        "gmgn",
        "helius",
    ],
    ProviderCapability.TOKEN_POOLS: [
        "dexscreener",
        "jupiter",
        "geckoterminal",
        "defillama",
        "raydium",
        "pumpfun",
        "gmgn",
        "moralis",
    ],
    ProviderCapability.TOKEN_CREATION: ["solana_rpc", "helius", "gmgn"],
    ProviderCapability.HISTORICAL_PRICE: ["moralis", "dexscreener", "gmgn", "helius"],
    ProviderCapability.PNL: ["local", "gmgn"],
    ProviderCapability.LAUNCHPAD: ["solana_rpc", "dexscreener", "gmgn"],
}

REPORT_PERIOD_HISTORY = [
    "MORALIS_WALLET_SWAPS",
    "SOLANA_RPC_HISTORY_FALLBACK",
    "HELIUS_OPTIONAL_HISTORY",
    "GMGN_DEEP_HISTORY",
]


class ProviderPriorityRegistry:
    def __init__(self, mapping: dict[ProviderCapability, list[str]] | None = None) -> None:
        self.mapping = dict(DEFAULT_PRIORITY)
        if mapping:
            self.mapping.update(mapping)

    def order(self, capability: ProviderCapability) -> list[str]:
        return list(self.mapping.get(capability, []))
