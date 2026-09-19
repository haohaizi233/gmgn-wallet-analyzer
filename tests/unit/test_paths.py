from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.utils import paths


class PathLayoutTests(unittest.TestCase):
    def test_source_mode_uses_project_root(self):
        root = Path(__file__).resolve().parents[2]
        self.assertEqual(paths.APP_DIR, root)
        self.assertEqual(paths.PROJECT_ROOT, root)
        self.assertFalse(paths.is_frozen())
        self.assertEqual(paths.ENV_PATH, root / ".env")
        self.assertEqual(paths.INPUT_DIR, root / "input")
        self.assertEqual(paths.OUTPUT_DIR, root / "output")

    def test_ensure_runtime_dirs_keeps_existing_env(self):
        paths.ensure_runtime_dirs()
        self.assertTrue(paths.INPUT_DIR.exists())
        self.assertTrue(paths.OUTPUT_DIR.exists())
        self.assertTrue(paths.COOKIE_DIR.exists())
        self.assertTrue(paths.default_wallet_file().exists())
        if paths.ENV_PATH.exists():
            before = paths.ENV_PATH.read_bytes()
            paths.ensure_runtime_dirs()
            self.assertEqual(paths.ENV_PATH.read_bytes(), before)

    def test_wallet_file_roundtrip(self):
        with tempfile.TemporaryDirectory() as raw:
            folder = Path(raw)
            target = folder / "地址.txt"
            written = paths.write_wallet_file("AAA\nBBB\n", target)
            self.assertEqual(written.read_text(encoding="utf-8").strip(), "AAA\nBBB")

    def test_data_file_prefers_override(self):
        with tempfile.TemporaryDirectory() as raw:
            data_dir = Path(raw)
            override = data_dir / "special_assets.json"
            override.write_text("{}", encoding="utf-8")
            with mock.patch.object(paths, "DATA_DIR", data_dir), mock.patch.object(paths, "RESOURCE_DIR", data_dir / "missing"):
                self.assertEqual(paths.data_file("special_assets.json"), override)


if __name__ == "__main__":
    unittest.main()
