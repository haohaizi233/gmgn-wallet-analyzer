from __future__ import annotations

import json
import os
import threading
import time
from typing import Any, Callable, Optional

from app.api.exceptions import AnalysisCancelledError
from app.browser.cookie_pool import CookieAccount, CookiePool
from app.browser.exceptions import BrowserNetworkError, BrowserSessionError, PlaywrightNotAvailable
from app.browser.selectors import HOME_URL
from app.utils.logger import get_logger

NAV_TIMEOUT_MS = 20_000
DCL_TIMEOUT_MS = 8_000
NAV_RETRIES = 3
LOGIN_POLL_S = 20.0

logger = get_logger("gmgn.browser")

OnEvent = Callable[[str], None]


def require_playwright():
    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeout
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise PlaywrightNotAvailable(
            "未安装 Playwright。请执行：pip install playwright"
        ) from exc
    return sync_playwright, PlaywrightTimeout


def _ensure_bundled_browsers() -> None:
    """Point Playwright at the bundled browser kernel when running as a frozen exe."""
    from app.utils.paths import is_frozen, APP_DIR

    if not is_frozen():
        return
    if os.environ.get("PLAYWRIGHT_BROWSERS_PATH"):
        return
    bundled = APP_DIR / "playwright_browsers"
    if bundled.is_dir():
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(bundled)


