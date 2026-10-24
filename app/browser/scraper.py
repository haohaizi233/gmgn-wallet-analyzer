from __future__ import annotations

import json
import re
import threading
import time
from typing import Any, Callable, Optional
from urllib.parse import urlparse

from app.api.exceptions import AnalysisCancelledError
from app.browser.cookie_pool import has_login_cookies
from app.browser.exceptions import BrowserSessionError
from app.browser.payloads import (
    ADDRESS_HREF_RE,
    activity_token_labels,
    classify_url,
    extract_activity_items,
    extract_token_mints,
    oldest_activity_ts,
)
from app.browser.drawer_metrics import parse_drawer_metrics
from app.browser import selectors as sel
from app.utils.logger import get_logger

logger = get_logger("gmgn.browser")
OnEvent = Callable[..., None]
LOGIN_RETRY_GAP = 12.0
BROWSER_CLOSED = "浏览器窗口被关掉了，采集中断"
BROWSER_CLOSED_LOGIN = "浏览器窗口被关掉了，登录未完成"
ANNOUNCE_ACTIONS = (
    sel.ANNOUNCE_NEXT,
    '.pi-modal button:has-text("Got it")',
    '.pi-modal button:has-text("Done")',
    '.pi-modal button:has-text("Start")',
    '.pi-modal button:has-text("Finish")',
    '.pi-modal button:has-text("完成")',
    '.pi-modal button:has-text("我知道了")',
)
SCROLLER_BOX_JS = """() => {
    const activity = document.querySelector('[data-sentry-component="Activity"]');
    const wrapper = activity && (
        activity.querySelector('.g-table-wrapper')
        || activity.querySelector('.g-table-content')
        || activity.querySelector('table')
    );
    const rows = activity ? [...activity.querySelectorAll('tr.g-table-row')] : [];
    const visibleRow = rows.find((row) => {
        const box = row.getBoundingClientRect();
        return box.width > 40 && box.height > 8 && box.bottom > 48 && box.top < window.innerHeight - 24;
    });
    const hit = wrapper || visibleRow || activity;
    const clip = (el) => {
        if (!el) return null;
        const r = el.getBoundingClientRect();
        if (r.width < 20 || r.height < 8) return null;
        let x = r.x;
        let y = r.y;
        let w = r.width;
        let h = r.height;
        if (w > window.innerWidth * 0.65) {
            x = r.x + w * 0.52;
            w = w * 0.46;
        }
        if (h > window.innerHeight * 0.75) {
            y = r.y + Math.max(h * 0.42, 220);
            h = h * 0.5;
        }
        return {x, y, w, h};
    };
    return clip(hit) || {
        x: window.innerWidth * 0.58,
        y: window.innerHeight * 0.55,
        w: window.innerWidth * 0.36,
        h: window.innerHeight * 0.38,
    };
}"""
WHEEL_JS = """(deltaY) => {
    const activity = document.querySelector('[data-sentry-component="Activity"]');
    const wrapper = activity && (
        activity.querySelector('.g-table-wrapper')
        || activity.querySelector('.g-table-content')
        || activity.querySelector('table')
        || activity
    );
    const row = activity && activity.querySelector('tr.g-table-row');
    const start = wrapper || row || activity;
    let scroller = start;
    while (scroller && scroller !== document.body) {
        const style = window.getComputedStyle(scroller);
        const overflowY = style.overflowY;
        if ((overflowY === 'auto' || overflowY === 'scroll' || overflowY === 'overlay')
            && scroller.scrollHeight > scroller.clientHeight + 8) {
            break;
        }
        scroller = scroller.parentElement;
    }
    if (!scroller) scroller = activity && activity.parentElement;
    if (start) {
        const box = start.getBoundingClientRect();
        start.dispatchEvent(new WheelEvent('wheel', {
            deltaY,
            bubbles: true,
            cancelable: true,
            view: window,
            clientX: box.x + Math.max(box.width * 0.62, 80),
            clientY: box.y + Math.max(box.height * 0.55, 40),
        }));
    }
    if (scroller) {
        if (typeof scroller.scrollBy === 'function') {
            scroller.scrollBy(0, deltaY);
        } else {
            scroller.scrollTop = Math.max(0, (scroller.scrollTop || 0) + deltaY);
        }
    }
    return true;
}"""
JUMP_END_JS = """() => {
    const activity = document.querySelector('[data-sentry-component="Activity"]');
    let node = (activity && (
        activity.querySelector('.g-table-wrapper')
        || activity.querySelector('.g-table-content')
        || activity
    )) || activity;
    while (node && node !== document.body) {
        if (node.scrollHeight > node.clientHeight + 4) {
            node.scrollTop = node.scrollHeight;
            return true;
        }
        node = node.parentElement;
    }
    return false;
}"""
DRAWER_SCROLLER_JS = """() => {
    const drawer = document.querySelector('.pi-drawer')
        || document.querySelector('[class*="drawer-content"]')
        || document.querySelector('[data-sentry-component="DrawerToken"]')
        || document.querySelector('[data-sentry-component="DrawerTokenStats"]');
    if (!drawer) return null;
    let node = drawer;
    while (node && node !== document.body) {
        const style = window.getComputedStyle(node);
        const overflowY = style.overflowY;
        if ((overflowY === 'auto' || overflowY === 'scroll' || overflowY === 'overlay')
            && node.scrollHeight > node.clientHeight + 8) {
            const r = node.getBoundingClientRect();
            if (r.width > 80 && r.height > 80) {
                return {x: r.x, y: r.y, w: r.width, h: r.height,
                        scrollTop: node.scrollTop, scrollHeight: node.scrollHeight};
            }
        }
        node = node.parentElement;
    }
    const body = drawer.querySelector('.pi-drawer-body') || drawer;
    if (body) {
        const r = body.getBoundingClientRect();
        if (r.width > 80 && r.height > 80) {
            return {x: r.x, y: r.y, w: r.width, h: r.height,
                    scrollTop: body.scrollTop || 0, scrollHeight: body.scrollHeight || 0};
        }
    }
    return null;
}"""
DRAWER_WHEEL_JS = """(deltaY) => {
    const drawer = document.querySelector('.pi-drawer')
        || document.querySelector('[class*="drawer-content"]')
        || document.querySelector('[data-sentry-component="DrawerToken"]');
    if (!drawer) return false;
    let scroller = drawer;
    while (scroller && scroller !== document.body) {
        const style = window.getComputedStyle(scroller);
        const overflowY = style.overflowY;
        if ((overflowY === 'auto' || overflowY === 'scroll' || overflowY === 'overlay')
            && scroller.scrollHeight > scroller.clientHeight + 8) {
            break;
        }
        scroller = scroller.parentElement;
    }
    if (!scroller || scroller === document.body) {
        scroller = drawer.querySelector('.pi-drawer-body') || drawer;
    }
    if (scroller) {
        if (typeof scroller.scrollBy === 'function') {
            scroller.scrollBy(0, deltaY);
        } else {
            scroller.scrollTop = Math.max(0, (scroller.scrollTop || 0) + deltaY);
        }
        const r = scroller.getBoundingClientRect();
        scroller.dispatchEvent(new WheelEvent('wheel', {
            deltaY, bubbles: true, cancelable: true, view: window,
            clientX: r.x + r.width * 0.5, clientY: r.y + r.height * 0.5,
        }));
    }
    return true;
}"""
LIST_SIGNATURE_JS = """() => {
    const activity = document.querySelector('[data-sentry-component="Activity"]');
    if (!activity) return 'none';
    const rows = [...activity.querySelectorAll('tr.g-table-row')];
    const visible = rows.filter((row) => {
        const box = row.getBoundingClientRect();
        return box.height > 4 && box.bottom > 0 && box.top < window.innerHeight;
    });
    const label = (row) => ((row && row.innerText) || '').split('\\n')[0].trim().slice(0, 24);
    let top = 0;
    let node = activity.querySelector('.g-table-wrapper') || activity;
    while (node && node !== document.body) {
        if (node.scrollHeight > node.clientHeight + 4) {
            top = node.scrollTop || 0;
            break;
        }
        node = node.parentElement;
    }
    return [visible.length, label(visible[0]), label(visible[visible.length - 1]), Math.round(top)].join('|');
}"""
ACTIVITY_SCAN_JS = """() => {
    const activity = document.querySelector('[data-sentry-component="Activity"]');
    if (!activity) return {rows: 0, visible: 0, lastTime: '', names: []};
    const rows = [...activity.querySelectorAll('tr.g-table-row')];
    const visible = rows.filter((row) => {
        const box = row.getBoundingClientRect();
        return box.height > 4 && box.bottom > 0 && box.top < window.innerHeight;
    });
    const last = visible[visible.length - 1] || rows[rows.length - 1];
    let lastTime = '';
    const cells = last ? [...last.querySelectorAll('td')] : [];
    for (const cell of cells) {
        const t = (cell.innerText || '').replace(/\\s+/g, ' ').trim();
        if (t && t.length < 28 && /(ago|刚刚|[0-9]+\\s*[smhd]|秒前|分钟前|小时前|天前)/i.test(t)) {
            lastTime = t;
        }
    }
    const names = [];
    const seen = new Set();
    for (const row of rows) {
        const raw = ((row.innerText || '').split('\\n')[0] || '').trim();
        const name = raw.replace(/^(Buy|Sell|Add|Remove|Transfer)\\s+/i, '').split(/\\s+/)[0];
        if (name && !seen.has(name)) {
            seen.add(name);
            names.push(name);
        }
    }
    return {rows: rows.length, visible: visible.length, lastTime, names};
}"""
RESET_SCROLL_JS = """() => {
    const activity = document.querySelector('[data-sentry-component="Activity"]');
    const wrapper = activity && (
        activity.querySelector('.g-table-wrapper')
        || activity.querySelector('.g-table-content')
        || activity
    );
    let node = wrapper || activity;
    while (node && node !== document.body) {
        if (node.scrollHeight > node.clientHeight + 4) {
            node.scrollTop = 0;
        }
        node = node.parentElement;
    }
}"""
HEADER_AUTH_JS = """() => {
    const TOP = 120;
    const inTop = (el) => {
        if (!el) return false;
        const style = window.getComputedStyle(el);
        if (style.display === 'none' || style.visibility === 'hidden' || Number(style.opacity) === 0) {
            return false;
        }
        const box = el.getBoundingClientRect();
        return box.width > 4 && box.height > 4 && box.top < TOP && box.bottom > 0;
    };
    const loginRe = /^(log\\s*in|sign\\s*in|sign\\s*up|登录|登入|註冊|注册)$/i;
    let login = false;
    const nodes = document.querySelectorAll('button, a, [role="button"], #signUpBtn');
    for (const el of nodes) {
        if (!inTop(el)) continue;
        if (el.id === 'signUpBtn') {
            login = true;
            break;
        }
        const text = (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
        if (loginRe.test(text)) {
            login = true;
            break;
        }
    }
    const watchlist = [...document.querySelectorAll(
        '[data-sentry-component="WatchlistEntry"], [data-sentry-component="Watchlist"]'
    )].some(inTop);
    const right = window.innerWidth * 0.62;
    const avatar = [...document.querySelectorAll('[data-sentry-component="UserAvatar"]')].some((el) => {
        if (!inTop(el)) return false;
        return el.getBoundingClientRect().x >= right;
    });
    return {
        login,
        watchlist,
        avatar,
        ready: login || watchlist || avatar,
    };
}"""


