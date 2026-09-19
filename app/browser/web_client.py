from __future__ import annotations

import threading
from typing import Any, Callable, Optional

from app.api.exceptions import AnalysisCancelledError, GMGNError
from app.api.metrics import RateMetrics
from app.api.rate_limiter import WeightedRateLimiter
from app.browser.cookie_pool import CookiePool
from app.browser.payloads import as_openapi_page, extract_activity_items, extract_token_mints
from app.browser.scraper import GmgnScraper
from app.browser.session import BrowserSession
from app.utils.logger import get_logger

logger = get_logger("gmgn.web")
OnRaw = Callable[[str, dict[str, Any]], None]
OnEvent = Callable[[str], None]


class _DummySafety:
    global_cooldown_until = 0.0

    def snapshot(self) -> dict[str, Any]:
        return {"mode": "cookie"}


class _DummyScheduler:
    def __init__(self, pool: Any) -> None:
        self.pool = pool
        self.safety = _DummySafety()

    def get_next_available_time(self) -> float:
        import time

        return time.time()

    def wait_for_available_credential(self, cancel_event: Optional[threading.Event] = None) -> None:
        if cancel_event is not None and cancel_event.is_set():
            raise AnalysisCancelledError()


class GmgnWebClient:
    """Duck-typed GMGNClient backed by gmgn.ai cookies + headed/headless browser."""

    cookie_mode = True

    def __init__(
        self,
        cookie_pool: CookiePool,
        *,
        headed: bool = True,
        qr_timeout: float = 180.0,
        limiter: Optional[WeightedRateLimiter] = None,
        cancel_event: Optional[threading.Event] = None,
        on_raw: Optional[OnRaw] = None,
        on_event: Optional[OnEvent] = None,
        start_ts: int = 0,
        end_ts: int = 0,
    ) -> None:
        self.api_key = "cookie"
        self.cookie_pool = cookie_pool
        self.headed = headed
        self.qr_timeout = qr_timeout
        self.limiter = limiter or WeightedRateLimiter()
        self.cancel_event = cancel_event
        self.on_raw = on_raw
        self.on_event = on_event
        self.start_ts = int(start_ts or 0)
        self.end_ts = int(end_ts or 0)
        self.metrics = RateMetrics()
        self.scheduler = _DummyScheduler(cookie_pool)
        self.request_count = 0
        self.retry_429_count = 0
        self.error_count = 0
        self._session: Optional[BrowserSession] = None
        self._scraper: Optional[GmgnScraper] = None
        self._lock = threading.RLock()
        self._wallet_acts: dict[str, list[dict[str, Any]]] = {}
        self._token_acts: dict[tuple[str, str], list[dict[str, Any]]] = {}
        self._token_info: dict[str, dict[str, Any]] = {}
        self._holdings: dict[str, dict[str, Any]] = {}
        self._stats: dict[str, dict[str, Any]] = {}
        self._profits: dict[str, dict[str, Any]] = {}
        self._opened: set[str] = set()
        self._token_opened: set[tuple[str, str]] = set()

    def warmup(self) -> None:
        """Start the browser and reuse/login cookies before any Helius/API work."""
        self._ensure_session()

    def close(self) -> None:
        session = self._session
        self._session = None
        self._scraper = None
        if session is not None:
            session.close()

    def _emit(self, message: str) -> None:
        logger.info(message)
        if self.on_event:
            self.on_event(message)

    def _ensure_session(self) -> GmgnScraper:
        with self._lock:
            if self._scraper is not None:
                return self._scraper
            session = BrowserSession(
                self.cookie_pool,
                headed=self.headed,
                qr_timeout=self.qr_timeout,
                cancel_event=self.cancel_event,
                on_event=self.on_event,
            )
            session.start()
            scraper = GmgnScraper(
                session,
                cancel_event=self.cancel_event,
                on_event=self.on_event,
                on_raw=self.on_raw,
            )
            scraper.attach()
            self._session = session
            self._scraper = scraper
            return scraper

    def prepare_wallet(self, chain: str, wallet: str) -> None:
        _ = chain
        key = wallet
        with self._lock:
            if key in self._opened:
                return
        scraper = self._ensure_session()
        scraper.open_wallet(wallet)
        activities = scraper.scroll_until_coverage(self.start_ts)
        tokens = scraper.unique_tokens(activities)
        self._emit(f"去重后代币 {len(tokens)} 个，开始逐个打开详情")
        scraper._scroll_activity_top()
        with self._lock:
            self._wallet_acts[key] = list(activities)
            for payload in scraper.capture.by_kind.get("holdings") or []:
                self._holdings[key] = payload if isinstance(payload, dict) else {"data": payload}
            for payload in scraper.capture.by_kind.get("stats") or []:
                self._stats[key] = payload if isinstance(payload, dict) else {"data": payload}
            for payload in scraper.capture.by_kind.get("profits") or []:
                self._profits[key] = payload if isinstance(payload, dict) else {"data": payload}
            extra = [mint for mint in extract_token_mints(self._holdings.get(key) or {}) if mint != key]
            from app.providers.token_universe import MAX_UNIVERSE_TOKENS

            merged = list(dict.fromkeys([*tokens, *extra]))[:MAX_UNIVERSE_TOKENS]
            self._opened.add(key)
        for mint in merged:
            self._open_token(wallet, mint, scraper)

    def _open_token(self, wallet: str, mint: str, scraper: GmgnScraper) -> None:
        pair = (wallet, mint)
        if pair in self._token_opened:
            return
        before = len(scraper.capture.activities())
        detail = scraper.open_token_detail(mint)
        after = scraper.capture.activities()
        new_items = after[before:] if len(after) >= before else after
        token_items = [
            item
            for item in new_items
            if mint in str(item)
        ] or [item for item in after if mint in str(item.get("token_address") or item.get("base_address") or "")]
        payload = detail.get("payload") if isinstance(detail, dict) else {}
        if isinstance(payload, dict) and payload:
            self._token_info[mint] = payload
        elif isinstance(detail, dict):
            self._token_info[mint] = {
                "code": 0,
                "data": {
                    "address": mint,
                    "raw_drawer": detail.get("drawer_text") or "",
                },
            }
        with self._lock:
            self._token_acts[pair] = token_items or [
                item for item in self._wallet_acts.get(wallet, []) if mint in str(item)
            ]
            self._token_opened.add(pair)

    def get_wallet_activity(
        self,
        chain: str,
        wallet_address: str,
        *,
        token_address: Optional[str] = None,
        cursor: Optional[str] = None,
        limit: int = 50,
        types: Optional[list[str]] = None,
    ) -> dict[str, Any]:
        _ = types
        self.request_count += 1
        self.prepare_wallet(chain, wallet_address)
        if token_address:
            scraper = self._ensure_session()
            self._open_token(wallet_address, token_address, scraper)
            items = list(self._token_acts.get((wallet_address, token_address)) or [])
            if not items:
                items = [
                    item
                    for item in self._wallet_acts.get(wallet_address, [])
                    if token_address in str(item)
                ]
        else:
            items = list(self._wallet_acts.get(wallet_address, []))
        if self.end_ts:
            filtered = []
            from app.browser.payloads import activity_timestamp

            for item in items:
                ts = activity_timestamp(item)
                if ts and ts > self.end_ts:
                    continue
                filtered.append(item)
            items = filtered
        return as_openapi_page(items, cursor, limit)

    def get_wallet_stats(self, chain: str, wallet_addresses: list[str] | str, period: str = "7d") -> dict[str, Any]:
        _ = chain, period
        wallets = wallet_addresses if isinstance(wallet_addresses, list) else [wallet_addresses]
        wallet = wallets[0] if wallets else ""
        if wallet:
            self.prepare_wallet("sol", wallet)
        payload = self._stats.get(wallet) or {"code": 0, "data": {}}
        self.request_count += 1
        return payload if isinstance(payload, dict) else {"code": 0, "data": payload}

    def get_wallet_profits(self, chain: str, wallet_addresses: list[str] | str, period: str = "7d") -> dict[str, Any]:
        _ = chain, period
        wallets = wallet_addresses if isinstance(wallet_addresses, list) else [wallet_addresses]
        wallet = wallets[0] if wallets else ""
        if wallet:
            self.prepare_wallet("sol", wallet)
        payload = self._profits.get(wallet) or {"code": 0, "data": {}}
        self.request_count += 1
        return payload if isinstance(payload, dict) else {"code": 0, "data": payload}

    def get_wallet_holdings(
        self,
        chain: str,
        wallet_address: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        _ = kwargs
        self.prepare_wallet(chain, wallet_address)
        payload = self._holdings.get(wallet_address) or {"code": 0, "data": {"holdings": [], "next": None}}
        self.request_count += 1
        return payload if isinstance(payload, dict) else {"code": 0, "data": payload}

    def get_token_info(self, chain: str, address: str) -> dict[str, Any]:
        _ = chain
        self.request_count += 1
        payload = self._token_info.get(address)
        if payload:
            return payload if isinstance(payload, dict) else {"code": 0, "data": payload}
        return {"code": 0, "data": {"address": address}}

    def get_token_pool_info(self, chain: str, address: str) -> dict[str, Any]:
        info = self.get_token_info(chain, address)
        data = info.get("data") if isinstance(info, dict) else None
        if isinstance(data, dict) and data.get("pool"):
            return {"code": 0, "data": data.get("pool")}
        return {"code": 0, "data": {}}

    def test_connection(self) -> dict[str, Any]:
        scraper = self._ensure_session()
        from app.browser.scraper import is_logged_in

        ok = is_logged_in(scraper.page)
        if not ok:
            raise GMGNError("Cookie 未登录")
        return {"ok": True, "cookie": getattr(self._session, "account", None) and self._session.account.id}
