"""和风天气新版预警接口解析的单元测试。"""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from rainwatch.sources import qweather


def fake_cfg(**overrides):
    base = dict(
        qw={
            "warning_districts": ["从化", "花都", "白云"],
            "warning_points": [],
            "warning_types": ["暴雨"],
            "warning_levels": ["黄色", "橙色", "红色"],
        },
        raw={"district_centers": {"从化": [23.5499, 113.581], "花都": [23.40, 113.22],
                                  "白云": [23.20, 113.27]}},
        points=[
            {"id": "BY-03", "district": "白云", "lat": 23.2204, "lon": 113.1894},
        ],
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def alert(headline="白云区气象台发布暴雨黄色预警信号", type_name="暴雨", color="yellow",
          description="预计未来6小时白云区有暴雨"):
    return {
        "id": "202510310001",
        "headline": headline,
        "description": description,
        "eventType": {"name": type_name, "code": "1003"},
        "color": {"code": color},
        "severity": "moderate",
        "issuedTime": "2026-10-31T22:00+08:00",
        "expireTime": "2026-11-01T10:00+08:00",
    }


class TestWarningPoints(unittest.TestCase):
    def test_from_district_centers(self):
        points = qweather._warning_points(fake_cfg())
        self.assertEqual(len(points), 3)
        self.assertIn("从化", [p[2] for p in points])

    def test_custom_points_dedup(self):
        cfg = fake_cfg(qw={"warning_districts": ["白云"], "warning_points": [[23.20, 113.27]],
                           "warning_types": ["暴雨"], "warning_levels": ["黄色", "橙色", "红色"]})
        points = qweather._warning_points(cfg)
        self.assertEqual(len(points), 1)  # 与白云中心坐标重复，已去重


class TestAlertLevel(unittest.TestCase):
    def test_from_headline(self):
        self.assertEqual(qweather._alert_level(alert()), "黄色")
        self.assertEqual(qweather._alert_level(alert(headline="暴雨红色预警信号", color="red")), "红色")

    def test_from_color_fallback(self):
        self.assertEqual(qweather._alert_level(alert(headline="暴雨预警", color="orange")), "橙色")


class TestMatchWarning(unittest.TestCase):
    def test_matching(self):
        cfg = fake_cfg()
        self.assertTrue(qweather._match_warning(alert(), cfg))

    def test_type_filtered(self):
        cfg = fake_cfg()
        self.assertFalse(qweather._match_warning(alert(headline="大风蓝色预警信号", type_name="大风"), cfg))

    def test_level_filtered(self):
        cfg = fake_cfg()
        self.assertFalse(qweather._match_warning(alert(headline="暴雨蓝色预警信号", color="blue"), cfg))


if __name__ == "__main__":
    unittest.main()
