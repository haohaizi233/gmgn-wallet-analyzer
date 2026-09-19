from __future__ import annotations

import json
import random
import threading
import time
import uuid
from typing import Any, Callable, Optional

import requests
from requests.adapters import HTTPAdapter

from app.api.credential_pool import ApiCredential, CredentialPool, mask_key
from app.api.exceptions import (
    AnalysisCancelledError,
    GMGNAuthError,
    GMGNBadResponseError,
    GMGNError,
    GMGNNetworkError,
    GMGNRateLimitError,
    RateLimitSeverity,
)
from app.api.metrics import RateMetrics
from app.api.models import ApiResponse
from app.api.rate_limiter import ENDPOINT_WEIGHTS, RateLimitState, WeightedRateLimiter
from app.api.route_policy import route_weight
from app.api.scheduler import CredentialScheduler, GlobalSafetyController
from app.api.singleflight import SingleFlight
from app.utils.logger import get_logger
from app.utils.time_utils import format_clock
from app.version import __version__

logger = get_logger("gmgn.api")

OnState = Callable[[RateLimitState], None]
OnRaw = Callable[[str, dict[str, Any]], None]
OnWait = Callable[[dict[str, Any]], None]


def _is_banned(body: dict[str, Any], text: str) -> bool:
    blob = " ".join(
        [
            str(body.get("error") or ""),
            str(body.get("message") or ""),
            str(body.get("upgrade_message") or ""),
            text or "",
        ]
    )
    lower = blob.lower()
    if "RATE_LIMIT_BANNED" in blob:
        return True
    if "冷却期间请勿继续发请求" in blob:
        return True
    if "ban" in lower and "rate" in lower:
        return True
    if "cooldown" in lower and ("ban" in lower or "不要" in blob or "请勿" in blob):
        return True
    return False


