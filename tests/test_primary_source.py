"""主数据源切换（和风为主 / Open-Meteo 校验）的单元测试。"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace

from rainwatch import compute, render


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
        primary_source="qweather",
    )
    cfg.summary = lambda: "测试配置"
    return cfg


def fake_openmeteo(now):
    """每小时 0.5mm（ECMWF 单模式）→ 每日约 12mm。"""
    start = datetime(2026, 11, 3, 0, 0)
    series = {}
    for pid in ("A", "B"):
        pairs = [(start + timedelta(hours=i), 1.0) for i in range(24 * 7)]
        series[pid] = {"ecmwf_ifs025": pairs}
    return {"source": "openmeteo", "fetched_at": now.isoformat(timespec="minutes"),
            "models": ["ecmwf_ifs025"], "series": series}


def fake_qweather():
    return {
        "source": "qweather",
        "fetched_at": "2026-11-03T19:00",
        "daily": {
            "A": {"2026-11-04": 20.0, "2026-11-05": 5.0, "2026-11-06": 0.0,
                  "2026-11-07": 0.0, "2026-11-08": 0.0},
            "B": {"2026-11-04": 30.0, "2026-11-05": 10.0, "2026-11-06": 0.0,
                  "2026-11-07": 0.0, "2026-11-08": 0.0},
        },
        "warnings": [],
        "errors": [],
    }


class TestRollingFromDaily(unittest.TestCase):
    def test_fractional_overlap(self):
        daily = {"2026-11-03": 24.0, "2026-11-04": 48.0}
        now = datetime(2026, 11, 3, 12, 0)
        # 12h 属于 11-03（24mm×0.5=12）+ 12h 属于 11-04（48mm×0.5=24）= 36
        self.assertAlmostEqual(compute._rolling_from_daily(daily, now, 24), 36.0, places=2)

    def test_missing_day_returns_none(self):
        daily = {"2026-11-03": 10.0}
        now = datetime(2026, 11, 3, 12, 0)
        self.assertIsNone(compute._rolling_from_daily(daily, now, 48))


class TestBuildReportPrimary(unittest.TestCase):
    def test_qweather_is_primary(self):
        cfg = fake_cfg()
        now = datetime(2026, 11, 3, 19, 0)
        report = compute.build_report(cfg, now, fake_openmeteo(now), fake_qweather())
        self.assertEqual(report["primary"], "qweather")
        self.assertEqual(report["primary_label"], "和风天气")
        huadu = report["districts"]["花都"]["daily"][0]
        self.assertEqual(huadu["date"], "2026-11-04")
        self.assertAlmostEqual(huadu["mm"], 20.0, places=1)       # 主值来自和风
        self.assertAlmostEqual(huadu["peer_mm"], 24.0, places=1)  # 校验值为 Open-Meteo
        self.assertIn("主源为和风天气", report["daily_note"])

    def test_fallback_to_openmeteo_when_qweather_missing(self):
        cfg = fake_cfg()
        now = datetime(2026, 11, 3, 19, 0)
        report = compute.build_report(cfg, now, fake_openmeteo(now), None)
        self.assertEqual(report["primary"], "openmeteo")
        huadu = report["districts"]["花都"]["daily"][0]
        self.assertAlmostEqual(huadu["mm"], 24.0, places=1)

    def test_openmeteo_unavailable_still_uses_qweather(self):
        """主源为和风时，Open-Meteo（校验源）挂掉不应导致运行失败。"""
        cfg = fake_cfg()
        now = datetime(2026, 11, 3, 19, 0)
        empty_om = {"series": {}, "models": [], "fetched_at": None}
        report = compute.build_report(cfg, now, empty_om, fake_qweather())
        self.assertEqual(report["primary"], "qweather")
        huadu = report["districts"]["花都"]["daily"][0]
        self.assertAlmostEqual(huadu["mm"], 20.0, places=1)
        self.assertEqual(report["agreement"]["status"], "single")
        self.assertIsNone(huadu["peer_mm"])


class TestRenderPrimary(unittest.TestCase):
    def test_message_and_dashboard_labels(self):
        cfg = fake_cfg()
        now = datetime(2026, 11, 3, 19, 0)
        report = compute.build_report(cfg, now, fake_openmeteo(now), fake_qweather())
        msg = render.make_message(report, cfg, kind="daily")
        self.assertIn("和风天气（主）", msg["html"])
        self.assertIn("Open-Meteo", msg["html"])
        html = render.render_dashboard(report, [], cfg)
        self.assertIn("多源对比（和风天气（主）/ Open-Meteo（校验）", html)
        self.assertIn("主源为和风天气", html)


if __name__ == "__main__":
    unittest.main()
