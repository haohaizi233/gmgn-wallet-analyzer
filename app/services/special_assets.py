from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from app.domain.enums import AcquisitionType
from app.domain.models import TokenInfo
from app.utils.paths import data_file

REGISTRY_PATH = data_file("special_assets.json")


@dataclass(frozen=True)
class SpecialAsset:
    token_address: str
    acquisition_type: AcquisitionType
    platform: str
    label: str
    reason: str
    source: str = ""
    display: str = ""


TYPE_MAP = {
    "WRAPPED": AcquisitionType.WRAPPED,
    "BRIDGE": AcquisitionType.BRIDGE,
    "AIRDROP": AcquisitionType.AIRDROP,
    "MINT": AcquisitionType.MINT,
    "TRANSFER_IN": AcquisitionType.TRANSFER_IN,
    "XSTOCK": AcquisitionType.TRANSFER_IN,
    "UNKNOWN": AcquisitionType.UNKNOWN,
}


class KnownAssetRegistry:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or REGISTRY_PATH
        self.by_mint: dict[str, SpecialAsset] = {}
        self.reload()

    def reload(self) -> None:
        self.by_mint = {}
        if not self.path.exists():
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        rows = payload.get("assets") if isinstance(payload, dict) else payload
        if not isinstance(rows, list):
            return
        for item in rows:
            if not isinstance(item, dict):
                continue
            mint = str(item.get("mint") or item.get("token_address") or "").strip()
            if not mint:
                continue
            kind = TYPE_MAP.get(str(item.get("type") or "UNKNOWN").upper(), AcquisitionType.UNKNOWN)
            display = str(item.get("display") or item.get("label") or mint)
            source = str(item.get("source") or "")
            self.by_mint[mint] = SpecialAsset(
                token_address=mint,
                acquisition_type=kind,
                platform=source or display,
                label=display,
                reason=str(item.get("reason") or f"{source} {display}".strip()),
                source=source,
                display=display,
            )

    def get(self, mint: str) -> Optional[SpecialAsset]:
        return self.by_mint.get(mint)


_REGISTRY = KnownAssetRegistry()


def lookup_special_asset(token_address: str, info: Optional[TokenInfo] = None) -> Optional[SpecialAsset]:
    known = _REGISTRY.get(token_address)
    if known:
        return known
    symbol = (info.symbol if info else "") or ""
    name = (info.name if info else "") or ""
    blob = f"{symbol} {name}".lower()
    if "xstock" in blob:
        return SpecialAsset(token_address, AcquisitionType.UNKNOWN, "xStocks", symbol or "xStock", "名称包含 xStock，按代币化股票处理", "xStocks", symbol or "xStock")
    if "wrapped" in blob:
        return SpecialAsset(token_address, AcquisitionType.WRAPPED, "Wrapped", symbol or "Wrapped", "名称包含 Wrapped", "Wrapped", symbol or "Wrapped")
    if any(word in blob for word in ("wormhole", "portal", "allbridge", "cbbtc", "bridge")):
        if "cbbtc" in blob or "coinbase" in blob:
            platform = "Coinbase"
        elif "wormhole" in blob or "portal" in blob:
            platform = "Wormhole"
        elif "allbridge" in blob:
            platform = "Allbridge"
        else:
            platform = "Bridge"
        return SpecialAsset(token_address, AcquisitionType.BRIDGE, platform, symbol or "Bridge", "名称匹配桥接资产特征", platform, symbol or platform)
    return None
