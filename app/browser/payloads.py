from __future__ import annotations

import re
from typing import Any, Optional

from app.api.parsers import as_dict, first_present, unwrap_payload

TOKEN_HREF_RE = re.compile(r"/sol/token/([1-9A-HJ-NP-Za-km-z]{32,44})")
ADDRESS_HREF_RE = re.compile(r"/sol/address/([1-9A-HJ-NP-Za-km-z]{32,44})")
ACTIVITY_KEYS = (
    "event_type",
    "tx_hash",
    "transaction_hash",
    "token_address",
    "base_address",
    "block_unix_time",
    "timestamp",
    "side",
    "cost_usd",
    "amount_usd",
)


def _looks_like_activity(item: Any) -> bool:
    if not isinstance(item, dict):
        return False
    hits = sum(1 for key in ACTIVITY_KEYS if key in item and item.get(key) not in (None, ""))
    if hits >= 2:
        return True
    token = item.get("token")
    return hits >= 1 and isinstance(token, dict) and bool(token.get("address") or token.get("token_address"))


def _walk_lists(payload: Any, bucket: list[list[Any]], depth: int = 0) -> None:
    if depth > 6:
        return
    if isinstance(payload, list):
        if payload and all(isinstance(x, dict) for x in payload[:8]):
            bucket.append(payload)
        for item in payload[:80]:
            _walk_lists(item, bucket, depth + 1)
        return
    if not isinstance(payload, dict):
        return
    for key in ("activities", "list", "history", "trades", "items", "records", "data"):
        value = payload.get(key)
        if isinstance(value, list):
            bucket.append(value)
            for item in value[:80]:
                _walk_lists(item, bucket, depth + 1)
        elif isinstance(value, dict):
            _walk_lists(value, bucket, depth + 1)


def extract_activity_items(payload: Any) -> list[dict[str, Any]]:
    data = unwrap_payload(payload)
    lists: list[list[Any]] = []
    _walk_lists(data, lists)
    found: list[dict[str, Any]] = []
    seen: set[str] = set()
    for rows in lists:
        activity_rows = [row for row in rows if _looks_like_activity(row)]
        if len(activity_rows) < max(1, len(rows) // 3) and not (rows and _looks_like_activity(rows[0])):
            continue
        for row in activity_rows:
            key = _activity_key(row)
            if key in seen:
                continue
            seen.add(key)
            found.append(row)
    return found


def _activity_key(item: dict[str, Any]) -> str:
    token = as_dict(item.get("token"))
    token_address = str(
        first_present(item, "token_address", "base_address")
        or first_present(token, "address", "token_address")
        or ""
    )
    tx = str(first_present(item, "tx_hash", "transaction_hash", "hash") or "")
    ts = str(first_present(item, "timestamp", "block_unix_time", "time") or "")
    side = str(first_present(item, "event_type", "type", "side") or "")
    return f"{tx}|{token_address}|{ts}|{side}"


def activity_timestamp(item: dict[str, Any]) -> int:
    raw = first_present(item, "timestamp", "block_unix_time", "time", "block_time")
    try:
        value = int(float(raw))
    except (TypeError, ValueError):
        return 0
    if value > 10_000_000_000:
        value //= 1000
    return value


def oldest_activity_ts(items: list[dict[str, Any]]) -> int:
    stamps = [activity_timestamp(item) for item in items if activity_timestamp(item) > 0]
    return min(stamps) if stamps else 0


def activity_token_labels(item: dict[str, Any]) -> list[str]:
    """Visible names/symbols used in the Activity table (no mint href on the row)."""
    if not isinstance(item, dict):
        return []
    token = as_dict(item.get("token"))
    ordered: list[str] = []
    for raw in (
        first_present(token, "symbol", "token_symbol"),
        first_present(item, "symbol", "token_symbol"),
        first_present(token, "name", "token_name"),
        first_present(item, "name", "token_name"),
    ):
        text = str(raw or "").strip()
        if text and text not in ordered:
            ordered.append(text)
    return ordered


def extract_token_mints(*blobs: Any) -> list[str]:
    ordered: list[str] = []
    seen: set[str] = set()

    def add(mint: str) -> None:
        text = (mint or "").strip()
        if len(text) < 32 or text in seen:
            return
        seen.add(text)
        ordered.append(text)

    for blob in blobs:
        if isinstance(blob, str):
            for match in TOKEN_HREF_RE.findall(blob):
                add(match)
            continue
        if isinstance(blob, dict):
            token = as_dict(blob.get("token"))
            add(str(first_present(blob, "token_address", "base_address", "address") or ""))
            add(str(first_present(token, "address", "token_address") or ""))
            add(str(blob.get("token_address") or ""))
            continue
        if isinstance(blob, list):
            for item in blob:
                for mint in extract_token_mints(item):
                    add(mint)
    return ordered


def classify_url(url: str) -> str:
    text = (url or "").lower()
    if "search" in text:
        return "search"
    if "wallet_activity" in text or "activity" in text:
        return "activity"
    if "holding" in text:
        return "holdings"
    if "wallet_stat" in text or "wallet/stat" in text:
        return "stats"
    if "profit" in text or "pnl" in text:
        return "profits"
    if "token_info" in text or "/tokens/" in text or "token/" in text:
        return "token"
    if "pool" in text:
        return "pool"
    return "other"


def as_openapi_page(items: list[dict[str, Any]], cursor: Optional[str], limit: int) -> dict[str, Any]:
    start = 0
    if cursor:
        try:
            start = max(0, int(cursor))
        except ValueError:
            start = 0
    size = max(1, min(50, int(limit or 50)))
    chunk = items[start : start + size]
    nxt: Optional[str] = str(start + size) if start + size < len(items) else None
    return {"code": 0, "data": {"activities": chunk, "next": nxt}}
