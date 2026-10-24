from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from app.browser.cookie_pool import HEALTHY, CookiePool, has_login_cookies
from app.browser.payloads import (
    as_openapi_page,
    activity_in_window,
    activity_token_labels,
    classify_url,
    extract_activity_items,
    extract_token_mints,
    oldest_activity_ts,
)
from app.jobs.engine import AnalysisJobEngine
from app.jobs.models import AnalysisJob
from app.domain.models import AnalysisOptions, WalletAnalysisRequest


class CookiePoolTests(unittest.TestCase):
    def test_save_acquire_reuse_and_expire(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            pool = CookiePool(root)
            self.assertFalse(pool.has_usable())
            first = pool.save_storage({"cookies": [{"name": "sid", "value": "a", "domain": ".gmgn.ai", "path": "/"}]}, label="one")
            self.assertEqual(first.state, HEALTHY)
            self.assertTrue(pool.has_usable())
            picked = pool.acquire()
            self.assertIsNotNone(picked)
            assert picked is not None
            self.assertEqual(picked.id, first.id)
            loaded = pool.load_storage(picked)
            self.assertEqual(loaded["cookies"][0]["value"], "a")
            pool.mark_expired(picked)
            self.assertFalse(pool.has_usable())
            second = pool.save_storage({"cookies": [{"name": "sid", "value": "b", "domain": ".gmgn.ai", "path": "/"}]})
            self.assertNotEqual(second.id, first.id)
            self.assertEqual(len(pool.usable()), 1)
            removed = pool.purge_expired()
            self.assertEqual(removed, 1)
            self.assertEqual(len(pool.all()), 1)

    def test_reload_from_index(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            pool = CookiePool(root)
            pool.save_storage({"cookies": []}, label="keep")
            again = CookiePool(root)
            self.assertEqual(len(again.all()), 1)
            self.assertEqual(again.all()[0].label, "keep")


class LoginCookieTests(unittest.TestCase):
    def test_rejects_empty_and_analytics_only(self):
        self.assertFalse(has_login_cookies(None))
        self.assertFalse(has_login_cookies({}))
        self.assertFalse(has_login_cookies({"cookies": []}))
        self.assertFalse(
            has_login_cookies(
                {
                    "cookies": [
                        {"name": "_ga", "value": "GA1.1", "domain": ".gmgn.ai"},
                        {"name": "_gid", "value": "GA1.2", "domain": ".gmgn.ai"},
                    ]
                }
            )
        )
        self.assertFalse(
            has_login_cookies(
                {
                    "cookies": [
                        {"name": "_did", "value": "device-only", "domain": ".gmgn.ai"},
                        {"name": "__cf_bm", "value": "cloudflare", "domain": ".gmgn.ai"},
                    ]
                }
            )
        )

    def test_junk_storage_is_not_usable(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            pool = CookiePool(root)
            pool.save_storage(
                {
                    "cookies": [
                        {"name": "_did", "value": "device-only", "domain": ".gmgn.ai", "path": "/"},
                        {"name": "__cf_bm", "value": "cloudflare", "domain": ".gmgn.ai", "path": "/"},
                    ]
                },
                label="junk",
            )
            self.assertEqual(len(pool.all()), 1)
            self.assertFalse(pool.has_usable())
            self.assertIsNone(pool.acquire())

    def test_accepts_session_cookie(self):
        self.assertTrue(
            has_login_cookies(
                {"cookies": [{"name": "access_token", "value": "abc", "domain": ".gmgn.ai", "path": "/"}]}
            )
        )
        self.assertTrue(
            has_login_cookies(
                {
                    "cookies": [
                        {"name": "foo", "value": "1", "domain": "gmgn.ai"},
                        {"name": "bar", "value": "2", "domain": ".gmgn.ai"},
                    ]
                }
            )
        )


class PayloadTests(unittest.TestCase):
    def test_extract_activity_and_mints(self):
        payload = {
            "code": 0,
            "data": {
                "activities": [
                    {
                        "tx_hash": "abc",
                        "timestamp": 1_700_000_000,
                        "event_type": "buy",
                        "token_address": "So11111111111111111111111111111111111111112",
                        "token_amount": "1",
                    },
                    {
                        "hash": "def",
                        "block_unix_time": 1_699_000_000,
                        "side": "sell",
                        "token": {"address": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"},
                    },
                ]
            },
        }
        items = extract_activity_items(payload)
        self.assertEqual(len(items), 2)
        self.assertEqual(oldest_activity_ts(items), 1_699_000_000)
        mints = extract_token_mints(items, "/sol/token/EME9M9cSy9FvfHvcx2gMPkp1H5Dj4YaKufPRsAyon8Tf")
        self.assertIn("So11111111111111111111111111111111111111112", mints)
        self.assertIn("EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", mints)
        self.assertIn("EME9M9cSy9FvfHvcx2gMPkp1H5Dj4YaKufPRsAyon8Tf", mints)
        page = as_openapi_page(items, "0", 1)
        self.assertEqual(len(page["data"]["activities"]), 1)
        self.assertEqual(page["data"]["next"], "1")
        page2 = as_openapi_page(items, "1", 1)
        self.assertIsNone(page2["data"]["next"])

    def test_activity_token_labels(self):
        self.assertEqual(
            activity_token_labels(
                {"token": {"symbol": "QQQx", "name": "QQQ", "address": "Mint111111111111111111111111111111111111111"}}
            ),
            ["QQQx", "QQQ"],
        )
        self.assertEqual(activity_token_labels({"symbol": "BABA"}), ["BABA"])

    def test_activity_in_window_keeps_22h_drops_36h(self):
        now = 1_800_000_000
        start_ts = now - 86400
        inside = {"timestamp": now - 22 * 3600, "symbol": "STRID"}
        outside = {"timestamp": now - 36 * 3600, "symbol": "OLD"}
        self.assertTrue(activity_in_window(inside, start_ts, now))
        self.assertFalse(activity_in_window(outside, start_ts, now))

    def test_classify_url(self):
        self.assertEqual(classify_url("https://gmgn.ai/api/v1/wallet_activity/sol"), "activity")
        self.assertEqual(classify_url("https://gmgn.ai/vas/api/v1/search_v3?q=abc"), "search")
        self.assertEqual(classify_url("https://gmgn.ai/api/v1/token_info/sol/xx"), "token")


class CollectionModeTests(unittest.TestCase):
    def test_options_api_wins_over_config_automation(self):
        engine = AnalysisJobEngine.__new__(AnalysisJobEngine)
        engine.config = type("C", (), {"collection_mode": "automation"})()
        job = AnalysisJob(
            job_id="j",
            wallets=["w"],
            requests=[WalletAnalysisRequest(wallet_address="w", options=AnalysisOptions(collection_mode="api"))],
            created_at=datetime.now(),
        )
        self.assertEqual(engine._collection_mode(job), "api")

    def test_gui_automation_option(self):
        engine = AnalysisJobEngine.__new__(AnalysisJobEngine)
        engine.config = type("C", (), {"collection_mode": "api"})()
        job = AnalysisJob(
            job_id="j",
            wallets=["w"],
            requests=[WalletAnalysisRequest(wallet_address="w", options=AnalysisOptions(collection_mode="automation", browser_headed=True))],
            created_at=datetime.now(),
        )
        self.assertEqual(engine._collection_mode(job), "automation")


class LoginRetryTests(unittest.TestCase):
    def test_should_reopen_login_when_header_still_says_log_in(self):
        from app.browser.scraper import should_reopen_login

        self.assertTrue(
            should_reopen_login(logged_in=False, login_visible=True, login_dialog=False, announcement=False)
        )
        self.assertFalse(
            should_reopen_login(logged_in=False, login_visible=True, login_dialog=True, announcement=False)
        )
        self.assertFalse(
            should_reopen_login(logged_in=False, login_visible=True, login_dialog=False, announcement=True)
        )
        self.assertFalse(
            should_reopen_login(logged_in=True, login_visible=False, login_dialog=False, announcement=False)
        )

    def test_wait_reopens_login_and_closed_page_is_clear_error(self):
        from unittest.mock import patch

        from app.browser.exceptions import BrowserSessionError
        from app.browser.scraper import wait_until_logged_in

        class ClosedPage:
            def is_closed(self):
                return True

        with self.assertRaises(BrowserSessionError):
            wait_until_logged_in(ClosedPage(), timeout=1)

        class OpenPage:
            def is_closed(self):
                return False

            def wait_for_timeout(self, _ms):
                return None

        clicks: list[int] = []
        with patch("app.browser.scraper.LOGIN_RETRY_GAP", 0), patch(
            "app.browser.scraper.dismiss_announcement_modal", return_value=0
        ), patch("app.browser.scraper.is_logged_in", return_value=False), patch(
            "app.browser.scraper._text_has_login", return_value=True
        ), patch("app.browser.scraper.login_dialog_visible", return_value=False), patch(
            "app.browser.scraper.announcement_modal_visible", return_value=False
        ), patch("app.browser.scraper.click_login", side_effect=lambda _p: clicks.append(1) or True):
            wait_until_logged_in(OpenPage(), timeout=0.05)
        self.assertGreaterEqual(len(clicks), 1)


class HeaderAuthTests(unittest.TestCase):
    def test_top_bar_login_button_means_cookie_is_dead(self):
        from app.browser.scraper import is_logged_in

        class Page:
            def evaluate(self, _script):
                return {"login": True, "watchlist": False, "avatar": False, "ready": True}

        self.assertFalse(is_logged_in(Page()))

    def test_top_bar_watchlist_means_logged_in(self):
        from app.browser.scraper import is_logged_in

        class Page:
            def evaluate(self, _script):
                return {"login": False, "watchlist": True, "avatar": True, "ready": True}

        self.assertTrue(is_logged_in(Page()))

    def test_header_not_ready_is_not_logged_in(self):
        from app.browser.scraper import is_logged_in

        class Page:
            def evaluate(self, _script):
                return {"login": False, "watchlist": False, "avatar": False, "ready": False}

        self.assertFalse(is_logged_in(Page()))

    def test_watchlist_does_not_count_if_login_still_in_header(self):
        from app.browser.scraper import is_logged_in

        class Page:
            def evaluate(self, _script):
                return {"login": True, "watchlist": True, "avatar": False, "ready": True}

        self.assertFalse(is_logged_in(Page()))


class HomeReadyTests(unittest.TestCase):
    def test_home_ready_needs_search_not_just_avatar(self):
        from app.browser.scraper import home_is_ready

        class Trigger:
            def __init__(self, visible: bool) -> None:
                self.visible = visible

            @property
            def first(self):
                return self

            def is_visible(self):
                return self.visible

        class Page:
            def __init__(self, visible: bool) -> None:
                self.visible = visible

            def locator(self, _sel):
                return Trigger(self.visible)

            def evaluate(self, _script):
                return {"login": False, "watchlist": True, "avatar": True, "ready": True}

        self.assertFalse(home_is_ready(Page(False)))
        self.assertTrue(home_is_ready(Page(True)))


class ActivityWheelTests(unittest.TestCase):
    def test_wheel_uses_real_mouse(self):
        from app.browser.scraper import GmgnScraper

        class Mouse:
            def __init__(self) -> None:
                self.moves: list[tuple[float, float]] = []
                self.wheels: list[tuple[int, int]] = []

            def move(self, x, y):
                self.moves.append((x, y))

            def wheel(self, dx, dy):
                self.wheels.append((dx, dy))

        class Page:
            def __init__(self) -> None:
                self.mouse = Mouse()

            def is_closed(self):
                return False

            def evaluate(self, script, *args):
                return True

        class Session:
            def __init__(self, page) -> None:
                self.page = page

        page = Page()
        scraper = GmgnScraper.__new__(GmgnScraper)
        scraper.session = Session(page)
        scraper._scroller_box = lambda: {"x": 10.0, "y": 40.0, "w": 200.0, "h": 400.0}  # type: ignore
        scraper._wheel_activity(900)
        self.assertEqual(page.mouse.wheels, [(0, 900)])
        self.assertTrue(page.mouse.moves)
        x, y = page.mouse.moves[0]
        self.assertGreater(x, 10.0 + 200.0 * 0.5)
        self.assertGreater(y, 40.0 + 400.0 * 0.4)

    def test_wheel_aims_table_center_not_top_stats(self):
        from app.browser.scraper import GmgnScraper

        class Mouse:
            def __init__(self) -> None:
                self.moves: list[tuple[float, float]] = []
                self.wheels: list[tuple[int, int]] = []

            def move(self, x, y):
                self.moves.append((x, y))

            def wheel(self, dx, dy):
                self.wheels.append((dx, dy))

        class Page:
            def __init__(self) -> None:
                self.mouse = Mouse()

            def is_closed(self):
                return False

            def evaluate(self, script, *args):
                return True

        class Session:
            def __init__(self, page) -> None:
                self.page = page

        page = Page()
        scraper = GmgnScraper.__new__(GmgnScraper)
        scraper.session = Session(page)
        scraper._scroller_box = lambda: {"x": 700.0, "y": 80.0, "w": 500.0, "h": 900.0}  # type: ignore
        scraper._wheel_activity(900)
        x, y = page.mouse.moves[0]
        self.assertGreater(x, 900)
        self.assertGreater(y, 80 + 300)

    def test_scroll_until_row_stops_when_list_does_not_move(self):
        from app.browser.scraper import GmgnScraper

        class Page:
            def is_closed(self):
                return False

            def wait_for_timeout(self, _ms):
                return None

            def evaluate(self, script, *args):
                return "1|BABA|BABA|0"

        class Session:
            def __init__(self, page) -> None:
                self.page = page

        wheels: list[int] = []
        events: list[str] = []
        scraper = GmgnScraper.__new__(GmgnScraper)
        scraper.session = Session(Page())
        scraper.cancel_event = None
        scraper.emit = events.append
        scraper._check = lambda: None
        scraper._reveal_activity_table = lambda: None  # type: ignore
        scraper._scroll_activity_top = lambda: None  # type: ignore
        scraper._find_activity_row = lambda _labels: None  # type: ignore
        scraper._wheel_activity = lambda delta: wheels.append(delta)  # type: ignore
        scraper._scroll_until_row(["BABA"], rounds=40)
        self.assertGreater(len(wheels), 4)
        self.assertLess(len(wheels), 20)
        self.assertTrue(any("回滚" in msg for msg in events))
        self.assertTrue(any("不再滚动" in msg for msg in events))

    def test_closed_page_during_scroll_is_collect_interrupt(self):
        from app.browser.exceptions import BrowserSessionError
        from app.browser.scraper import GmgnScraper

        class Closed(Exception):
            pass

        Closed.__name__ = "TargetClosedError"

        class Page:
            def is_closed(self):
                return False

            def wait_for_timeout(self, _ms):
                raise Closed("Page.wait_for_timeout: Target page, context or browser has been closed")

            def evaluate(self, script, *args):
                return "1|x|y|0"

        class Session:
            def __init__(self, page) -> None:
                self.page = page

        scraper = GmgnScraper.__new__(GmgnScraper)
        scraper.session = Session(Page())
        scraper.cancel_event = None
        scraper.emit = lambda _m: None
        scraper._check = lambda: None
        scraper._reveal_activity_table = lambda: None  # type: ignore
        scraper._scroll_activity_top = lambda: None  # type: ignore
        scraper._find_activity_row = lambda _labels: None  # type: ignore
        scraper._wheel_activity = lambda _delta: None  # type: ignore
        with self.assertRaises(BrowserSessionError) as ctx:
            scraper._scroll_until_row(["Xanax"], rounds=5)
        self.assertIn("采集中断", str(ctx.exception))
        self.assertNotIn("登录未完成", str(ctx.exception))

    def test_scroll_until_coverage_waits_for_api_to_catch_up(self):
        """Coverage must not be declared while the DOM has more rows than the API captured."""
        from app.browser.scraper import GmgnScraper

        class Page:
            def __init__(self) -> None:
                self.url = "https://gmgn.ai/sol/address/abc"

            def is_closed(self):
                return False

            def wait_for_timeout(self, _ms):
                return None

        class Session:
            def __init__(self, page) -> None:
                self.page = page

        events: list[str] = []
        scraper = GmgnScraper.__new__(GmgnScraper)
        scraper.session = Session(Page())
        scraper.cancel_event = None
        scraper.emit = events.append
        scraper._check = lambda: None
        scraper._listening = True
        scraper.attach = lambda: None  # type: ignore
        scraper._reveal_activity_table = lambda: None  # type: ignore
        scraper._wheel_activity = lambda _delta: None  # type: ignore
        scraper._unstick_activity_scroll = lambda: None  # type: ignore

        # Simulate API lagging behind DOM: rows grow faster than captured activities.
        # start_ts is large enough that oldest enters the window early, but the API
        # count is still behind the DOM row count. Coverage must NOT fire until the
        # API catches up and stays stable for two rounds.
        call_state = {"i": 0}

        class FakeCapture:
            def activities(self):
                i = call_state["i"]
                call_state["i"] += 1
                phases = [
                    (50, 100),  # round 1: API=50, DOM=100 (lagging)
                    (50, 100),  # round 2: still lagging
                    (100, 100),  # round 3: caught up
                    (100, 100),  # round 4: stable -> covered after 2 confirms
                ]
                count = phases[min(i, len(phases) - 1)][0]
                # timestamp=100 is older than start_ts=500, so the window is "covered"
                return [{"timestamp": 100, "token_address": "x"} for _ in range(count)]

        scraper.capture = FakeCapture()

        def fake_row_count():
            i = call_state["i"] - 1
            return [100, 100, 100, 100][min(i, 3)]

        scraper._row_count = fake_row_count  # type: ignore

        scraper.scroll_until_coverage(start_ts=500, max_rounds=10)
        # Should NOT have stopped at round 1 or 2 (API lagging), only after 2 confirms.
        self.assertTrue(any("接口落后于表格" in m for m in events))
        self.assertTrue(any("已同步" in m or "已覆盖且接口已同步" in m for m in events))

    def test_scroll_keeps_loading_until_22h_rows_for_24h_window(self):
        """A rolling 1-day window must not stop after a few hours of history."""
        from app.browser.scraper import GmgnScraper

        class Page:
            def is_closed(self):
                return False

            def wait_for_timeout(self, _ms):
                return None

        class Session:
            def __init__(self, page) -> None:
                self.page = page

        events: list[str] = []
        scraper = GmgnScraper.__new__(GmgnScraper)
        scraper.session = Session(Page())
        scraper.cancel_event = None
        scraper.emit = events.append
        scraper._check = lambda: None
        scraper._listening = True
        scraper.attach = lambda: None  # type: ignore
        scraper._reveal_activity_table = lambda: None  # type: ignore
        scraper._wheel_activity = lambda _delta: None  # type: ignore
        scraper._unstick_activity_scroll = lambda: None  # type: ignore
        scraper._activity_scan_hint = lambda: {}  # type: ignore

        now = 1_800_000_000
        start_ts = now - 86400
        call_state = {"i": 0}
        phases = [
            [{"timestamp": now - 3600, "symbol": "NEW", "token_address": "MintNew11111111111111111111111111111111"}],
            [{"timestamp": now - 3 * 3600, "symbol": "MID", "token_address": "MintMid11111111111111111111111111111111"}] * 100,
            [{"timestamp": now - 3 * 3600, "symbol": "MID", "token_address": "MintMid11111111111111111111111111111111"}] * 100,
            [{"timestamp": now - 22 * 3600, "symbol": "STRID", "token_address": "MintStrid111111111111111111111111111111"}] * 140,
            [{"timestamp": now - 22 * 3600, "symbol": "STRID", "token_address": "MintStrid111111111111111111111111111111"}] * 140,
            [{"timestamp": now - 25 * 3600, "symbol": "OLD", "token_address": "MintOld11111111111111111111111111111111"}] * 180,
            [{"timestamp": now - 25 * 3600, "symbol": "OLD", "token_address": "MintOld11111111111111111111111111111111"}] * 180,
        ]

        class FakeCapture:
            def activities(self):
                i = call_state["i"]
                call_state["i"] += 1
                return list(phases[min(i, len(phases) - 1)])

        scraper.capture = FakeCapture()

        def fake_row_count():
            i = max(0, call_state["i"] - 1)
            return len(phases[min(i, len(phases) - 1)])

        scraper._row_count = fake_row_count  # type: ignore

        scraper.scroll_until_coverage(start_ts=start_ts, max_rounds=12)
        joined = "\n".join(events)
        self.assertIn("STRID", joined)
        self.assertTrue(any("已覆盖且接口已同步" in m for m in events))
        self.assertFalse(any("已覆盖且接口已同步" in m and "100 条" in m for m in events))


class WindowScrollProgressTests(unittest.TestCase):
    def test_22h_into_24h_window_is_mostly_filled(self):
        from app.browser.scraper import window_scroll_progress

        now = 1_800_000_000
        start_ts = now - 86400
        oldest = now - 22 * 3600
        done, total = window_scroll_progress(start_ts, oldest, 3, 80, now=now)
        self.assertEqual(total, 100)
        self.assertGreaterEqual(done, 90)
        self.assertLess(done, 100)

    def test_past_window_is_complete(self):
        from app.browser.scraper import window_scroll_progress

        now = 1_800_000_000
        done, total = window_scroll_progress(now - 86400, now - 90000, 8, 80, now=now)
        self.assertEqual((done, total), (100, 100))


class ScraperProgressEmitTests(unittest.TestCase):
    def test_emit_forwards_done_total_to_on_event(self):
        from app.browser.scraper import GmgnScraper

        seen: list[tuple] = []

        def on_event(message: str, **extra):
            seen.append((message, extra.get("done"), extra.get("total"), extra.get("stage")))

        scraper = GmgnScraper.__new__(GmgnScraper)
        scraper.on_event = on_event
        scraper._pg_done = 0
        scraper._pg_total = 0
        scraper._pg_token = ""
        scraper._pg_stage = "browser"
        scraper._set_progress(done=5, total=23, token="STRID", stage="browser_token")
        scraper.emit("打开代币详情 STRID（5/23）")
        self.assertEqual(seen[-1], ("打开代币详情 STRID（5/23）", 5, 23, "browser_token"))


class SessionNavTests(unittest.TestCase):
    def test_goto_timeout_is_network_error(self):
        from app.browser.exceptions import BrowserNetworkError
        from app.browser.session import NAV_RETRIES, BrowserSession

        class FakeTimeout(Exception):
            pass

        FakeTimeout.__name__ = "TimeoutError"

        class FakePage:
            def __init__(self) -> None:
                self.calls = 0

            def goto(self, *args, **kwargs):
                self.calls += 1
                raise FakeTimeout("Page.goto: Timeout 20000ms exceeded.")

            def wait_for_timeout(self, _ms):
                return None

        session = BrowserSession.__new__(BrowserSession)
        session.cancel_event = None
        session.emit = lambda _m: None
        session._check = lambda: None
        page = FakePage()
        with self.assertRaises(BrowserNetworkError):
            session._goto_home(page)
        self.assertEqual(page.calls, NAV_RETRIES)

    def test_goto_home_emits_and_uses_short_timeout(self):
        from app.browser.session import NAV_TIMEOUT_MS, BrowserSession

        logs: list[str] = []
        kwargs_seen: list[dict] = []

        class FakePage:
            def goto(self, url, **kwargs):
                kwargs_seen.append(kwargs)

            def wait_for_load_state(self, *_a, **_k):
                return None

            def wait_for_timeout(self, _ms):
                return None

        session = BrowserSession.__new__(BrowserSession)
        session.cancel_event = None
        session.emit = logs.append
        session._check = lambda: None
        session._cancelled = lambda: False  # type: ignore
        session._closed_or_cancelled = lambda _exc: False  # type: ignore
        session._goto_home(FakePage())
        self.assertTrue(any("打开" in msg for msg in logs))
        self.assertEqual(kwargs_seen[0].get("wait_until"), "commit")
        self.assertLessEqual(int(kwargs_seen[0].get("timeout") or 0), NAV_TIMEOUT_MS)

    def test_restore_storage_writes_cookies_and_init_script(self):
        from app.browser.session import BrowserSession

        added: list[dict] = []
        scripts: list[str] = []

        class Ctx:
            def add_cookies(self, cookies):
                added.extend(cookies)

        class Page:
            def add_init_script(self, script):
                scripts.append(script)

        session = BrowserSession.__new__(BrowserSession)
        session.emit = lambda _m: None
        session._restore_storage(
            Ctx(),
            Page(),
            {
                "cookies": [{"name": "sid", "value": "a", "domain": ".gmgn.ai", "path": "/"}],
                "origins": [
                    {
                        "origin": "https://gmgn.ai",
                        "localStorage": [{"name": "token", "value": "abc"}],
                    }
                ],
            },
        )
        self.assertEqual(added[0]["name"], "sid")
        self.assertTrue(any("localStorage.setItem" in script for script in scripts))
        self.assertTrue(any("abc" in script for script in scripts))


class WebClientPagingTests(unittest.TestCase):
    def test_serves_cached_activities_without_browser(self):
        from app.browser.web_client import GmgnWebClient

        client = GmgnWebClient.__new__(GmgnWebClient)
        client.request_count = 0
        client.start_ts = 0
        client.end_ts = 0
        client._wallet_acts = {
            "Wallet111": [
                {"tx_hash": "a", "timestamp": 10, "event_type": "buy", "token_address": "Mint111111111111111111111111111111111111111"},
                {"tx_hash": "b", "timestamp": 9, "event_type": "sell", "token_address": "Mint111111111111111111111111111111111111111"},
            ]
        }
        client._opened = {"Wallet111"}
        client._token_acts = {}
        client._token_opened = set()
        client.prepare_wallet = lambda *a, **k: None  # type: ignore
        page = client.get_wallet_activity("sol", "Wallet111", limit=1)
        self.assertEqual(len(page["data"]["activities"]), 1)
        self.assertEqual(page["data"]["next"], "1")
        page2 = client.get_wallet_activity("sol", "Wallet111", cursor="1", limit=1)
        self.assertEqual(page2["data"]["activities"][0]["tx_hash"], "b")
        self.assertIsNone(page2["data"]["next"])

    def test_wallet_activity_drops_rows_before_start_ts(self):
        from app.browser.web_client import GmgnWebClient

        now = 1_800_000_000
        client = GmgnWebClient.__new__(GmgnWebClient)
        client.request_count = 0
        client.start_ts = now - 86400
        client.end_ts = now
        client._wallet_acts = {
            "Wallet111": [
                {"tx_hash": "in", "timestamp": now - 10 * 3600, "event_type": "buy", "symbol": "STRID"},
                {"tx_hash": "old", "timestamp": now - 36 * 3600, "event_type": "sell", "symbol": "OLD"},
            ]
        }
        client._opened = {"Wallet111"}
        client._token_acts = {}
        client._token_opened = set()
        client.prepare_wallet = lambda *a, **k: None  # type: ignore
        page = client.get_wallet_activity("sol", "Wallet111", limit=50)
        hashes = [row["tx_hash"] for row in page["data"]["activities"]]
        self.assertEqual(hashes, ["in"])

    def test_cookie_mode_analyze_does_not_call_helius_history(self):
        from unittest.mock import MagicMock

        from app.domain.enums import ReportPeriod
        from app.domain.models import AnalysisOptions, WalletAnalysisRequest
        from app.services.wallet_analysis_service import WalletAnalysisService
        from app.storage.database import Database

        client = MagicMock()
        client.cookie_mode = True
        client.on_raw = None
        client.request_count = 0
        client.retry_429_count = 0
        client.error_count = 0
        client.get_wallet_activity.return_value = {"code": 0, "data": {"activities": [], "next": None}}
        client.get_wallet_stats.return_value = {"code": 0, "data": {}}
        client.get_wallet_profits.return_value = {"code": 0, "data": {}}
        orch = MagicMock()
        orch.collect_wallet_swaps.side_effect = AssertionError("automation must not wait on Helius history")
        orch.expand_token_universe.side_effect = AssertionError("automation must not probe Helius universe first")
        orch.batch_metadata.return_value = {}
        orch.batch_pools.return_value = {}
        orch.health_snapshot.return_value = []
        orch.metrics_snapshot.return_value = []
        orch.persist_credits.return_value = None
        orch.last_coverage = None
        service = WalletAnalysisService(client=client, db=Database(), orchestrator=orch, token_workers=1)
        req = WalletAnalysisRequest(
            wallet_address="11111111111111111111111111111111",
            period=ReportPeriod.D7,
            start_time=1,
            end_time=2,
            options=AnalysisOptions(
                collection_mode="automation",
                save_raw_json=False,
                fetch_token_created_at=False,
                fetch_platform_pool=False,
                autofill_missing=False,
            ),
        )
        try:
            service.analyze(req)
        except Exception:
            pass
        client.prepare_wallet.assert_called()
        orch.collect_wallet_swaps.assert_not_called()


class WalletIsolationTests(unittest.TestCase):
    def _scraper(self):
        from app.browser.scraper import CaptureBag, GmgnScraper

        scraper = GmgnScraper.__new__(GmgnScraper)
        scraper.capture = CaptureBag()
        scraper._token_details = {}
        scraper._mint_labels = {}
        scraper._wallet = ""
        scraper._capture_gen = 0
        scraper.on_raw = None
        scraper.emit = lambda message: None
        scraper._close_detail = lambda: None
        return scraper

    def test_reset_drops_previous_wallet_activities(self):
        scraper = self._scraper()
        scraper._wallet = "QGRe9GTHJxG3zZUJxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
        mint = "MintFromWallet1xxxxxxxxxxxxxxxxxxxxxxxxxxxxx1"
        scraper.capture.add(
            "https://gmgn.ai/api/v1/wallet_activity/sol?wallet=QGRe9GTHJxG3zZUJxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
            {"data": {"activities": [{"event_type": "buy", "token_address": mint, "timestamp": 1788738411}]}},
        )
        self.assertEqual(len(scraper.capture.activities()), 1)
        scraper._token_details = {mint: {"opened": True}}
        scraper._mint_labels = {mint: ["SPCXx"]}
        scraper.reset_for_wallet("9P9aAh3kxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx")
        self.assertEqual(scraper.capture.activities(), [])
        self.assertEqual(scraper._token_details, {})
        self.assertEqual(scraper._mint_labels, {})
        self.assertEqual(scraper._capture_gen, 1)

    def test_keep_response_drops_other_wallet_activity(self):
        scraper = self._scraper()
        scraper._wallet = "9P9aAh3kxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
        self.assertFalse(
            scraper._keep_response(
                "https://gmgn.ai/vas/api/v1/wallet_activity/sol?wallet=QGRe9GTHJxG3zZUJxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
            )
        )
        self.assertTrue(
            scraper._keep_response(
                "https://gmgn.ai/vas/api/v1/wallet_activity/sol?wallet=9P9aAh3kxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
            )
        )
        self.assertTrue(scraper._keep_response("https://gmgn.ai/defi/quotation/v1/tokens/sol/SomeMint"))

    def test_unique_tokens_only_from_current_activities(self):
        scraper = self._scraper()
        wallet = "9P9aAh3kxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
        other = "SPCXxMintFromWallet1xxxxxxxxxxxxxxxxxxxxxxx"
        mine = "TokenOnWallet2xxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
        scraper._wallet = wallet
        items = [
            {"event_type": "buy", "token_address": mine, "timestamp": 10},
            {"event_type": "buy", "token_address": wallet, "timestamp": 9},
        ]
        self.assertEqual(scraper.unique_tokens(items), [mine])
        self.assertNotIn(other, scraper.unique_tokens(items))


if __name__ == "__main__":
    unittest.main()
