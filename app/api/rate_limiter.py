from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

from app.api.exceptions import AnalysisCancelledError
from app.api.metrics import RateMetrics
from app.domain.enums import ApiHealth


class Clock:
    def monotonic(self) -> float:
        return time.monotonic()

    def time(self) -> float:
        return time.time()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


@dataclass
class RateLimitState:
    health: ApiHealth = ApiHealth.OK
    tokens: float = 5.0
    capacity: float = 5.0
    cooldown_until: float = 0.0
    last_reset_at: Optional[int] = None
    server_rate: float = 5.0
    target_utilization: float = 0.8
    adaptive_target: float = 0.8
    effective_rate: float = 4.0
    utilization_1m: float = 0.0

    @property
    def display(self) -> str:
        if self.health == ApiHealth.COOLDOWN and self.last_reset_at:
            from app.utils.time_utils import format_clock

            return f"{ApiHealth.COOLDOWN.value}至 {format_clock(self.last_reset_at)}"
        util = f"{self.utilization_1m * 100:.0f}%"
        target = f"{self.adaptive_target * 100:.0f}%"
        if self.health == ApiHealth.WAITING:
            return f"{ApiHealth.WAITING.value}（{self.tokens:.1f}/{self.capacity:.0f} 使用率{util} 目标{target}）"
        return f"{ApiHealth.OK.value} {self.tokens:.1f}/{self.capacity:.0f} 使用率{util} 目标{target}"


