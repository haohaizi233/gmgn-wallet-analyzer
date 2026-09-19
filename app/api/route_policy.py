from __future__ import annotations

from dataclasses import dataclass

ROUTE_POLICIES = {
    "wallet_activity": 3,
    "wallet_stats": 3,
    "wallet_profits": 3,
    "token_info": 1,
    "token_pool_info": 1,
    "user_info": 2,
    "wallet_holdings": 5,
}


def route_weight(route_name: str) -> int:
    return int(ROUTE_POLICIES.get(route_name, 1))