def page_is_closed(page) -> bool:
    if page is None:
        return True
    try:
        return bool(page.is_closed())
    except Exception:
        return True


def is_target_closed(exc: BaseException) -> bool:
    name = type(exc).__name__
    text = str(exc).lower()
    return "TargetClosed" in name or "has been closed" in text or "target closed" in text


def should_reopen_login(*, logged_in: bool, login_visible: bool, login_dialog: bool, announcement: bool) -> bool:
    """Re-click header Log In only when the login popup is gone and the header still says Log In."""
    return bool(login_visible) and not logged_in and not login_dialog and not announcement


def _any_visible(locator, limit: int = 8) -> bool:
    try:
        total = locator.count()
    except Exception:
        return False
    for idx in range(min(total, limit)):
        try:
            if locator.nth(idx).is_visible():
                return True
        except Exception:
            continue
    return False


def header_auth_state(page) -> dict[str, bool]:
    """Top-bar login fields only. Cookie files are not proof of login."""
    empty = {"login": False, "watchlist": False, "avatar": False, "ready": False}
    if page is None:
        return empty
    try:
        raw = page.evaluate(HEADER_AUTH_JS)
    except Exception as exc:
        if is_target_closed(exc):
            raise BrowserSessionError(BROWSER_CLOSED) from exc
        return empty
    if not isinstance(raw, dict):
        return empty
    return {
        "login": bool(raw.get("login")),
        "watchlist": bool(raw.get("watchlist")),
        "avatar": bool(raw.get("avatar")),
        "ready": bool(raw.get("ready")),
    }


