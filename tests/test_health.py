"""健康监控与每周邮件报告的单元测试。"""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from rainwatch import health


def fake_cfg(tmp_dir: str):
    cfg = SimpleNamespace(
        data_dir=Path(tmp_dir),
        state_path=Path(tmp_dir) / "state.json",
        general={"daily_report_hours": [8, 19]},
        dashboard_url="https://example.github.io/yagang-rainwatch/",
    )
    cfg.admin_email = "admin@example.com"
    return cfg


class TestWeeklyReport(unittest.TestCase):
    def test_no_failures_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = fake_cfg(tmp)
            now = datetime(2026, 11, 16, 8, 0)  # 周一
            health.record_run(cfg, now - timedelta(hours=2), "daily", {})
            self.assertIsNone(health.weekly_report(cfg, now))

    def test_report_contains_failure_details(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = fake_cfg(tmp)
            now = datetime(2026, 11, 16, 8, 0)
            health.record_run(cfg, now - timedelta(hours=3), "daily",
                              {"openmeteo": 1, "qweather": 0, "chinawx": 0})
            health.record_failure(cfg, now - timedelta(hours=2), {
                "run_id": "999", "run_number": "42",
                "run_url": "https://github.com/x/y/actions/runs/999",
                "event": "schedule", "note": "主流程异常",
            })
            report = health.weekly_report(cfg, now)
            self.assertIsNotNone(report)
            subject, text, html = report
            self.assertIn("1 次运行失败", subject)
            self.assertIn("运行总次数：2", text)
            self.assertIn("失败率 50.0%", text)
            self.assertIn("https://github.com/x/y/actions/runs/999", text)
            self.assertIn("失败明细", html)

    def test_maybe_send_weekly_only_when_failures_and_due(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = fake_cfg(tmp)
            now = datetime(2026, 11, 16, 8, 30)  # 周一 08:30
            state: dict = {}
            # 无故障：不发邮件，标记本周已处理
            with mock.patch.object(health, "send_email") as send:
                result = health.maybe_send_weekly(cfg, now, state)
                send.assert_not_called()
                self.assertTrue(result.get("skipped"))
                self.assertEqual(state.get("weekly_report_sent"), "2026-11-16")

            # 有故障：发送并标记
            state = {}
            health.record_failure(cfg, now - timedelta(hours=1), {"run_number": "7"})
            with mock.patch.object(health, "send_email",
                                   return_value={"ok": True, "channel": "smtp"}) as send:
                result = health.maybe_send_weekly(cfg, now, state)
                self.assertTrue(result.get("ok"))
                self.assertEqual(send.call_count, 1)
                self.assertEqual(state.get("weekly_report_sent"), "2026-11-16")
                # 同日再次运行不重复发送
                health.maybe_send_weekly(cfg, now, state)
                self.assertEqual(send.call_count, 1)

    def test_outside_window_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = fake_cfg(tmp)
            state: dict = {}
            result = health.maybe_send_weekly(cfg, datetime(2026, 11, 16, 15, 0), state)
            self.assertTrue(result.get("skipped"))
            self.assertIsNone(state.get("weekly_report_sent"))

    def test_github_issue_fallback_when_no_email(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = fake_cfg(tmp)
            cfg.admin_email = ""
            now = datetime(2026, 11, 16, 8, 10)
            state: dict = {}
            health.record_failure(cfg, now - timedelta(hours=1), {"run_number": "9"})
            with mock.patch.object(health, "send_email",
                                   return_value={"ok": False, "skipped": True, "reason": "no config"}), \
                    mock.patch.object(health, "send_github_issue",
                                      return_value={"ok": True, "channel": "github_issue"}) as issue:
                result = health.maybe_send_weekly(cfg, now, state)
                self.assertTrue(result.get("ok"))
                self.assertEqual(issue.call_count, 1)
                self.assertEqual(state.get("weekly_report_sent"), "2026-11-16")


class TestGitHubIssue(unittest.TestCase):
    def test_without_env_skipped(self):
        with mock.patch.dict(os.environ, {"GH_TOKEN": "", "GITHUB_TOKEN": "",
                                          "GITHUB_REPOSITORY": ""}):
            result = health.send_github_issue(SimpleNamespace(), "测试", "正文")
        self.assertTrue(result.get("skipped"))

    def test_create_issue(self):
        env = {"GH_TOKEN": "x", "GITHUB_REPOSITORY": "owner/repo"}
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(health, "_github_post",
                                  return_value={"html_url": "https://github.com/owner/repo/issues/1",
                                                "number": 1}) as post:
            result = health.send_github_issue(SimpleNamespace(), "主题", "正文")
        self.assertTrue(result.get("ok"))
        self.assertEqual(result["issue_number"], 1)
        post.assert_called_once()


if __name__ == "__main__":
    unittest.main()
