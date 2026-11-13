"""运行失败提醒（--failure-notify）的单元测试。"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from rainwatch import __main__ as main_mod
from rainwatch import alerts


def fake_cfg(tmp_dir: str):
    return SimpleNamespace(
        message_title_prefix="鸦岗断面周边降雨预报",
        state_path=Path(tmp_dir) / "state.json",
    )


class TestFailureNotify(unittest.TestCase):
    def test_at_most_once_per_day(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = fake_cfg(tmp)
            now = datetime(2026, 11, 13, 17, 0)
            with mock.patch.object(main_mod.notifier, "push",
                                   return_value={"ok": True, "skipped": False}) as push:
                self.assertEqual(main_mod._notify_failure(cfg, now), 0)
                self.assertEqual(push.call_count, 1)
                state = alerts.load_state(cfg.state_path)
                self.assertEqual(state.get("last_failure_notify"), "2026-11-13")

                # 同一天再次失败：不重复提醒
                self.assertEqual(main_mod._notify_failure(cfg, now), 0)
                self.assertEqual(push.call_count, 1)

                # 次日可再次提醒
                next_day = datetime(2026, 11, 14, 9, 0)
                self.assertEqual(main_mod._notify_failure(cfg, next_day), 0)
                self.assertEqual(push.call_count, 2)

    def test_push_failure_does_not_mark_notified(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = fake_cfg(tmp)
            now = datetime(2026, 11, 13, 17, 0)
            with mock.patch.object(main_mod.notifier, "push",
                                   return_value={"ok": False, "skipped": False, "reason": "error"}) as push:
                main_mod._notify_failure(cfg, now)
                self.assertEqual(push.call_count, 1)
                state = alerts.load_state(cfg.state_path)
                self.assertIsNone(state.get("last_failure_notify"))


if __name__ == "__main__":
    unittest.main()