def _text_has_login(page) -> bool:
    try:
        return bool(header_auth_state(page).get("login"))
    except BrowserSessionError:
        raise
    except Exception:
        return False


def is_logged_in(page) -> bool:
    """True only when the top bar has switched off Log In and shows Watchlist or the user avatar."""
    if page is None:
        return False
    try:
        state = header_auth_state(page)
        if not state.get("ready") or state.get("login"):
            return False
        return bool(state.get("watchlist") or state.get("avatar"))
    except BrowserSessionError:
        raise
    except Exception:
        return False


def home_is_ready(page) -> bool:
    """True when the user is logged in and the home Search control is actually on screen."""
    try:
        if not is_logged_in(page):
            return False
        return bool(page.locator(sel.SEARCH_TRIGGER).first.is_visible())
    except Exception as exc:
        if is_target_closed(exc):
            raise BrowserSessionError(BROWSER_CLOSED) from exc
        return False


def wait_home_ready(page, emit: Optional[OnEvent] = None, timeout: float = 25.0) -> bool:
    """Wait until the home shell is actually usable. Header avatar can appear before Search."""
    if emit:
        emit("首页还在加载，等搜索框稳定后再输入")
    deadline = time.time() + timeout
    stable_since = 0.0
    while time.time() < deadline:
        if page_is_closed(page):
            raise BrowserSessionError(BROWSER_CLOSED)
        dismiss_announcement_modal(page, emit)
        if home_is_ready(page):
            if stable_since <= 0:
                stable_since = time.time()
            elif time.time() - stable_since >= 1.6:
                if emit:
                    emit("首页已加载完，可以搜索")
                return True
        else:
            stable_since = 0.0
        _sleep_page(page, 400)
    if emit:
        emit("首页仍未完全加载")
    return False


def announcement_modal_visible(page) -> bool:
    """True only for the update carousel (Next/Got it), not the wallet login dialog."""
    try:
        if not _any_visible(page.locator(sel.ANNOUNCE_MODAL), limit=4):
            return False
        for selector in ANNOUNCE_ACTIONS:
            if _any_visible(page.locator(selector), limit=4):
                return True
        return False
    except Exception as exc:
        if is_target_closed(exc):
            raise BrowserSessionError(BROWSER_CLOSED) from exc
        return False


def login_dialog_visible(page) -> bool:
    """Wallet/QR login popup. Header search input is always in the DOM — ignore that."""
    try:
        if _any_visible(page.locator(sel.QR_CANDIDATES), limit=8):
            return True
        if _any_visible(page.locator(sel.SEARCH_MODAL), limit=2):
            return False
        if announcement_modal_visible(page):
            return False
        if _any_visible(page.locator("[role='dialog']"), limit=6):
            return True
        if _any_visible(page.locator(".pi-modal"), limit=4):
            return True
        if _any_visible(page.locator("iframe"), limit=6):
            return True
        return False
    except Exception as exc:
        if is_target_closed(exc):
            raise BrowserSessionError(BROWSER_CLOSED) from exc
        return False


def dismiss_announcement_modal(page, emit: Optional[OnEvent] = None) -> int:
    """Click Next/Finish on the GMGN update carousel. Never Escape — that also closes the login popup."""
    clicks = 0
    for step in range(8):
        if page_is_closed(page):
            raise BrowserSessionError(BROWSER_CLOSED)
        if not announcement_modal_visible(page):
            break
        clicked = False
        for selector in ANNOUNCE_ACTIONS:
            btn = page.locator(selector)
            if not _any_visible(btn, limit=3):
                continue
            if emit:
                emit(f"关闭更新遮罩（{step + 1}/8）")
            try:
                btn.first.click(force=True, timeout=3000)
                clicks += 1
                clicked = True
                _sleep_page(page, 400)
                break
            except Exception as exc:
                if is_target_closed(exc):
                    raise BrowserSessionError(BROWSER_CLOSED) from exc
                continue
        if clicked:
            continue
        break
    if announcement_modal_visible(page) and emit:
        emit("更新遮罩仍在，尝试继续登录")
    return clicks


def _sleep_page(page, ms: int) -> None:
    try:
        page.wait_for_timeout(ms)
    except Exception as exc:
        if is_target_closed(exc):
            raise BrowserSessionError(BROWSER_CLOSED) from exc
        time.sleep(max(0.05, ms / 1000))


def require_logged_in(page, storage_state: dict[str, Any] | None) -> None:
    if announcement_modal_visible(page):
        raise BrowserSessionError("更新遮罩未关掉，登录按钮点不到")
    for _ in range(4):
        if is_logged_in(page) and not announcement_modal_visible(page):
            break
        dismiss_announcement_modal(page, None)
        try:
            page.wait_for_timeout(500)
        except Exception as exc:
            if is_target_closed(exc):
                raise BrowserSessionError(BROWSER_CLOSED) from exc
    else:
        raise BrowserSessionError("顶部仍是 Log In，没有切换成 Watchlist/头像")
    if not has_login_cookies(storage_state):
        raise BrowserSessionError("未拿到登录 Cookie，无法采集数据")


def _header_login_button(page):
    try:
        signup = page.locator(f'{sel.SIGNUP_BTN}:has-text("{sel.LOG_IN_TEXT}")')
        if _any_visible(signup, limit=2):
            return signup.first
    except Exception:
        pass
    try:
        signup = page.locator(sel.SIGNUP_BTN)
        if _any_visible(signup, limit=2):
            return signup.first
    except Exception:
        pass
    loc = page.get_by_text(sel.LOG_IN_TEXT, exact=True)
    try:
        total = loc.count()
    except Exception:
        return None
    for idx in range(min(total, 8)):
        el = loc.nth(idx)
        try:
            if not el.is_visible():
                continue
            in_overlay = el.evaluate("node => !!node.closest('.pi-modal, [role=dialog]')")
            if in_overlay:
                continue
        except Exception:
            continue
        return el
    return None