class GMGNClient:
    def __init__(
        self,
        api_key: str = "",
        base_url: str = "https://openapi.gmgn.ai",
        limiter: Optional[WeightedRateLimiter] = None,
        session: Optional[requests.Session] = None,
        max_429_retries: int = 8,
        on_state: Optional[OnState] = None,
        on_raw: Optional[OnRaw] = None,
        cancel_event: Optional[threading.Event] = None,
        credential_pool: Optional[CredentialPool] = None,
        metrics: Optional[RateMetrics] = None,
        singleflight: Optional[SingleFlight] = None,
        scheduler: Optional[CredentialScheduler] = None,
        on_wait: Optional[OnWait] = None,
        safety_margin: float | None = None,
        shared_limit_detection: bool = True,
        max_api_wait: float = 900.0,
    ) -> None:
        keys = [api_key] if (api_key or "").strip() else []
        self.credentials = credential_pool or CredentialPool(keys)
        if limiter is not None and self.credentials.all():
            for cred in self.credentials.all():
                cred.limiter.server_rate = limiter.server_rate
                cred.limiter.capacity = max(cred.limiter.capacity, limiter.capacity)
                if cred.state.value not in ("RATE_LIMITED", "COOLDOWN"):
                    cred.limiter.rate = limiter.server_rate * cred.limiter._adaptive_target
                    if limiter.server_rate >= 20:
                        cred.limiter.tokens = limiter.capacity
                        cred.limiter.safety_margin = getattr(limiter, "safety_margin", cred.limiter.safety_margin)
        self.api_key = (self.credentials.all()[0].api_key if self.credentials.all() else (api_key or "").strip())
        self.base_url = (base_url or "https://openapi.gmgn.ai").rstrip("/")
        self.limiter = limiter or (self.credentials.all()[0].limiter if self.credentials.all() else WeightedRateLimiter())
        self._shared_session = session
        self._local = threading.local()
        self.max_429_retries = max(0, max_429_retries)
        self.on_state = on_state
        self.on_raw = on_raw
        self.on_wait = on_wait
        self.cancel_event = cancel_event
        self.metrics = metrics or RateMetrics()
        self.singleflight = singleflight or SingleFlight()
        margin = self.credentials.safety_margin if safety_margin is None else float(safety_margin)
        safety = GlobalSafetyController(
            self.credentials,
            enabled=shared_limit_detection,
            safety_margin=margin,
            clock=getattr(self.credentials, "_clock", None),
        )
        self.scheduler = scheduler or CredentialScheduler(
            self.credentials,
            safety=safety,
            clock=getattr(self.credentials, "_clock", None),
            max_wait=max_api_wait,
            on_waiting=self._on_waiting,
        )
        self._stats_lock = threading.Lock()
        self.request_count = 0
        self.retry_429_count = 0
        self.error_count = 0
        if on_state and self.credentials.all():
            for cred in self.credentials.all():
                cred.limiter._on_state = on_state

    def _on_waiting(self, payload: dict[str, Any]) -> None:
        if self.on_wait:
            try:
                self.on_wait(payload)
            except Exception:
                pass
        if self.on_state:
            try:
                state = self.limiter.snapshot()
                self.on_state(state)
            except Exception:
                pass

    def _http_session(self) -> requests.Session:
        if self._shared_session is not None:
            return self._shared_session
        session = getattr(self._local, "session", None)
        if session is None:
            session = requests.Session()
            adapter = HTTPAdapter(pool_connections=8, pool_maxsize=8, max_retries=0)
            session.mount("https://", adapter)
            session.mount("http://", adapter)
            self._local.session = session
        return session

    def set_cancel_event(self, event: Optional[threading.Event]) -> None:
        self.cancel_event = event

    def test_connection(self) -> dict[str, Any]:
        return self.get_user_info()

    def get_user_info(self) -> dict[str, Any]:
        return self._request("GET", "/v1/user/info", "user_info", {})

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
        query: dict[str, Any] = {
            "chain": chain,
            "wallet_address": wallet_address,
            "limit": min(50, max(1, limit)),
        }
        if token_address:
            query["token_address"] = token_address
        if cursor:
            query["cursor"] = cursor
        if types:
            query["type"] = types
        return self._request("GET", "/v1/user/wallet_activity", "wallet_activity", query)

    def get_wallet_stats(self, chain: str, wallet_addresses: list[str] | str, period: str = "7d") -> dict[str, Any]:
        wallets = wallet_addresses if isinstance(wallet_addresses, list) else [wallet_addresses]
        return self._request(
            "GET",
            "/v1/user/wallet_stats",
            "wallet_stats",
            {"chain": chain, "wallet_address": wallets, "period": period},
        )

    def get_wallet_profits(self, chain: str, wallet_addresses: list[str] | str, period: str = "7d") -> dict[str, Any]:
        wallets = wallet_addresses if isinstance(wallet_addresses, list) else [wallet_addresses]
        return self._request(
            "POST",
            "/v1/user/wallet_profits",
            "wallet_profits",
            {},
            json_body={"chain": chain, "period": period, "wallet_addresses": wallets},
        )

    def get_wallet_holdings(
        self,
        chain: str,
        wallet_address: str,
        *,
        cursor: Optional[str] = None,
        limit: int = 50,
        hide_airdrop: bool = False,
        hide_closed: bool = False,
        hide_abnormal: bool = False,
        order_by: str = "last_active_timestamp",
        direction: str = "desc",
    ) -> dict[str, Any]:
        query: dict[str, Any] = {
            "chain": chain,
            "wallet_address": wallet_address,
            "limit": min(50, max(1, limit)),
            "order_by": order_by,
            "direction": direction,
            "hide_airdrop": str(bool(hide_airdrop)).lower(),
            "hide_closed": str(bool(hide_closed)).lower(),
            "hide_abnormal": str(bool(hide_abnormal)).lower(),
        }
        if cursor:
            query["cursor"] = cursor
        return self._request("GET", "/v1/user/wallet_holdings", "wallet_holdings", query)

    def get_token_info(self, chain: str, address: str) -> dict[str, Any]:
        key = f"token_info|{chain}|{address}"
        return self.singleflight.do(key, lambda: self._request("GET", "/v1/token/info", "token_info", {"chain": chain, "address": address}))

    def get_token_pool_info(self, chain: str, address: str) -> dict[str, Any]:
        key = f"token_pool_info|{chain}|{address}"
        return self.singleflight.do(
            key, lambda: self._request("GET", "/v1/token/pool_info", "token_pool_info", {"chain": chain, "address": address})
        )

    def request_raw(self, method: str, path: str, weight_key: str, query: dict[str, Any], json_body: Any = None) -> ApiResponse:
        data = self._request(method, path, weight_key, query, json_body=json_body)
        return ApiResponse(path=path, method=method, status_code=200, data=data, raw={"data": data})

    def _request(
        self,
        method: str,
        path: str,
        weight_key: str,
        query: dict[str, Any],
        json_body: Any = None,
    ) -> dict[str, Any]:
        if not self.credentials.has_usable() and not self.api_key:
            raise GMGNAuthError("未配置 GMGN_API_KEY。请在系统设置中填写。")
        retries_5xx = 0
        retries_net = 0
        switches_429 = 0
        last_error: Optional[Exception] = None
        while True:
            self._check_cancel()
            lease = self.scheduler.acquire_credential(weight_key, self.cancel_event)
            cred = lease.credential
            timestamp = int(time.time())
            client_id = str(uuid.uuid4())
            auth_query = {**query, "timestamp": timestamp, "client_id": client_id}
            params = self._flatten_params(auth_query)
            url = f"{self.base_url}{path}"
            headers = {
                "X-APIKEY": cred.api_key,
                "Content-Type": "application/json",
                "User-Agent": f"gmgn-wallet-analyzer/{__version__}",
            }
            started = time.perf_counter()
            try:
                with self._stats_lock:
                    self.request_count += 1
                response = self._http_session().request(
                    method,
                    url,
                    params=params,
                    json=json_body if json_body is not None else None,
                    headers=headers,
                    timeout=(8, 25),
                )
            except requests.Timeout as exc:
                http_latency = time.perf_counter() - started
                self.scheduler.report_response(lease, status=0, latency=http_latency)
                self.scheduler.release_credential(lease)
                retries_net += 1
                self.metrics.record(
                    lease.weight,
                    latency=http_latency,
                    wait_seconds=lease.local_wait,
                    status=0,
                    local_wait=lease.local_wait,
                    credential_wait=lease.credential_wait,
                    global_wait=lease.global_wait,
                    cooldown_wait=lease.cooldown_wait,
                )
                if retries_net > 3:
                    with self._stats_lock:
                        self.error_count += 1
                        self.metrics.timeouts += 1
                    raise GMGNNetworkError(f"{method} {path} 超时: {exc}") from exc
                self._sleep(self._backoff(retries_net))
                continue
            except requests.RequestException as exc:
                http_latency = time.perf_counter() - started
                self.scheduler.report_response(lease, status=0, latency=http_latency)
                self.scheduler.release_credential(lease)
                retries_net += 1
                if retries_net > 3:
                    with self._stats_lock:
                        self.error_count += 1
                    raise GMGNNetworkError(f"{method} {path} 网络错误: {exc}") from exc
                self._sleep(self._backoff(retries_net))
                continue

            http_latency = time.perf_counter() - started
            body_text = response.text
            body_json: dict[str, Any] = {}
            try:
                parsed = json.loads(body_text) if body_text else {}
                if isinstance(parsed, dict):
                    body_json = parsed
            except json.JSONDecodeError:
                parsed = None

            if self.on_raw:
                try:
                    self.on_raw(
                        path,
                        {
                            "method": method,
                            "status": response.status_code,
                            "query": {k: v for k, v in auth_query.items() if k not in ("timestamp", "client_id")},
                            "body": json_body,
                            "response": parsed if parsed is not None else body_text[:4000],
                            "key": cred.masked,
                        },
                    )
                except Exception:
                    logger.exception("保存原始 API JSON 失败")

            rate_limited = response.status_code == 429 or (
                isinstance(body_json, dict)
                and (
                    str(body_json.get("error") or "").startswith("RATE_LIMIT")
                    or str(body_json.get("error") or "") in ("RATE_LIMIT_EXCEEDED", "RATE_LIMIT_BANNED")
                )
            )
            self.metrics.record(
                lease.weight,
                latency=http_latency,
                wait_seconds=lease.local_wait,
                status=response.status_code if not rate_limited else 429,
                local_wait=lease.local_wait,
                credential_wait=lease.credential_wait,
                global_wait=lease.global_wait,
                cooldown_wait=lease.cooldown_wait,
            )

            reset_at = None
            if rate_limited:
                reset_at = cred.limiter.update_from_headers(dict(response.headers), body_json)
                # update_from_headers 已写入 limiter cooldown；report_response 再写 credential 状态
                # 但 update_from_headers 会再次 set_server_cooldown。report 也会。避免二次降利用率：
                # 下面 report 用已解析 reset。limiter 已被 update_from_headers 更新。
            banned = rate_limited and _is_banned(body_json, body_text)
            resume = cred.limiter.effective_resume_at() if rate_limited else 0.0
            logger.info(
                "route=%s weight=%s key=%s local_wait=%.2fs credential_wait=%.2fs cooldown_wait=%.2fs http=%.2fs status=%s%s",
                weight_key,
                lease.weight,
                cred.masked,
                lease.local_wait,
                lease.credential_wait,
                lease.cooldown_wait,
                http_latency,
                429 if rate_limited else response.status_code,
                (
                    f" reset={format_clock(int(reset_at or 0))} effective_resume={format_clock(int(resume))}"
                    if rate_limited
                    else ""
                ),
            )

            if rate_limited:
                # update_from_headers 已经 set_server_cooldown；mark_429 会再调一次导致利用率连降。
                # 先恢复 limiter adaptive 只记事件，credential 状态仍必须 RATE_LIMITED。
                self.metrics.note_rate_limit_event()
                with self._stats_lock:
                    self.retry_429_count += 1
                # 回滚 limiter 第二次惩罚：mark_429 内部会再 set_server_cooldown。
                # 采用：不走 limiter.set，只写 credential。但 update_from_headers 已 set 一次（正确）。
                # 因此这里手动写 credential，不再调用 mark_429 的 limiter 部分。
                from app.api.credential_pool import CredentialState

                cred.state = CredentialState.RATE_LIMITED
                cred.count_429 += 1
                cred.consecutive_429 += 1
                cred.success_since_429 = 0
                cred.last_error = "RATE_LIMIT_BANNED" if banned else "RATE_LIMIT_EXCEEDED"
                unix = int(reset_at or (time.time() + 2))
                cred.cooldown_until = float(cred.limiter.effective_resume_at() or (unix + self.credentials.safety_margin))
                if banned:
                    cred.limiter._adaptive_target = max(0.30, min(cred.limiter._adaptive_target - 0.05, 0.50))
                    cred.limiter.rate = cred.limiter.server_rate * cred.limiter._adaptive_target
                self.scheduler.safety.note_429(cred, reset_at)
                self.scheduler.release_credential(lease)
                switches_429 += 1
                if switches_429 > max(self.max_429_retries, 1) and not self.credentials.has_healthy_now():
                    # 仍有 Key 在 cooldown：继续等，不算失败
                    if not self.credentials.has_usable():
                        raise GMGNRateLimitError(
                            self._rate_limit_message(body_json, reset_at),
                            status=429,
                            api_error=str(body_json.get("error") or "RATE_LIMIT_EXCEEDED"),
                            reset_at=reset_at,
                            severity=RateLimitSeverity.BANNED if banned else RateLimitSeverity.THROTTLED,
                        )
                continue

            if response.status_code in (401, 403):
                self.scheduler.report_response(lease, status=response.status_code, latency=http_latency, body=body_json)
                self.scheduler.release_credential(lease)
                if self.credentials.has_usable():
                    logger.warning("%s %s 失败，切换下一把健康 Key", cred.masked, response.status_code)
                    continue
                with self._stats_lock:
                    self.error_count += 1
                raise GMGNAuthError(self._auth_message(response.status_code, body_json), status=response.status_code)

            if response.status_code >= 500:
                self.scheduler.report_response(lease, status=response.status_code, latency=http_latency, body=body_json)
                self.scheduler.release_credential(lease)
                retries_5xx += 1
                if retries_5xx > 3:
                    with self._stats_lock:
                        self.error_count += 1
                    raise GMGNNetworkError(f"{method} {path} 服务器错误 HTTP {response.status_code}", status=response.status_code)
                backoff = self._backoff(retries_5xx)
                logger.warning("HTTP %s，%.1fs 后重试 %s", response.status_code, backoff, path)
                self._sleep(backoff)
                continue

            if parsed is None:
                self.scheduler.release_credential(lease)
                with self._stats_lock:
                    self.error_count += 1
                raise GMGNBadResponseError(f"{method} {path} 非 JSON 响应 HTTP {response.status_code}", status=response.status_code)

            code = body_json.get("code", 0)
            if code not in (0, "0", None):
                error_name = str(body_json.get("error") or "")
                if error_name in ("RATE_LIMIT_EXCEEDED", "RATE_LIMIT_BANNED"):
                    self.scheduler.release_credential(lease)
                    continue
                self.scheduler.report_response(lease, status=response.status_code or 400, latency=http_latency, body=body_json)
                self.scheduler.release_credential(lease)
                with self._stats_lock:
                    self.error_count += 1
                raise GMGNBadResponseError(
                    f"{method} {path} 业务错误 code={code} error={error_name} message={body_json.get('message')}",
                    status=response.status_code,
                    api_error=error_name,
                )

            self.scheduler.report_response(lease, status=response.status_code, latency=http_latency, body=body_json)
            self.scheduler.release_credential(lease)
            data = body_json.get("data", body_json)
            return data if data is not None else {}

        raise last_error or GMGNError(f"{method} {path} 未知失败")

    def _flatten_params(self, query: dict[str, Any]) -> list[tuple[str, str]]:
        items: list[tuple[str, str]] = []
        for key, value in query.items():
            if value is None:
                continue
            if isinstance(value, (list, tuple)):
                for item in value:
                    items.append((key, str(item)))
            else:
                items.append((key, str(value)))
        return items

    def _auth_message(self, status: int, body: dict[str, Any]) -> str:
        message = str(body.get("message") or body.get("error") or "")
        if status == 401:
            return f"API Key 无效 / timestamp 问题。{message}".strip()
        return f"认证 / IP / 权限问题（HTTP 403）。{message} 若本机走 IPv6，请改为 IPv4。".strip()

    def _rate_limit_message(self, body: dict[str, Any], reset_at: Optional[int]) -> str:
        from app.utils.time_utils import format_datetime

        reset_text = format_datetime(reset_at, empty="未知时间")
        error = body.get("error") or "RATE_LIMIT_EXCEEDED"
        extra = body.get("upgrade_message") or "已达到当前套餐的限频上限"
        return f"{error}: {extra}。冷却至 {reset_text}。冷却期间请勿继续发请求，否则 ban 会延长。"

    def _backoff(self, attempt: int) -> float:
        base = {1: 0.5, 2: 1.0, 3: 2.0}.get(attempt, 2.0)
        return base + random.random() * 0.2

    def _sleep(self, seconds: float) -> None:
        remaining = max(0.0, seconds)
        while remaining > 0:
            self._check_cancel()
            step = min(0.2, remaining)
            if self.cancel_event is not None:
                if self.cancel_event.wait(step):
                    raise AnalysisCancelledError()
            else:
                time.sleep(step)
            remaining -= step

    def _check_cancel(self) -> None:
        if self.cancel_event is not None and self.cancel_event.is_set():
            raise AnalysisCancelledError()
