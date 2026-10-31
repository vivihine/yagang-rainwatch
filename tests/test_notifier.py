"""推送通道选择与消息转换的单元测试。"""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from rainwatch import notifier


def fake_cfg(**overrides):
    base = dict(
        notify={"channel": "auto"},
        pushplus_token="",
        wxpusher_app_token="",
        wxpusher_uid="",
        wxpusher_spt="",
        bark_key="",
        bark_server="https://api.day.app",
        dashboard_url="",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class TestResolveChannel(unittest.TestCase):
    def test_none(self):
        self.assertEqual(notifier.resolve_channel(fake_cfg()), "none")

    def test_wxpusher_standard(self):
        cfg = fake_cfg(wxpusher_app_token="AT_x", wxpusher_uid="UID_x")
        self.assertEqual(notifier.resolve_channel(cfg), "wxpusher")

    def test_wxpusher_spt(self):
        cfg = fake_cfg(wxpusher_spt="SPT_x")
        self.assertEqual(notifier.resolve_channel(cfg), "wxpusher_spt")

    def test_pushplus_fallback(self):
        cfg = fake_cfg(pushplus_token="tok")
        self.assertEqual(notifier.resolve_channel(cfg), "pushplus")

    def test_bark_fallback(self):
        cfg = fake_cfg(bark_key="barkkey")
        self.assertEqual(notifier.resolve_channel(cfg), "bark")

    def test_priority(self):
        cfg = fake_cfg(wxpusher_app_token="AT_x", wxpusher_uid="UID_x",
                       wxpusher_spt="SPT_x", pushplus_token="tok")
        self.assertEqual(notifier.resolve_channel(cfg), "wxpusher")

    def test_explicit_channel(self):
        cfg = fake_cfg(notify={"channel": "pushplus"}, pushplus_token="tok",
                       wxpusher_app_token="AT_x", wxpusher_uid="UID_x")
        self.assertEqual(notifier.resolve_channel(cfg), "pushplus")


class TestPushBehaviour(unittest.TestCase):
    def test_dry_run_skipped(self):
        cfg = fake_cfg(wxpusher_app_token="AT_x", wxpusher_uid="UID_x")
        result = notifier.push(cfg, "测试", "<p>hi</p>", dry_run=True)
        self.assertTrue(result["skipped"])
        self.assertFalse(result["ok"])
        self.assertEqual(result["channel"], "wxpusher")

    def test_no_channel_skipped(self):
        result = notifier.push(fake_cfg(), "测试", "<p>hi</p>")
        self.assertTrue(result["skipped"])
        self.assertEqual(result["reason"], "no-token")

    def test_html_to_text(self):
        text = notifier._html_to_text("<p><b>标题</b><br>第一行</p><p>第二行</p>")
        self.assertIn("标题", text)
        self.assertIn("第一行", text)
        self.assertNotIn("<", text)


if __name__ == "__main__":
    unittest.main()