def click_login(page) -> bool:
    if page_is_closed(page):
        raise BrowserSessionError(BROWSER_CLOSED_LOGIN)
    btn = _header_login_button(page)
    if btn is None:
        return False
    try:
        btn.click(force=True, timeout=8000)
        _sleep_page(page, 600)
        return True
    except Exception as exc:
        if is_target_closed(exc):
            raise BrowserSessionError(BROWSER_CLOSED_LOGIN) from exc
        logger.info("点击右上角 Log In 失败", exc_info=True)
        return False


def wait_until_logged_in(
    page,
    timeout: float,
    cancel_event: Optional[threading.Event] = None,
    emit: Optional[OnEvent] = None,
) -> bool:
    deadline = time.time() + timeout
    last_click = time.time()
    confirm_hits = 0
    while time.time() < deadline:
        if cancel_event is not None and cancel_event.is_set():
            raise AnalysisCancelledError()
        if page_is_closed(page):
            raise BrowserSessionError(BROWSER_CLOSED_LOGIN)
        try:
            dismiss_announcement_modal(page, emit)
            logged_in = is_logged_in(page)
            login_visible = _text_has_login(page)
            dialog = login_dialog_visible(page)
            announcement = announcement_modal_visible(page)
        except Exception as exc:
            if is_target_closed(exc):
                raise BrowserSessionError(BROWSER_CLOSED_LOGIN) from exc
            raise
        if logged_in and not announcement:
            confirm_hits += 1
            if confirm_hits >= 2:
                return True
        else:
            confirm_hits = 0
        now = time.time()
        if dialog:
            if emit and now - last_click >= LOGIN_RETRY_GAP:
                emit("已打开登录窗口，请扫码。右上角 Log In 是登录，不是注册")
                last_click = now
        elif should_reopen_login(
            logged_in=logged_in,
            login_visible=login_visible,
            login_dialog=dialog,
            announcement=announcement,
        ):
            if now - last_click >= LOGIN_RETRY_GAP:
                if emit:
                    emit("登录窗口关了，右上角仍是 Log In，重新打开登录")
                click_login(page)
                last_click = now
        _sleep_page(page, 800)
    dismiss_announcement_modal(page, emit)
    return is_logged_in(page)


def _unique_activity_labels(items: list[dict[str, Any]]) -> list[str]:
    ordered: list[str] = []
    seen: set[str] = set()
    for item in items:
        for label in activity_token_labels(item):
            if label not in seen:
                seen.add(label)
                ordered.append(label)
    return ordered


def window_scroll_progress(
    start_ts: int,
    oldest: int,
    round_idx: int,
    max_rounds: int,
    now: int | None = None,
) -> tuple[int, int]:
    """Map activity-table scrolling onto a determinate 0–100 (or round) bar."""
    if start_ts:
        current = int(now if now is not None else time.time())
        span = max(1, current - start_ts)
        covered = max(0, current - oldest) if oldest else 0
        done = min(99, int(100 * covered / span))
        if oldest and oldest <= start_ts:
            done = 100
        return done, 100
    total = max(1, int(max_rounds))
    return min(round_idx + 1, total), total


