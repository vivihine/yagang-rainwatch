"""中国天气网（官方等级）解析与风险优先逻辑的单元测试。"""

from __future__ import annotations

import unittest
from datetime import datetime
from types import SimpleNamespace

from rainwatch import alerts, compute, render
from rainwatch.sources import chinawx

SAMPLE_HTML = """
<li><h1>13日（今天）</h1><p class="wea">中雨</p></li>
<li><h1>14日（明天）</h1><p class="wea">晴</p></li>
<li><h1>15日（后天）</h1><p class="wea">小雨转大雨</p></li>
<li><h1>16日（周一）</h1><p class="wea">雷阵雨</p></li>
"""


class TestChinaWxParse(unittest.TestCase):
    def test_level_from_text(self):
        self.assertEqual(chinawx.level_from_text("阴转中雨"), "中雨")
        self.assertEqual(chinawx.level_from_text("小雨转大雨"), "大雨")
        self.assertEqual(chinawx.level_from_text("大暴雨"), "大暴雨")
        self.assertEqual(chinawx.level_from_text("晴"), "无雨")
        self.assertEqual(chinawx.level_from_text("雷阵雨"), "小雨")

    def test_parse_forecast(self):
        now = datetime(2026, 11, 13, 22, 0)
        parsed = chinawx.parse_forecast(SAMPLE_HTML, now)
        self.assertEqual(len(parsed), 4)
        self.assertEqual(parsed["2026-11-13"]["level"], "中雨")
        self.assertEqual(parsed["2026-11-15"]["level"], "大雨")
        self.assertEqual(parsed["2026-11-14"]["level"], "无雨")


def fake_cfg():
    points = [
        {"id": "A", "name": "测试点A", "district": "花都", "channel": "测试",
         "approx": False, "lat": 23.40, "lon": 113.22},
        {"id": "B", "name": "测试点B", "district": "白云", "channel": "测试",
         "approx": False, "lat": 23.30, "lon": 113.20},
    ]
    cfg = SimpleNamespace(
        rain_mm=[0.1, 10.0, 25.0, 50.0, 100.0, 250.0],
        rain_names=["小雨", "中雨", "大雨", "暴雨", "大暴雨", "特大暴雨"],
        concern_mm=[10.0, 25.0, 50.0, 100.0],
        concern_names=["关注", "显著", "高风险", "极高风险"],
        agreement={"ok_mm": 5.0, "warn_mm": 10.0},
        alerts={"event_min_level": "中雨", "event_jump_mm": 10.0, "max_event_push_per_day": 4},
        general={"forecast_days": 5, "rolling_hours": [24, 72, 120],
                 "timezone": "Asia/Shanghai"},
        districts=["花都", "白云"],
        display_districts=["花都", "白云"],
        page_title="鸦岗断面周边降雨预报-清积存提醒",
        page_subtitle="（广州市环科院）",
        message_title_prefix="鸦岗断面周边降雨预报",
        event_title_prefix="鸦岗断面周边降雨提醒",
        dashboard_url="",
        repo_url="",
        points=points,
        primary_source="openmeteo",
    )
    cfg.summary = lambda: "测试配置"
    return cfg


def fake_om(now):
    """仅 11-04 水文日（08–08）有雨 24mm，其余为 0。"""
    start = datetime(2026, 11, 3, 0, 0)
    series = {}
    for pid in ("A", "B"):
        pairs = [(start + __import__("datetime").timedelta(hours=i),
                  1.0 if 32 <= i < 56 else 0.0) for i in range(24 * 7)]
        series[pid] = {"ecmwf_ifs025": pairs}
    return {"source": "openmeteo", "fetched_at": now.isoformat(timespec="minutes"),
            "models": ["ecmwf_ifs025"], "series": series}


def fake_official():
    return {
        "enabled": True,
        "fetched_at": "2026-11-03T19:00",
        "errors": [],
        "districts": {
            "花都": {"2026-11-04": {"text": "中雨", "level": "中雨"}},
            "白云": {"2026-11-06": {"text": "大雨", "level": "大雨"}},
        },
    }


class TestRiskPriority(unittest.TestCase):
    def test_official_events_and_divergence(self):
        cfg = fake_cfg()
        now = datetime(2026, 11, 3, 19, 0)
        report = compute.build_report(cfg, now, fake_om(now), None, fake_official())
        events = report.get("official_events") or []
        divergences = report.get("official_divergence") or []
        # 花都 11-04：主源约 24mm（≥中雨）、官方中雨 → 事件
        self.assertTrue(any(e["district"] == "花都" and e["date"] == "2026-11-04" for e in events))
        # 白云 11-06：主源 0mm、官方大雨 → 分歧
        self.assertTrue(any(d["district"] == "白云" and d["level"] == "大雨" for d in divergences))
        advice = render.build_advice(report, cfg)
        self.assertIn("中国天气网官方预报", advice)
        msg = render.make_message(report, cfg, kind="daily")
        self.assertIn("官方预报（中国天气网）", msg["html"])
        html = render.render_dashboard(report, [], cfg)
        self.assertIn("中国天气网（官方等级）", html)

    def test_official_event_triggers_alert(self):
        cfg = fake_cfg()
        now = datetime(2026, 11, 3, 19, 0)
        report = compute.build_report(cfg, now, fake_om(now), None, fake_official())
        state = {"last_run": None, "daily_reports": {}, "event_counts": {},
                 "pushed_events": [], "prev": {}}
        events = alerts.detect_events(cfg, report, state, now)
        self.assertTrue(any(e.get("type") == "official" for e in events))
        # 记录推送后不再重复
        alerts.record_event_push(state, now, events)
        again = alerts.detect_events(cfg, report, state, now)
        self.assertFalse(any(e.get("type") == "official" for e in again))


if __name__ == "__main__":
    unittest.main()
