from __future__ import annotations

import unittest

from app.gui.ui_scheduler import coalesce_messages, plan_tree_patch, row_identity


class CoalesceTests(unittest.TestCase):
    def test_progress_keeps_latest_per_wallet(self):
        batch = [
            {"type": "progress", "wallet": "A", "done": 1},
            {"type": "progress", "wallet": "A", "done": 2},
            {"type": "progress", "wallet": "B", "done": 1},
            {"type": "log", "text": "one"},
            {"type": "log", "text": "two"},
        ]
        out = coalesce_messages(batch)
        self.assertEqual(len(out["progress"]), 2)
        by_wallet = {m["wallet"]: m["done"] for m in out["progress"]}
        self.assertEqual(by_wallet["A"], 2)
        self.assertEqual(by_wallet["B"], 1)
        self.assertEqual(len(out["logs"]), 2)
        self.assertEqual(out["critical"], [])

    def test_wallet_done_drops_stale_progress(self):
        batch = [
            {"type": "progress", "wallet": "A", "done": 3},
            {"type": "wallet_done", "wallet": "A"},
            {"type": "progress", "wallet": "A", "done": 4},
        ]
        out = coalesce_messages(batch)
        self.assertEqual(out["progress"], [])
        self.assertEqual(out["critical"][0]["type"], "wallet_done")

    def test_done_clears_all_progress(self):
        batch = [
            {"type": "progress", "wallet": "A", "done": 1},
            {"type": "done", "reports": []},
        ]
        out = coalesce_messages(batch)
        self.assertEqual(out["progress"], [])
        self.assertEqual(out["critical"][0]["type"], "done")

    def test_log_flush_is_bounded(self):
        batch = [{"type": "log", "text": str(i)} for i in range(80)]
        out = coalesce_messages(batch)
        self.assertEqual(len(out["logs"]), 40)
        self.assertEqual(out["logs"][0]["text"], "40")


class RowPatchPlanTests(unittest.TestCase):
    def test_identity_prefers_stable_id(self):
        self.assertEqual(row_identity({"_id": "mint1", "_copy": "x"}, 9), "mint1")
        self.assertEqual(row_identity({"_copy": "addr"}, 1), "addr")
        self.assertEqual(row_identity({"symbol": "SOL"}, 3), "r3")

    def test_plan_deletes_inserts_and_keeps_order(self):
        plan = plan_tree_patch(["a", "b", "c"], ["c", "d"])
        self.assertEqual(plan["delete"], ["a", "b"])
        self.assertEqual(plan["insert"], ["d"])
        self.assertEqual(plan["order"], ["c", "d"])


class ProgressCountTests(unittest.TestCase):
    def test_browser_logs_without_total_keep_token_bar(self):
        from app.gui.components.progress_panel import coalesce_progress

        done, total, sticky, count, frac = coalesce_progress("打开代币详情 STRID（3/23）", 3, 23, None)
        self.assertEqual((done, total, count), (3, 23, "3 / 23"))
        self.assertAlmostEqual(frac, 3 / 23)
        done, total, sticky, count, frac = coalesce_progress("详情已打开（点名字）", 0, 0, sticky)
        self.assertEqual((done, total, count), (3, 23, "3 / 23"))
        self.assertAlmostEqual(frac, 3 / 23)

    def test_running_without_counts_shows_in_progress_not_preparing(self):
        from app.gui.components.progress_panel import coalesce_progress

        done, total, sticky, count, frac = coalesce_progress("正在打开 GMGN 钱包页", 0, 0, None)
        self.assertEqual(count, "进行中")
        self.assertGreater(frac, 0)
        self.assertIsNone(sticky)

    def test_idle_clears_sticky(self):
        from app.gui.components.progress_panel import coalesce_progress

        _, _, sticky, count, frac = coalesce_progress("已取消", 0, 0, (4, 10))
        self.assertIsNone(sticky)
        self.assertEqual(count, "已取消")
        self.assertEqual(frac, 0)


if __name__ == "__main__":
    unittest.main()