class CaptureBag:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.by_kind: dict[str, list[Any]] = {
            "activity": [],
            "holdings": [],
            "stats": [],
            "profits": [],
            "token": [],
            "pool": [],
            "search": [],
            "other": [],
        }
        self.raw: list[dict[str, Any]] = []

    def add(self, url: str, payload: Any) -> None:
        kind = classify_url(url)
        with self.lock:
            self.by_kind.setdefault(kind, []).append(payload)
            self.raw.append({"url": url, "kind": kind, "data": payload})

    def clear(self) -> None:
        with self.lock:
            for key in list(self.by_kind):
                self.by_kind[key] = []
            self.raw = []

    def activities(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        seen: set[int] = set()
        with self.lock:
            payloads = list(self.by_kind.get("activity") or [])
        for payload in payloads:
            for item in extract_activity_items(payload):
                marker = id(item) if not isinstance(item, dict) else hash(json.dumps(item, sort_keys=True, default=str))
                if marker in seen:
                    continue
                seen.add(marker)
                items.append(item)
        return items


class GmgnScraper:
    def __init__(
        self,
        session,
        *,
        cancel_event: Optional[threading.Event] = None,
        on_event: Optional[OnEvent] = None,
        on_raw: Optional[Callable[[str, dict[str, Any]], None]] = None,
    ) -> None:
        self.session = session
        self.cancel_event = cancel_event
        self.on_event = on_event
        self.on_raw = on_raw
        self.capture = CaptureBag()
        self._listening = False
        self._token_details: dict[str, dict[str, Any]] = {}
        self._mint_labels: dict[str, list[str]] = {}
        self._wallet = ""
        self._capture_gen = 0
        self._pg_done = 0
        self._pg_total = 0
        self._pg_token = ""
        self._pg_stage = "browser"

    def _set_progress(
        self,
        *,
        done: int | None = None,
        total: int | None = None,
        token: str | None = None,
        stage: str | None = None,
    ) -> None:
        if done is not None:
            self._pg_done = int(done)
        if total is not None:
            self._pg_total = int(total)
        if token is not None:
            self._pg_token = str(token)
        if stage is not None:
            self._pg_stage = str(stage)

    def emit(self, message: str) -> None:
        logger.info(message)
        if not self.on_event:
            return
        extra = {
            "done": self._pg_done,
            "total": self._pg_total,
            "token": self._pg_token,
            "stage": self._pg_stage,
        }
        try:
            self.on_event(message, **extra)
        except TypeError:
            self.on_event(message)

    def _check(self) -> None:
        if self.cancel_event is not None and self.cancel_event.is_set():
            raise AnalysisCancelledError()

    @property
    def page(self):
        page = getattr(self.session, "page", None)
        if page is None:
            raise BrowserSessionError("浏览器页面未就绪")
        return page

    def attach(self) -> None:
        if self._listening:
            return
        self.page.on("response", self._on_response)
        self._listening = True

    def _keep_response(self, url: str) -> bool:
        wallet = self._wallet
        if not wallet:
            return True
        kind = classify_url(url)
        if kind not in ("activity", "holdings", "stats", "profits"):
            return True
        if wallet in url:
            return True
        named = re.findall(r"(?:wallet|address)=([1-9A-HJ-NP-Za-km-z]{32,44})", url, flags=re.I)
        named.extend(ADDRESS_HREF_RE.findall(url))
        if named and wallet not in named:
            return False
        return True

    def _on_response(self, response) -> None:
        gen = self._capture_gen
        try:
            url = response.url or ""
            host = urlparse(url).hostname or ""
            if "gmgn.ai" not in host:
                return
            ctype = (response.headers or {}).get("content-type", "")
            if "json" not in ctype and "javascript" not in ctype:
                if "/api/" not in url and "/vas/" not in url and "/defi/" not in url:
                    return
            if response.status != 200:
                return
            payload = response.json()
        except Exception:
            return
        if gen != self._capture_gen:
            return
        if not self._keep_response(url):
            return
        self.capture.add(url, payload)
        if self.on_raw:
            try:
                self.on_raw(url, payload if isinstance(payload, dict) else {"data": payload})
            except Exception:
                pass

    def reset_for_wallet(self, wallet: str) -> None:
        """Each wallet gets a clean capture bag and a closed drawer. Never reuse the previous address."""
        prev = self._wallet
        if prev:
            self.emit(f"切换钱包 {wallet[:8]}，已清空上一地址 {prev[:8]} 的流水缓存")
            self._close_detail()
        self._wallet = wallet
        self._capture_gen += 1
        self.capture.clear()
        self._token_details = {}
        self._mint_labels = {}
        self._set_progress(done=0, total=0, token="", stage="browser")

    def open_wallet(self, wallet: str) -> None:
        self._check()
        self.attach()
        self.reset_for_wallet(wallet)
        page = self.page
        dismiss_announcement_modal(page, self.emit)
        if self._drawer_open():
            self._close_detail()
            self.capture.clear()
        self.emit(f"直接打开地址页 {wallet[:8]}…")
        page.goto(sel.ADDRESS_URL.format(wallet=wallet), wait_until="domcontentloaded")
        dismiss_announcement_modal(page, self.emit)
        self._wait_wallet_page(wallet)

    def _search_and_open(self, wallet: str) -> bool:
        page = self.page
        try:
            dismiss_announcement_modal(page, self.emit)
            trigger = page.locator(sel.SEARCH_TRIGGER).first
            trigger.wait_for(state="visible", timeout=12000)
            _sleep_page(page, 500)
            self.emit("打开搜索框")
            trigger.click(timeout=8000)
            modal = page.locator(sel.SEARCH_MODAL)
            modal.wait_for(state="visible", timeout=10000)
            box = modal.locator("input").last
            box.wait_for(state="visible", timeout=8000)
            box.click(timeout=5000)
            _sleep_page(page, 350)
            box.fill("")
            _sleep_page(page, 200)
            self.emit("输入钱包地址")
            box.type(wallet, delay=35)
            _sleep_page(page, 1000)
            link = page.locator(f'{sel.SEARCH_WALLET} a[href*="/sol/address/{wallet}"]').first
            try:
                link.wait_for(state="visible", timeout=10000)
            except Exception:
                link = modal.locator(f'a[href*="/sol/address/{wallet}"]').first
                link.wait_for(state="visible", timeout=6000)
            self.emit("点搜索结果打开钱包")
            link.click(timeout=8000)
            _sleep_page(page, 1500)
            url = page.url or ""
            return wallet[:8] in url and "/sol/address/" in url
        except Exception as exc:
            if is_target_closed(exc):
                raise BrowserSessionError(BROWSER_CLOSED) from exc
            logger.info("搜索点击失败", exc_info=True)
            return False

    def _wait_wallet_page(self, wallet: str) -> None:
        page = self.page
        deadline = time.time() + 25
        while time.time() < deadline:
            self._check()
            url = page.url or ""
            if wallet[:8] in url and "/sol/address/" in url:
                return
            _sleep_page(page, 400)
        if wallet[:8] not in (page.url or "") or "/sol/address/" not in (page.url or ""):
            page.goto(sel.ADDRESS_URL.format(wallet=wallet), wait_until="domcontentloaded")
            dismiss_announcement_modal(page, self.emit)

    def scroll_until_coverage(self, start_ts: int, *, max_rounds: int = 80) -> list[dict[str, Any]]:
        """Scroll first; do not open token drawers until the window is covered.

        The GMGN activity table loads rows into the DOM before the matching
        ``wallet_activity`` JSON response is captured.  Stopping the moment the
        oldest captured activity enters the window therefore misses the rows
        that are visible on screen but whose API response has not arrived yet.

        To avoid that, coverage is only declared when:
        * the oldest captured activity is at or before ``start_ts`` AND
        * the captured API count has caught up with the visible DOM rows AND
        * that stable state holds for two consecutive rounds (debounce).
        """
        self._check()
        self.attach()
        self._reveal_activity_table()
        if start_ts:
            from app.utils.time_utils import format_datetime

            self.emit(f"下拉流水直到早于 {format_datetime(start_ts)}")
        self._set_progress(done=0, total=100 if start_ts else max_rounds, token="", stage="browser_scroll")
        stagnant = 0
        last_count = -1
        last_oldest = 0
        last_rows = -1
        covered_confirms = 0
        for round_idx in range(max_rounds):
            self._check()
            activities = self.capture.activities()
            oldest = oldest_activity_ts(activities)
            count = len(activities)
            rows = self._row_count()
            labels = _unique_activity_labels(activities)
            hint = self._activity_scan_hint()
            tail = hint.get("lastTime") or "-"
            recent = "、".join(labels[-8:]) if labels else "-"
            visible_names = [str(x) for x in (hint.get("names") or []) if x]
            visible = "、".join(visible_names[-6:]) if visible_names else "-"
            done, total = window_scroll_progress(start_ts, oldest, round_idx, max_rounds)
            self._set_progress(done=done, total=total, stage="browser_scroll")
            self.emit(
                f"下拉加载流水 round={round_idx + 1} 行={rows} 接口={count} "
                f"oldest={oldest or '-'} 表尾={tail} 可见={visible} 接口代币={len(labels)} 最近={recent}"
            )
            leftover = round_idx == 0 and rows > 0 and count > max(rows * 3, 80)
            if leftover:
                self.emit("接口条数远大于当前表格，当作上一钱包残留，继续下拉")
                self._capture_gen += 1
                self.capture.clear()
                stagnant = 0
                last_count = -1
                last_oldest = 0
                covered_confirms = 0
                continue
            in_window = bool(start_ts and oldest and oldest <= start_ts)
            api_caught_up = count >= rows or rows == 0
            if in_window and api_caught_up:
                covered_confirms += 1
                if covered_confirms >= 2:
                    self.emit(f"时间窗口已覆盖且接口已同步（{count} 条），停止下拉")
                    break
                self.emit(f"时间窗口已覆盖，确认第 {covered_confirms}/2 轮 接口={count} 行={rows}")
            else:
                covered_confirms = 0
                if in_window and not api_caught_up:
                    self.emit(f"接口落后于表格（接口={count} 行={rows}），继续下拉等接口同步")
            if count == last_count and oldest == last_oldest and rows == last_rows:
                stagnant += 1
            else:
                stagnant = 0
            last_count = count
            last_oldest = oldest
            last_rows = rows
            if stagnant == 3:
                self._unstick_activity_scroll()
            if stagnant >= 8:
                if start_ts and (not oldest or oldest > start_ts):
                    self.emit("列表停住但仍未盖住窗口，回滚再拉")
                    self._unstick_activity_scroll()
                    stagnant = 4
                else:
                    self.emit("列表不再增长，停止下拉")
                    break
            self._wheel_activity(800)
            _sleep_page(self.page, 500)
            self._wheel_activity(500)
            _sleep_page(self.page, 800)
        activities = self.capture.activities()
        labels = _unique_activity_labels(activities)
        preview = "、".join(labels[:40]) if labels else "无"
        extra = f" 等{len(labels)}个" if len(labels) > 40 else ""
        self.emit(f"流水已扫 {len(activities)} 条，表内代币 {len(labels)} 个：{preview}{extra}")
        return activities

    def unique_tokens(self, activities: list[dict[str, Any]] | None = None) -> list[str]:
        items = activities if activities is not None else self.capture.activities()
        self._index_mint_labels(items)
        mints = extract_token_mints(items)
        current = self._wallet
        if current:
            mints = [mint for mint in mints if mint != current]
        return mints

    def _index_mint_labels(self, items: list[dict[str, Any]]) -> None:
        for item in items:
            mints = extract_token_mints(item)
            labels = activity_token_labels(item)
            if not mints or not labels:
                continue
            for mint in mints:
                bucket = self._mint_labels.setdefault(mint, [])
                for label in labels:
                    if label not in bucket:
                        bucket.append(label)

    def _labels_for_mint(self, mint: str) -> list[str]:
        if mint not in self._mint_labels:
            self._index_mint_labels(self.capture.activities())
        return list(self._mint_labels.get(mint) or [])

    def open_token_detail(self, mint: str) -> dict[str, Any]:
        self._check()
        if mint in self._token_details:
            return self._token_details[mint]
        labels = self._labels_for_mint(mint)
        shown = labels[0] if labels else mint[:8]
        self.emit(f"打开代币详情 {shown}（{mint[:8]}）…")
        page = self.page
        before = len(self.capture.by_kind.get("token") or [])
        opened = self._open_token_from_list(mint, labels)
        if not opened:
            self.emit(f"头像/名字未点到 {shown}，跳过该详情")
            detail = {"token_address": mint, "opened": False}
            self._token_details[mint] = detail
            return detail
        _sleep_page(page, 900)
        self._scroll_drawer_until_complete(mint, shown)
        drawer_text = self._drawer_text()
        drawer_metrics = parse_drawer_metrics(drawer_text)
        if drawer_text:
            logger.info(
                "抽屉文本 %s 长度=%d 片段=%r total_profit=%s avg_mc_buy=%s bought=%s",
                shown, len(drawer_text), drawer_text[:200], drawer_metrics.total_profit,
                drawer_metrics.avg_mc_buy, drawer_metrics.bought_usd,
            )
            try:
                from pathlib import Path as _P
                _dbg = _P("data/raw/drawer_text_debug")
                _dbg.mkdir(parents=True, exist_ok=True)
                (_dbg / f"{mint[:12]}.txt").write_text(drawer_text, encoding="utf-8")
            except Exception:
                pass
        else:
            logger.info("抽屉文本为空 %s url=%s", shown, getattr(page, "url", ""))
        after = list(self.capture.by_kind.get("token") or [])
        payload = after[-1] if len(after) > before else {}
        detail = {
            "token_address": mint,
            "opened": True,
            "drawer_text": drawer_text,
            "drawer_metrics": drawer_metrics,
            "payload": payload,
        }
        self._token_details[mint] = detail
        self._close_detail()
        return detail

    def _open_token_from_list(self, mint: str, labels: list[str]) -> bool:
        if self._click_token_href(mint):
            return self._wait_drawer()
        row = self._find_activity_row(labels)
        if row is None:
            self._scroll_until_row(labels)
            row = self._find_activity_row(labels)
        if row is None:
            return False
        return self._click_activity_row(row)

    def _click_token_href(self, mint: str) -> bool:
        loc = self.page.locator(f'a[href*="/sol/token/{mint}"]')
        try:
            if loc.count() == 0:
                return False
            target = loc.first
            box = target.bounding_box()
            if box and box.get("width", 0) > 2:
                self.page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
                return True
            target.click(timeout=2500, force=True)
            return True
        except Exception as exc:
            if is_target_closed(exc):
                raise BrowserSessionError(BROWSER_CLOSED) from exc
            return False

    def _find_activity_row(self, labels: list[str]):
        rows = self.page.locator(sel.ACTIVITY_ROW)
        try:
            if rows.count() == 0:
                rows = self.page.locator("tr.g-table-row")
        except Exception:
            rows = self.page.locator("tr.g-table-row")
        for label in labels:
            if not label:
                continue
            try:
                matched = rows.filter(has_text=label)
                if matched.count() > 0:
                    return matched.first
            except Exception as exc:
                if is_target_closed(exc):
                    raise BrowserSessionError(BROWSER_CLOSED) from exc
                continue
        return None

    def _click_activity_row(self, row) -> bool:
        try:
            row.scroll_into_view_if_needed(timeout=2500)
        except Exception:
            pass
        labels: list[str] = []
        try:
            raw = (row.locator("td").nth(1).inner_text(timeout=1500) or "").split("\n")[0].strip()
            if raw:
                labels.append(raw)
        except Exception:
            pass
        if self._js_click_token_name(row, labels):
            if self._wait_drawer():
                self.emit("详情已打开（点名字）")
                return True
        try:
            cell = row.locator("td").nth(1)
            box = cell.bounding_box()
            if box:
                self.page.mouse.click(float(box["x"]) + 40, float(box["y"]) + float(box["height"]) / 2)
                if self._wait_drawer():
                    self.emit("详情已打开（点代币格）")
                    return True
            cell.click(timeout=2500, force=True)
            if self._wait_drawer():
                self.emit("详情已打开（force 代币格）")
                return True
        except Exception as exc:
            if is_target_closed(exc):
                raise BrowserSessionError(BROWSER_CLOSED) from exc
        try:
            row.click(timeout=2500, force=True)
            if self._wait_drawer():
                self.emit("详情已打开（点整行）")
                return True
        except Exception as exc:
            if is_target_closed(exc):
                raise BrowserSessionError(BROWSER_CLOSED) from exc
            logger.info("点击流水行失败", exc_info=True)
        return False

    def _js_click_token_name(self, row, labels: list[str]) -> bool:
        try:
            return bool(
                row.evaluate(
                    """(row, labels) => {
                        const cell = row.querySelectorAll('td')[1];
                        if (!cell) return false;
                        const wanted = (labels || []).map(s => String(s).trim()).filter(Boolean);
                        const walker = document.createTreeWalker(cell, NodeFilter.SHOW_TEXT);
                        let node;
                        while ((node = walker.nextNode())) {
                            const text = (node.textContent || '').trim();
                            if (!text) continue;
                            if (!wanted.length || wanted.includes(text)) {
                                const target = node.parentElement || cell;
                                target.click();
                                return true;
                            }
                        }
                        cell.click();
                        return true;
                    }""",
                    labels,
                )
            )
        except Exception as exc:
            if is_target_closed(exc):
                raise BrowserSessionError(BROWSER_CLOSED) from exc
            return False

    def _wait_drawer(self, timeout_ms: int = 2500) -> bool:
        deadline = time.time() + timeout_ms / 1000
        while time.time() < deadline:
            if self._drawer_open():
                return True
            _sleep_page(self.page, 200)
        return self._drawer_open()

    def _drawer_open(self) -> bool:
        page = self.page
        try:
            if "/sol/token/" in (page.url or ""):
                return True
            if _any_visible(page.locator(sel.TOKEN_DRAWER), limit=8):
                return True
            if _any_visible(page.locator(".pi-drawer, [class*='drawer-content']"), limit=4):
                return True
        except Exception:
            return False
        return False

    def _scroll_until_row(self, labels: list[str], rounds: int = 24) -> None:
        self._reveal_activity_table()
        self._scroll_activity_top()
        stagnant = 0
        last_sig = self._list_signature()
        for idx in range(rounds):
            self._check()
            if page_is_closed(self.page):
                raise BrowserSessionError(BROWSER_CLOSED)
            if self._find_activity_row(labels) is not None:
                return
            if idx == 0 or idx % 5 == 4:
                shown = labels[0] if labels else "代币"
                self.emit(f"鼠标滚动右半边流水表找 {shown} ({idx + 1}/{rounds})")
            self._wheel_activity(800)
            _sleep_page(self.page, 450)
            self._wheel_activity(500)
            _sleep_page(self.page, 700)
            sig = self._list_signature()
            if sig == last_sig:
                stagnant += 1
            else:
                stagnant = 0
                last_sig = sig
            if stagnant == 3:
                self._unstick_activity_scroll()
            if stagnant >= 6:
                self.emit(f"右半边流水表不再滚动，停止找 {labels[0] if labels else '代币'}")
                return

    def _list_signature(self) -> str:
        try:
            return str(self.page.evaluate(LIST_SIGNATURE_JS) or "")
        except Exception as exc:
            if is_target_closed(exc):
                raise BrowserSessionError(BROWSER_CLOSED) from exc
            return ""

    def _reveal_activity_table(self) -> None:
        page = self.page
        try:
            tab = page.get_by_text("Trades", exact=True)
            if tab.count():
                tab.first.click(timeout=2000)
                _sleep_page(page, 400)
        except Exception as exc:
            if is_target_closed(exc):
                raise BrowserSessionError(BROWSER_CLOSED) from exc
        try:
            page.locator(sel.ACTIVITY_TABLE).first.scroll_into_view_if_needed(timeout=3000)
        except Exception:
            try:
                page.locator(sel.ACTIVITY).first.scroll_into_view_if_needed(timeout=3000)
            except Exception:
                pass
        try:
            page.locator(sel.ACTIVITY_ROW).first.wait_for(state="visible", timeout=12000)
        except Exception:
            try:
                page.locator(f"{sel.ACTIVITY} {sel.TOKEN_HEADER}").first.wait_for(state="visible", timeout=8000)
            except Exception:
                pass

    def _drawer_text(self) -> str:
        # The drawer may render as a .pi-drawer (full-page nav to /sol/token/<mint>)
        # or as the DrawerToken sentry components. Try the same selectors the
        # scroller uses first, then fall back to the sentry components, then to
        # the page main content. Without .pi-drawer here the text comes back
        # empty and every drawer metric column shows "GMGN 未提供".
        selectors = [
            ".pi-drawer",
            "[class*='drawer-content']",
            "[class*='drawer-body']",
            sel.TOKEN_DRAWER,
        ]
        for sel_expr in selectors:
            try:
                loc = self.page.locator(sel_expr).first
                if loc.count() and loc.is_visible():
                    return loc.inner_text(timeout=3000)
            except Exception as exc:
                if is_target_closed(exc):
                    raise BrowserSessionError(BROWSER_CLOSED) from exc
                continue
        # Last resort: token page main content (URL-based drawer).
        try:
            if "/sol/token/" in (self.page.url or ""):
                loc = self.page.locator("main, [class*='token-detail'], [class*='token-info']").first
                if loc.count() and loc.is_visible():
                    return loc.inner_text(timeout=3000)
        except Exception as exc:
            if is_target_closed(exc):
                raise BrowserSessionError(BROWSER_CLOSED) from exc
        return ""

    def _scroll_drawer_until_complete(self, mint: str, shown: str, *, max_rounds: int = 50) -> None:
        """Scroll the drawer's transaction table to load all wallet_activity?token= pages."""
        page = self.page
        stagnant = 0
        last_count = -1
        for round_idx in range(max_rounds):
            self._check()
            if page_is_closed(page):
                raise BrowserSessionError(BROWSER_CLOSED)
            if not self._drawer_open():
                return
            count = len(self.capture.activities())
            if count == last_count:
                stagnant += 1
            else:
                stagnant = 0
                last_count = count
            if stagnant >= 3:
                self.emit(f"详情 {shown} 流水已加载完（{count} 条）")
                return
            if round_idx % 5 == 4:
                self.emit(f"滚动抽屉加载更多 {shown} ({round_idx + 1}/{max_rounds}) 接口={count}")
            self._wheel_drawer(800)
            _sleep_page(page, 500)
            self._wheel_drawer(500)
            _sleep_page(page, 700)
        self.emit(f"详情 {shown} 抽屉滚动达上限，接口={len(self.capture.activities())}")

    def _wheel_drawer(self, delta: int = 800) -> None:
        page = self.page
        if page_is_closed(page):
            raise BrowserSessionError(BROWSER_CLOSED)
        try:
            box = page.evaluate(DRAWER_SCROLLER_JS)
        except Exception as exc:
            if is_target_closed(exc):
                raise BrowserSessionError(BROWSER_CLOSED) from exc
            box = None
        if isinstance(box, dict) and box.get("w", 0) > 80:
            x = float(box["x"]) + float(box["w"]) * 0.5
            y = float(box["y"]) + float(box["h"]) * 0.5
            try:
                page.mouse.move(x, y)
            except Exception as exc:
                if is_target_closed(exc):
                    raise BrowserSessionError(BROWSER_CLOSED) from exc
        try:
            page.evaluate(DRAWER_WHEEL_JS, delta)
        except Exception as exc:
            if is_target_closed(exc):
                raise BrowserSessionError(BROWSER_CLOSED) from exc
        try:
            page.mouse.wheel(0, delta)
        except Exception as exc:
            if is_target_closed(exc):
                raise BrowserSessionError(BROWSER_CLOSED) from exc

    def _close_detail(self) -> None:
        page = self.page
        url = page.url or ""
        for _ in range(4):
            if not self._drawer_open() and "/sol/token/" not in (page.url or ""):
                break
            try:
                closer = page.locator(sel.DRAWER_CLOSE).first
                if closer.count():
                    closer.click(force=True, timeout=1500)
                    _sleep_page(page, 250)
            except Exception as exc:
                if is_target_closed(exc):
                    raise BrowserSessionError(BROWSER_CLOSED) from exc
            try:
                page.keyboard.press("Escape")
                _sleep_page(page, 250)
            except Exception as exc:
                if is_target_closed(exc):
                    raise BrowserSessionError(BROWSER_CLOSED) from exc
        if "/sol/token/" in (page.url or url):
            try:
                page.go_back(wait_until="domcontentloaded")
                _sleep_page(page, 800)
            except Exception as exc:
                if is_target_closed(exc):
                    raise BrowserSessionError(BROWSER_CLOSED) from exc

    def _row_count(self) -> int:
        try:
            return int(self.page.locator(sel.ACTIVITY_ROW).count())
        except Exception:
            try:
                return int(self.page.locator("tr.g-table-row").count())
            except Exception:
                return 0

    def _activity_scan_hint(self) -> dict[str, Any]:
        try:
            hint = self.page.evaluate(ACTIVITY_SCAN_JS)
        except Exception as exc:
            if is_target_closed(exc):
                raise BrowserSessionError(BROWSER_CLOSED) from exc
            return {}
        return hint if isinstance(hint, dict) else {}

    def _scroller_box(self) -> dict[str, float] | None:
        try:
            box = self.page.evaluate(SCROLLER_BOX_JS)
        except Exception as exc:
            if is_target_closed(exc):
                raise BrowserSessionError(BROWSER_CLOSED) from exc
            box = None
        if not isinstance(box, dict):
            return None
        return box

    def _wheel_point(self, box: dict[str, float]) -> tuple[float, float]:
        width = float(box.get("w") or 0)
        height = float(box.get("h") or 0)
        x = float(box.get("x") or 0) + max(width * 0.62, 48)
        y = float(box.get("y") or 0) + max(height * 0.55, 36)
        return x, y

    def _wheel_activity(self, delta: int = 1000) -> None:
        page = self.page
        if page_is_closed(page):
            raise BrowserSessionError(BROWSER_CLOSED)
        box = self._scroller_box()
        try:
            if box:
                x, y = self._wheel_point(box)
                page.mouse.move(x, y)
            else:
                size = getattr(page, "viewport_size", None) or {}
                x = float(size.get("width") or 1280) * 0.72
                y = float(size.get("height") or 800) * 0.62
                page.mouse.move(x, y)
        except Exception as exc:
            if is_target_closed(exc):
                raise BrowserSessionError(BROWSER_CLOSED) from exc
        try:
            page.evaluate(WHEEL_JS, delta)
        except Exception as exc:
            if is_target_closed(exc):
                raise BrowserSessionError(BROWSER_CLOSED) from exc
        try:
            page.mouse.wheel(0, delta)
        except Exception as exc:
            if is_target_closed(exc):
                raise BrowserSessionError(BROWSER_CLOSED) from exc

    def _unstick_activity_scroll(self) -> None:
        """Kuaishou-style bounce: roll back, then jump to the end of the inner scroller."""
        self.emit("流水表卡住，回滚再拉到底")
        self._wheel_activity(-900)
        _sleep_page(self.page, 600)
        try:
            self.page.evaluate(JUMP_END_JS)
        except Exception as exc:
            if is_target_closed(exc):
                raise BrowserSessionError(BROWSER_CLOSED) from exc
        self._wheel_activity(700)
        _sleep_page(self.page, 800)

    def _scroll_activity_top(self) -> None:
        try:
            self.page.evaluate(RESET_SCROLL_JS)
        except Exception as exc:
            if is_target_closed(exc):
                raise BrowserSessionError(BROWSER_CLOSED) from exc
        self._wheel_activity(-1600)
        _sleep_page(self.page, 220)

    def _scroll_list(self) -> None:
        self._wheel_activity(1100)
