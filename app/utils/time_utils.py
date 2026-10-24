from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from app.domain.enums import ReportPeriod

LOCAL_TZ = datetime.now().astimezone().tzinfo or timezone(timedelta(hours=8))


def now_ts() -> int:
    return int(datetime.now().timestamp())


def parse_timestamp(value) -> Optional[int]:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return to_unix(value)
    if isinstance(value, (int, float)):
        number = float(value)
        if number > 10_000_000_000:
            number /= 1000.0
        if number <= 0:
            return None
        return int(number)
    text = str(value).strip()
    if not text:
        return None
    try:
        if text.replace(".", "", 1).isdigit():
            return parse_timestamp(float(text))
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return to_unix(dt)
    except (TypeError, ValueError):
        return None


def to_unix(dt: datetime) -> int:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=LOCAL_TZ)
    return int(dt.timestamp())


def from_unix(ts: Optional[int | float]) -> Optional[datetime]:
    if ts is None:
        return None
    try:
        value = float(ts)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    if value > 10_000_000_000:
        value = value / 1000.0
    return datetime.fromtimestamp(value, tz=LOCAL_TZ)


def format_datetime(ts: Optional[int | float], empty: str = "GMGN 未提供") -> str:
    dt = from_unix(ts)
    if dt is None:
        return empty
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def format_clock(ts: Optional[int | float]) -> str:
    dt = from_unix(ts)
    if dt is None:
        return "--:--:--"
    return dt.strftime("%H:%M:%S")


def period_window(period: ReportPeriod, start: Optional[datetime] = None, end: Optional[datetime] = None) -> tuple[int, int]:
    now = datetime.now(tz=LOCAL_TZ)
    if period == ReportPeriod.CUSTOM:
        if start is None:
            raise ValueError("自定义时间必须提供开始日期")
        return to_unix(start), to_unix(end or now)
    if period == ReportPeriod.ALL:
        return 0, to_unix(end or now)
    days = {
        ReportPeriod.D1: 1,
        ReportPeriod.D7: 7,
        ReportPeriod.D30: 30,
        ReportPeriod.D90: 90,
    }[period]
    end_dt = end or now
    # 「最近 N 天」is a rolling window from now. The GUI date boxes only store
    # calendar days and pass today 23:59:59 as `end`; using that unclamped would
    # start a 1-day window at yesterday 23:59:59 and drop 21h–22h-ago trades.
    if end_dt > now:
        end_dt = now
    start_dt = end_dt - timedelta(days=days)
    return to_unix(start_dt), to_unix(end_dt)


def gmgn_profit_period(period: ReportPeriod) -> Optional[str]:
    mapping = {
        ReportPeriod.D1: "7d",
        ReportPeriod.D7: "7d",
        ReportPeriod.D30: "30d",
        ReportPeriod.ALL: "all",
    }
    return mapping.get(period)


def gmgn_stats_period(period: ReportPeriod) -> str:
    if period in (ReportPeriod.D1, ReportPeriod.D7):
        return "7d"
    if period in (ReportPeriod.D30, ReportPeriod.D90):
        return "30d"
    if period == ReportPeriod.ALL:
        return "30d"
    return "7d"


def gmgn_period_for_range(start_ts: int, end_ts: int) -> str:
    """Pick the closest GMGN-supported period for a custom date range."""
    if start_ts <= 0:
        return "all"
    span_days = max(1, (end_ts - start_ts) // 86400)
    if span_days <= 7:
        return "7d"
    if span_days <= 30:
        return "30d"
    return "all"


def format_elapsed(seconds: float) -> str:
    total = int(max(0, seconds))
    if total < 60:
        return f"{total}秒"
    minutes, sec = divmod(total, 60)
    if minutes < 60:
        return f"{minutes}分{sec}秒" if sec else f"{minutes}分"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}小时{minutes}分"