class WeightedRateLimiter:
    """线程安全加权令牌桶。所有 Key 必须共享同一个实例。

    server_rate：服务端允许的加权单位/秒（Free=5）
    target_utilization：长期目标（默认由配置 0.80 传入；单测默认 1.0 以保持旧行为）
    effective_rate = server_rate * adaptive_target
    capacity 不超过服务端 burst。
    """

    def __init__(
        self,
        rate: float = 5.0,
        capacity: float = 5.0,
        clock: Optional[Clock] = None,
        on_state: Optional[Callable[[RateLimitState], None]] = None,
        target_utilization: float = 1.0,
        metrics: Optional[RateMetrics] = None,
        initial_utilization: float | None = None,
        safety_margin: float = 0.0,
    ) -> None:
        self.server_rate = max(0.1, float(rate))
        self.capacity = max(1.0, float(capacity))
        self.target_utilization = min(1.0, max(0.1, float(target_utilization)))
        start = self.target_utilization if initial_utilization is None else min(self.target_utilization, max(0.30, float(initial_utilization)))
        self._adaptive_target = start
        self.rate = self.server_rate * self._adaptive_target
        self.tokens = min(self.capacity, self.rate)
        self.safety_margin = max(0.0, float(safety_margin))
        self._last_mono = 0.0
        self._last_grant_mono = 0.0
        self._clock = clock or Clock()
        self._lock = threading.RLock()
        self._cond = threading.Condition(self._lock)
        self._cooldown_until_unix = 0.0
        self._last_reset_at: Optional[int] = None
        self._on_state = on_state
        self._last_mono = self._clock.monotonic()
        self.metrics = metrics or RateMetrics()
        self._consecutive_429 = 0
        self._recovery_started = 0.0
        self._granted_weight = 0.0
        self._requested_weight = 0.0
        self._wait_seconds = 0.0
        self.rate_limit_events = 0

    def reset(self) -> None:
        with self._lock:
            self._adaptive_target = self.target_utilization
            self.rate = self.server_rate * self._adaptive_target
            self.tokens = min(self.capacity, self.rate)
            self._last_mono = self._clock.monotonic()
            self._cooldown_until_unix = 0.0
            self._last_reset_at = None
            self._consecutive_429 = 0
            self._recovery_started = 0.0
        self._emit(ApiHealth.OK)

    def snapshot(self) -> RateLimitState:
        with self._lock:
            self._refill_locked()
            self._maybe_recover_locked()
            health = ApiHealth.OK
            now = self._clock.time()
            if now < self._cooldown_until_unix:
                health = ApiHealth.COOLDOWN
            elif self.tokens < self.capacity:
                health = ApiHealth.WAITING if self.tokens < 1 else ApiHealth.OK
            util = self.metrics.utilization(self.server_rate, 60)
            return RateLimitState(
                health=health,
                tokens=self.tokens,
                capacity=self.capacity,
                cooldown_until=self._cooldown_until_unix,
                last_reset_at=self._last_reset_at,
                server_rate=self.server_rate,
                target_utilization=self.target_utilization,
                adaptive_target=self._adaptive_target,
                effective_rate=self.rate,
                utilization_1m=util,
            )

    def update_from_headers(self, headers: dict, body: Optional[dict] = None) -> Optional[int]:
        reset_at = None
        if isinstance(body, dict):
            data = body.get("data")
            reset_at = body.get("reset_at")
            if reset_at in (None, "") and isinstance(data, dict):
                reset_at = data.get("reset_at")
        header_reset = None
        if headers:
            header_reset = headers.get("X-RateLimit-Reset") or headers.get("x-ratelimit-reset")
        picked = reset_at if reset_at not in (None, "") else header_reset
        unix = None
        if picked not in (None, ""):
            try:
                unix = int(float(picked))
            except (TypeError, ValueError):
                unix = None
        if unix is not None and unix > 10_000_000_000:
            unix = unix // 1000
        if unix is None:
            unix = int(self._clock.time()) + 2
        return self.set_server_cooldown(unix)

    def set_server_cooldown(self, reset_at: int, safety_margin: float | None = None) -> int:
        unix = int(reset_at)
        margin = self.safety_margin if safety_margin is None else max(0.0, float(safety_margin))
        with self._lock:
            self._cooldown_until_unix = float(unix) + margin
            self._last_reset_at = unix
            self._consecutive_429 += 1
            self.rate_limit_events += 1
            drop = 0.20 if self._consecutive_429 >= 2 else 0.15
            self._adaptive_target = max(0.30, round(self._adaptive_target - drop, 2))
            self._adaptive_target = min(self._adaptive_target, self.target_utilization)
            self.rate = self.server_rate * self._adaptive_target
            self._recovery_started = 0.0
            self._cond.notify_all()
        self._emit(ApiHealth.COOLDOWN)
        return unix

    def try_acquire(self, weight: int) -> bool:
        need = max(1, int(weight))
        with self._lock:
            now_unix = self._clock.time()
            if now_unix < self._cooldown_until_unix:
                return False
            self._refill_locked()
            if self.tokens >= need:
                self.tokens -= need
                self._granted_weight += need
                self._requested_weight += need
                return True
            return False

    def wait_time(self, weight: int) -> float:
        need = max(1, int(weight))
        with self._lock:
            now_unix = self._clock.time()
            if now_unix < self._cooldown_until_unix:
                return self._cooldown_until_unix - now_unix
            self._refill_locked()
            if self.tokens >= need:
                return 0.0
            return (need - self.tokens) / max(self.rate, 0.01)

    def acquire(self, weight: int, cancel_event: Optional[threading.Event] = None, min_spacing: float = 0.0) -> float:
        need = max(1, int(weight))
        waited = 0.0
        self._requested_weight += need
        while True:
            if cancel_event is not None and cancel_event.is_set():
                raise AnalysisCancelledError()
            wait_seconds = 0.0
            health = ApiHealth.OK
            with self._lock:
                now_unix = self._clock.time()
                self._maybe_recover_locked()
                if now_unix < self._cooldown_until_unix:
                    wait_seconds = self._cooldown_until_unix - now_unix
                    health = ApiHealth.COOLDOWN
                else:
                    self._refill_locked()
                    spacing = max(0.0, float(min_spacing or 0.0))
                    if spacing and self._last_grant_mono:
                        since = self._clock.monotonic() - self._last_grant_mono
                        if since < spacing:
                            wait_seconds = spacing - since
                            health = ApiHealth.WAITING
                        elif self.tokens >= need:
                            self.tokens -= need
                            self._granted_weight += need
                            self._last_grant_mono = self._clock.monotonic()
                            self._wait_seconds += waited
                            self._emit(ApiHealth.OK)
                            return waited
                        else:
                            wait_seconds = (need - self.tokens) / max(self.rate, 0.01)
                            health = ApiHealth.WAITING
                    elif self.tokens >= need:
                        self.tokens -= need
                        self._granted_weight += need
                        self._last_grant_mono = self._clock.monotonic()
                        self._wait_seconds += waited
                        self._emit(ApiHealth.OK)
                        return waited
                    else:
                        wait_seconds = (need - self.tokens) / max(self.rate, 0.01)
                        health = ApiHealth.WAITING
            self._emit(health)
            self._sleep(max(wait_seconds, 0.01), cancel_event)
            waited += wait_seconds

    def _maybe_recover_locked(self) -> None:
        now = self._clock.time()
        if now < self._cooldown_until_unix:
            return
        if self._consecutive_429 <= 0:
            return
        if self._recovery_started <= 0:
            self._recovery_started = now
            self._adaptive_target = 0.50
            self.rate = self.server_rate * self._adaptive_target
            return
        elapsed = now - self._recovery_started
        if elapsed >= 30:
            self._adaptive_target = self.target_utilization
            self._consecutive_429 = 0
        elif elapsed >= 20:
            self._adaptive_target = min(self.target_utilization, 0.80)
        elif elapsed >= 10:
            self._adaptive_target = min(self.target_utilization, 0.65)
        else:
            self._adaptive_target = min(self.target_utilization, 0.50)
        self.rate = self.server_rate * self._adaptive_target

    def _refill_locked(self) -> None:
        now = self._clock.monotonic()
        elapsed = max(0.0, now - self._last_mono)
        self._last_mono = now
        self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)

    def note_success(self) -> None:
        """60s 无 429 且请求足够时缓慢升向 target。"""
        with self._lock:
            if self._consecutive_429 > 0:
                return
            if self._adaptive_target >= self.target_utilization:
                return
            # 由 scheduler 按墙钟控制升档；这里仅钳制
            self._adaptive_target = min(self.target_utilization, self._adaptive_target)
            self.rate = self.server_rate * self._adaptive_target

    def raise_utilization(self, step: float = 0.05) -> None:
        with self._lock:
            if self._consecutive_429 > 0:
                return
            self._adaptive_target = min(self.target_utilization, round(self._adaptive_target + step, 2))
            self.rate = self.server_rate * self._adaptive_target

    def effective_resume_at(self) -> float:
        with self._lock:
            return self._cooldown_until_unix

    def _sleep(self, seconds: float, cancel_event: Optional[threading.Event]) -> None:
        remaining = max(0.0, seconds)
        use_event = cancel_event is not None and type(self._clock) is Clock
        while remaining > 0:
            if cancel_event is not None and cancel_event.is_set():
                raise AnalysisCancelledError()
            step = min(0.2 if use_event else 0.05, remaining)
            if use_event:
                if cancel_event.wait(step):
                    raise AnalysisCancelledError()
            else:
                self._clock.sleep(step)
            remaining -= step

    def _emit(self, health: ApiHealth) -> None:
        if not self._on_state:
            return
        state = self.snapshot()
        state.health = health
        try:
            self._on_state(state)
        except Exception:
            pass


GlobalWeightedRateLimiter = WeightedRateLimiter

ENDPOINT_WEIGHTS = {
    "wallet_activity": 3,
    "wallet_stats": 3,
    "wallet_profits": 3,
    "token_info": 1,
    "token_pool_info": 1,
    "user_info": 2,
    "wallet_holdings": 5,
}