class BrowserSession:
    """One Playwright context for a job. Reuses cookies; QR-login only when the pool is empty or dead."""

    def __init__(
        self,
        cookie_pool: CookiePool,
        *,
        headed: bool = True,
        qr_timeout: float = 180.0,
        cancel_event: Optional[threading.Event] = None,
        on_event: Optional[OnEvent] = None,
    ) -> None:
        self.cookies = cookie_pool
        self.headed = bool(headed)
        self.qr_timeout = max(30.0, float(qr_timeout))
        self.cancel_event = cancel_event
        self.on_event = on_event
        self._playwright = None
        self._browser = None
        self.context = None
        self.page = None
        self.account: Optional[CookieAccount] = None
        self._closed = False

    def emit(self, message: str) -> None:
        logger.info(message)
        if self.on_event:
            self.on_event(message)

    def _check(self) -> None:
        if self.cancel_event is not None and self.cancel_event.is_set():
            raise AnalysisCancelledError()

    def start(self) -> None:
        from app.browser.cookie_pool import has_login_cookies

        self._check()
        _ensure_bundled_browsers()
        sync_playwright, _timeout = require_playwright()
        self._playwright = sync_playwright().start()
        self.emit("正在启动浏览器内核…")
        self._browser = self._launch_browser()
        self._arm_cancel_watch()
        stored_n = len(self.cookies.all())
        ready_n = len(self.cookies.usable())
        if stored_n and not ready_n:
            self.emit(f"号池 {stored_n} 个文件都没有登录 Cookie，不能复用")
        account = self.cookies.acquire()
        while account is not None:
            self._check()
            stored = self.cookies.load_storage(account)
            if not has_login_cookies(stored):
                self.emit(f"跳过 {account.id}：文件里没有登录 Cookie，不能复用")
                self.cookies.mark_expired(account, "no_login_cookie")
                account = self.cookies.acquire()
                continue
            self.emit(f"尝试复用 Cookie {account.id}")
            try:
                ok = self._open_with_storage(account)
            except AnalysisCancelledError:
                raise
            except BrowserNetworkError:
                raise
            if ok:
                self.account = account
                self.cookies.mark_valid(account)
                self.emit(f"Cookie {account.id} 有效，已登录")
                return
            self.emit(f"Cookie {account.id} 登录态无效（顶部未切换成已登录）")
            self.cookies.mark_expired(account, "login_invalid")
            account = self.cookies.acquire()
        from app.browser.scraper import header_auth_state, page_is_closed

        if self.page is not None and not page_is_closed(self.page):
            try:
                header = header_auth_state(self.page)
            except Exception:
                header = {}
            if header.get("login") or "gmgn.ai" in (getattr(self.page, "url", "") or ""):
                self.emit("没有可复用的登录 Cookie，请在当前窗口顶部扫码")
                self._qr_login()
                return
        self.emit("没有可复用的登录 Cookie，打开扫码登录")
        self._open_fresh(headed=True)
        self._qr_login()

    def storage_state(self) -> dict[str, Any]:
        if self.context is None:
            return {}
        return self.context.storage_state()

    def persist_login(self) -> CookieAccount:
        from app.browser.scraper import dismiss_announcement_modal, require_logged_in

        if self.page is None or self.context is None:
            raise BrowserSessionError("页面未就绪，无法保存 Cookie")
        dismiss_announcement_modal(self.page, self.emit)
        state = self.storage_state()
        require_logged_in(self.page, state)
        self.account = self.cookies.save_storage(state, account=self.account, label="gmgn.ai")
        self.emit(f"已保存 Cookie {self.account.id}（DOM 已登录且号池写入成功）")
        return self.account

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for obj_name in ("context", "_browser", "_playwright"):
            obj = getattr(self, obj_name, None)
            if obj is None:
                continue
            closer = getattr(obj, "close", None) or getattr(obj, "stop", None)
            if closer is None:
                continue
            try:
                closer()
            except Exception:
                logger.debug("关闭浏览器资源失败: %s", obj_name, exc_info=True)
        self.context = None
        self.page = None
        self._browser = None
        self._playwright = None

    def _launch_browser(self):
        chromium = self._playwright.chromium
        args = [
            "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage",
        ]
        last_error: Exception | None = None
        for channel in ("chrome", "msedge", None):
            kwargs: dict[str, Any] = {
                "headless": not self.headed,
                "args": args,
            }
            if channel:
                kwargs["channel"] = channel
            try:
                browser = chromium.launch(**kwargs)
                self.emit(f"浏览器已启动（{'有头' if self.headed else '无头'} / {channel or 'chromium'}）")
                return browser
            except Exception as exc:
                last_error = exc
                logger.info("启动 %s 失败: %s", channel or "chromium", exc)
        raise PlaywrightNotAvailable(
            "无法启动 Chrome / Edge / Chromium。请安装 Chrome，或执行 playwright install chromium"
        ) from last_error

    def _cancelled(self) -> bool:
        return self.cancel_event is not None and self.cancel_event.is_set()

    def _arm_cancel_watch(self) -> None:
        event = self.cancel_event
        if event is None:
            return

        def watch() -> None:
            while not self._closed:
                if event.wait(0.2):
                    break
            if self._closed:
                return
            logger.info("收到停止，关闭浏览器")
            for obj_name in ("page", "context", "_browser"):
                obj = getattr(self, obj_name, None)
                if obj is None:
                    continue
                closer = getattr(obj, "close", None)
                if closer is None:
                    continue
                try:
                    closer()
                except Exception:
                    pass

        threading.Thread(target=watch, daemon=True, name="gmgn-browser-cancel").start()

    def _closed_or_cancelled(self, exc: BaseException) -> bool:
        name = type(exc).__name__
        text = str(exc).lower()
        return self._cancelled() or "TargetClosed" in name or "has been closed" in text

    def _new_context(self, storage_state: dict[str, Any] | str | None = None):
        if self._browser is None:
            raise BrowserSessionError("浏览器未启动")
        kwargs: dict[str, Any] = {
            "viewport": {"width": 1440, "height": 960},
            "locale": "en-US",
            "timezone_id": "Asia/Shanghai",
        }
        # Do not pass storage_state here — restoring ~80KB localStorage can hang
        # new_context for a minute with no window and no logs.
        _ = storage_state
        self.emit("正在打开浏览器窗口…")
        context = self._browser.new_context(**kwargs)
        context.set_default_timeout(NAV_TIMEOUT_MS)
        context.set_default_navigation_timeout(NAV_TIMEOUT_MS)
        page = context.new_page()
        page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        self.emit("浏览器窗口已打开")
        return context, page

    def _restore_storage(self, context, page, state: dict[str, Any] | None) -> None:
        if not state:
            return
        cookies = [c for c in (state.get("cookies") or []) if isinstance(c, dict)]
        if cookies:
            self.emit(f"正在写入 Cookie（{len(cookies)} 条）")
            try:
                context.add_cookies(cookies)
            except Exception:
                logger.info("写入 Cookie 失败", exc_info=True)
        items: list[dict[str, str]] = []
        for origin in state.get("origins") or []:
            if not isinstance(origin, dict):
                continue
            if "gmgn.ai" not in str(origin.get("origin") or ""):
                continue
            for entry in origin.get("localStorage") or []:
                if not isinstance(entry, dict):
                    continue
                name = str(entry.get("name") or "")
                if not name:
                    continue
                items.append({"name": name, "value": str(entry.get("value") or "")})
        if not items:
            return
        payload = json.dumps(items, ensure_ascii=False)
        page.add_init_script(
            f"""() => {{
                const items = {payload};
                try {{
                    for (const item of items) {{
                        localStorage.setItem(item.name, item.value);
                    }}
                }} catch (e) {{}}
            }}"""
        )
        self.emit(f"已注入登录缓存（{len(items)} 项）")

    def _swap_context(self, context, page) -> None:
        old = self.context
        self.context = context
        self.page = page
        if old is not None and old is not context:
            try:
                old.close()
            except Exception:
                pass

    def _open_with_storage(self, account: CookieAccount) -> bool:
        from app.browser.cookie_pool import has_login_cookies
        from app.browser.scraper import dismiss_announcement_modal, header_auth_state, is_logged_in, wait_home_ready

        state = self.cookies.load_storage(account)
        if not state or not has_login_cookies(state):
            return False
        context, page = self._new_context()
        self._swap_context(context, page)
        self._restore_storage(context, page, state)
        try:
            self._goto_home(page)
            self.emit("正在看顶部导航是否从 Log In 切成 Watchlist/头像…")
            deadline = time.time() + LOGIN_POLL_S
            last_beat = 0.0
            while time.time() < deadline:
                self._check()
                try:
                    dismiss_announcement_modal(page, self.emit)
                    header = header_auth_state(page)
                    if header.get("login"):
                        self.emit(f"顶部仍是 Log In，Cookie {account.id} 已失效")
                        return False
                    if is_logged_in(page):
                        extra = "Watchlist" if header.get("watchlist") else "头像"
                        self.emit(f"顶部已切换为已登录（{extra}），再等首页加载完")
                        wait_home_ready(page, self.emit)
                        dismiss_announcement_modal(page, self.emit)
                        if is_logged_in(page):
                            return True
                        self.emit(f"顶部登录态又没了，Cookie {account.id} 无效")
                        return False
                except AnalysisCancelledError:
                    raise
                except Exception as exc:
                    if self._closed_or_cancelled(exc):
                        self._check()
                        raise
                now = time.time()
                if now - last_beat >= 3:
                    left = max(0, int(deadline - now))
                    self.emit(f"顶部导航尚未切换成已登录（剩余 {left}s）")
                    last_beat = now
                try:
                    page.wait_for_timeout(400)
                except Exception as exc:
                    if self._closed_or_cancelled(exc):
                        self._check()
                        raise BrowserSessionError("浏览器窗口被关掉了，采集中断") from exc
            self.emit(f"Cookie {account.id} 未通过校验：顶部没有 Watchlist/头像")
            return False
        except AnalysisCancelledError:
            raise
        except BrowserNetworkError:
            raise
        except Exception as exc:
            if self._closed_or_cancelled(exc):
                self._check()
                raise
            logger.info("用 Cookie %s 打开失败", account.id, exc_info=True)
            return False

    def _goto_home(self, page) -> None:
        last: Exception | None = None
        for attempt in range(1, NAV_RETRIES + 1):
            self._check()
            try:
                self.emit(f"打开 {HOME_URL}（第 {attempt}/{NAV_RETRIES} 次）")
                page.goto(HOME_URL, wait_until="commit", timeout=NAV_TIMEOUT_MS)
                self.emit("首页已连通，等待渲染…")
                try:
                    page.wait_for_load_state("domcontentloaded", timeout=DCL_TIMEOUT_MS)
                except Exception as exc:
                    if self._closed_or_cancelled(exc):
                        self._check()
                        raise
                    self.emit("渲染较慢，先继续用当前页面")
                    try:
                        page.wait_for_timeout(400)
                    except Exception:
                        pass
                return
            except AnalysisCancelledError:
                raise
            except Exception as exc:
                last = exc
                if self._closed_or_cancelled(exc):
                    self._check()
                    raise
                text = str(exc)
                if "Timeout" not in type(exc).__name__ and "timeout" not in text.lower() and "net::" not in text.lower():
                    raise
                self.emit(f"打开 gmgn.ai 失败（网络/超时）：{type(exc).__name__}，准备重试")
                try:
                    page.wait_for_timeout(800)
                except Exception:
                    pass
        raise BrowserNetworkError(
            "打不开 https://gmgn.ai/?chain=sol 。请检查网络或代理。这次没有把 Cookie 标成失效。"
        ) from last

    def _open_fresh(self, headed: bool | None = None) -> None:
        if headed is True and not self.headed:
            self.emit("扫码必须有头，正在切换为有头浏览器")
            self.headed = True
            if self._browser is not None:
                try:
                    self._browser.close()
                except Exception:
                    pass
            self._browser = self._launch_browser()
        context, page = self._new_context()
        self._swap_context(context, page)
        self._goto_home(page)
        from app.browser.scraper import dismiss_announcement_modal

        dismiss_announcement_modal(page, self.emit)

    def _qr_login(self) -> None:
        from app.browser.scraper import click_login, dismiss_announcement_modal, is_logged_in, wait_until_logged_in

        if self.page is None:
            raise BrowserSessionError("页面未就绪")
        dismiss_announcement_modal(self.page, self.emit)
        if is_logged_in(self.page):
            self.persist_login()
            return
        click_login(self.page)
        self.emit("请用 GMGN / 钱包扫码登录（号池为空或 Cookie 全部失效）")
        if not wait_until_logged_in(
            self.page,
            timeout=self.qr_timeout,
            cancel_event=self.cancel_event,
            emit=self.emit,
        ):
            from app.browser.exceptions import BrowserLoginTimeout

            raise BrowserLoginTimeout("扫码登录超时，或登录后 DOM 仍未变成已登录状态")
        dismiss_announcement_modal(self.page, self.emit)
        self.persist_login()
        self.emit("扫码登录成功，Cookie 已写入号池")

    def login_new_account(self) -> CookieAccount:
        """Always open a headed window and add a new cookie to the pool."""
        sync_playwright, _timeout = require_playwright()
        if self._playwright is None:
            self._playwright = sync_playwright().start()
        self.headed = True
        if self._browser is None:
            self._browser = self._launch_browser()
        self.account = None
        self._open_fresh(headed=True)
        self._qr_login()
        if self.account is None:
            from app.browser.exceptions import BrowserLoginTimeout

            raise BrowserLoginTimeout("扫码登录未保存 Cookie")
        return self.account
