"""失败记录（--record-failure）的单元测试：只记录，不发 WxPusher。"""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from rainwatch import __main__ as main_mod
from rainwatch import health


def fake_cfg(tmp_dir: str):
    return SimpleNamespace(
        data_dir=Path(tmp_dir),
        state_path=Path(tmp_dir) / "state.json",
    )


class TestRecordFailure(unittest.TestCase):
    def test_records_failure_without_push(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = fake_cfg(tmp)
            now = datetime(2026, 11, 14, 0, 5)
            env = {"GITHUB_RUN_ID": "123", "GITHUB_RUN_NUMBER": "456",
                   "GITHUB_RUN_URL": "https://example/run/123",
                   "GITHUB_EVENT_NAME": "schedule"}
            with mock.patch.object(main_mod.notifier, "push") as push, \
                    mock.patch.dict(os.environ, env):
                self.assertEqual(main_mod._record_failure(cfg, now), 0)
                push.assert_not_called()
            fails = health._load_records(cfg, health.FAIL_LOG)
            runs = health._load_records(cfg, health.RUN_LOG)
            self.assertEqual(len(fails), 1)
            self.assertEqual(fails[0]["run_id"], "123")
            self.assertEqual(fails[0]["run_url"], "https://example/run/123")
            self.assertEqual(len(runs), 1)
            self.assertEqual(runs[0]["status"], "failed")


if __name__ == "__main__":
    unittest.main()
