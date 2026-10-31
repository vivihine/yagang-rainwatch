"""关键计算的单元测试（标准库 unittest，无需第三方依赖）。

运行：PYTHONPATH=src python -m unittest discover -s tests -v
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from rainwatch import alerts, compute


def fake_cfg(**overrides):
    base = dict(
        rain_mm=[0.1, 10.0, 25.0, 50.0, 100.0, 250.0],
        rain_names=["小雨", "中雨", "大雨", "暴雨", "大暴雨", "特大暴雨"],
        concern_mm=[10.0, 25.0, 50.0, 100.0],
        concern_names=["关注", "显著", "高风险", "极高风险"],
        agreement={"ok_mm": 5.0, "warn_mm": 10.0},
        alerts={"event_min_level": "中雨", "event_jump_mm": 10.0, "max_event_push_per_day": 4},
        general={"forecast_days": 5, "rolling_hours": [24, 72, 120]},
        districts=["从化", "花都", "白云"],
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class TestClassify(unittest.TestCase):
    def setUp(self):
        self.cfg = fake_cfg()

    def test_levels(self):
        cases = [(0.0, "无雨"), (0.05, "无雨"), (0.1, "小雨"), (9.9, "小雨"),
                 (10.0, "中雨"), (24.9, "中雨"), (25.0, "大雨"), (49.9, "大雨"),
                 (50.0, "暴雨"), (100.0, "大暴雨"), (250.0, "特大暴雨")]
        for mm, expected in cases:
            self.assertEqual(compute.level_of(self.cfg, mm), expected, mm)

    def test_concern(self):
        cases = [(9.9, "无"), (10.0, "关注"), (25.0, "显著"),
                 (50.0, "高风险"), (100.0, "极高风险")]
        for mm, expected in cases:
            self.assertEqual(compute.concern_of(self.cfg, mm), expected, mm)

    def test_rank(self):
        self.assertLess(compute.level_rank(self.cfg, "中雨"), compute.level_rank(self.cfg, "大雨"))
        self.assertEqual(compute.level_rank(self.cfg, "不存在"), 0)


class TestWindows(unittest.TestCase):
    def test_report_days_morning(self):
        now = datetime(2026, 11, 1, 7, 0)
        days = compute.report_days(now, 5)
        self.assertEqual(days[0], datetime(2026, 11, 1, 8, 0))
        self.assertEqual(len(days), 5)

    def test_report_days_just_after_8(self):
        now = datetime(2026, 11, 1, 8, 30)
        days = compute.report_days(now, 5)
        self.assertEqual(days[0], datetime(2026, 11, 1, 8, 0))

    def test_report_days_after_window(self):
        now = datetime(2026, 11, 1, 9, 30)
        days = compute.report_days(now, 5)
        self.assertEqual(days[0], datetime(2026, 11, 2, 8, 0))

    def test_hydro_date_boundary(self):
        self.assertEqual(compute._hydro_date(datetime(2026, 11, 2, 7, 59)).isoformat(), "2026-11-01")
        self.assertEqual(compute._hydro_date(datetime(2026, 11, 2, 8, 0)).isoformat(), "2026-11-02")


class TestPointAggregation(unittest.TestCase):
    def test_daily_and_rolling(self):
        start = datetime(2026, 11, 1, 0, 0)
        series = {"ecmwf_ifs025": [], "gfs_seamless": []}
        for i in range(24 * 3):
            t = start + timedelta(hours=i)
            mm = 1.0  # 每小时 1mm
            series["ecmwf_ifs025"].append((t, mm))
            series["gfs_seamless"].append((t, mm + 1.0))
        days = [datetime(2026, 11, 1, 8, 0), datetime(2026, 11, 2, 8, 0)]
        daily = compute._point_daily(series, days)
        # 11-01 08:00 ~ 11-02 08:00 共 24h，ECMWF=24mm、GFS=48mm -> 中位数 36
        self.assertAlmostEqual(daily[datetime(2026, 11, 1).date()]["mm"], 36.0, places=1)
        self.assertAlmostEqual(daily[datetime(2026, 11, 1).date()]["spread"], 24.0, places=1)

        now = datetime(2026, 11, 1, 12, 10)
        rolling = compute._point_rolling(series, now, [24, 72])
        # 从 12:00 起 24 小时：ECMWF=24mm，GFS=48mm -> 36
        self.assertAlmostEqual(rolling[24]["mm"], 36.0, places=1)


class TestAlerts(unittest.TestCase):
    def test_due_daily_slot(self):
        state = alerts.load_state(Path(tempfile.gettempdir()) / "no-such-state.json")
        now = datetime(2026, 11, 1, 7, 0)
        self.assertIsNone(alerts.due_daily_slot(now, state, [8, 19]))
        now = datetime(2026, 11, 1, 8, 5)
        self.assertEqual(alerts.due_daily_slot(now, state, [8, 19]), "08")
        state["daily_reports"]["2026-11-01"]["08"] = "2026-11-01T08:05"
        self.assertEqual(alerts.due_daily_slot(now, state, [8, 19]), "19" if now.hour >= 19 else None)

    def test_no_duplicate_daily_slot(self):
        state = alerts.load_state(Path(tempfile.gettempdir()) / "no-such-state.json")
        now = datetime(2026, 11, 1, 8, 5)
        self.assertEqual(alerts.due_daily_slot(now, state, [8, 19]), "08")
        state["daily_reports"]["2026-11-01"]["08"] = "2026-11-01T08:05"
        self.assertIsNone(alerts.due_daily_slot(now, state, [8, 19]))

    def test_catch_up_prefers_latest_slot(self):
        state = alerts.load_state(Path(tempfile.gettempdir()) / "no-such-state.json")
        now = datetime(2026, 11, 1, 21, 22)
        # 首次部署当天 21 点：补发 19 点晚报，而不是早上漏掉的 08
        self.assertEqual(alerts.due_daily_slot(now, state, [8, 19]), "19")
        state["daily_reports"]["2026-11-01"]["19"] = "2026-11-01T21:22"
        # 08 已过期（超过3小时补发窗口），不再补发
        self.assertIsNone(alerts.due_daily_slot(now, state, [8, 19]))

    def test_catch_up_window(self):
        state = alerts.load_state(Path(tempfile.gettempdir()) / "no-such-state.json")
        # 08 点日报在 10:30 运行时可补发（2.5小时 < 3小时窗口）
        self.assertEqual(alerts.due_daily_slot(datetime(2026, 11, 1, 10, 30), state, [8, 19]), "08")
        # 08 点日报到 12:30 已超过补发窗口
        state2 = alerts.load_state(Path(tempfile.gettempdir()) / "no-such-state.json")
        self.assertIsNone(alerts.due_daily_slot(datetime(2026, 11, 1, 12, 30), state2, [8, 19]))


if __name__ == "__main__":
    unittest.main()
