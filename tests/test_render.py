"""消息渲染与提醒触发的单元测试。"""

from __future__ import annotations

import copy
import unittest
from types import SimpleNamespace

from rainwatch import alerts, render


def fake_cfg():
    cfg = SimpleNamespace(
        rain_mm=[0.1, 10.0, 25.0, 50.0, 100.0, 250.0],
        rain_names=["小雨", "中雨", "大雨", "暴雨", "大暴雨", "特大暴雨"],
        concern_mm=[10.0, 25.0, 50.0, 100.0],
        concern_names=["关注", "显著", "高风险", "极高风险"],
        agreement={"ok_mm": 5.0, "warn_mm": 10.0},
        alerts={"event_min_level": "中雨", "event_jump_mm": 10.0, "max_event_push_per_day": 4},
        general={"forecast_days": 5, "rolling_hours": [24, 72, 120]},
        districts=["从化", "花都", "白云"],
        display_districts=["花都", "白云", "从化"],
        page_title="鸦岗断面周边降雨预报-清积存提醒",
        page_subtitle="（广州市环科院）",
        message_title_prefix="鸦岗断面周边降雨预报",
        event_title_prefix="鸦岗断面周边降雨提醒",
        dashboard_url="https://example.github.io/yagang-rainwatch/",
        repo_url="https://github.com/example/yagang-rainwatch",
    )
    cfg.summary = lambda: "测试配置"
    return cfg


def day(date, mm, level, concern="无"):
    return {"date": date, "mm": mm, "level": level, "concern": concern,
            "spread_mm": 1.0, "max_point": {"name": "测试点", "mm": mm, "level": level},
            "qweather_mm": None, "agreement": "无对比"}


def rolling(hours, mm, level, concern="无"):
    return {"hours": hours, "mm": mm, "level": level, "level_kind": "24h累计",
            "max_daily_level": level, "concern": concern,
            "max_point": {"name": "测试点", "mm": mm}}


def fake_report():
    days = ["2026-11-02", "2026-11-03", "2026-11-04", "2026-11-05", "2026-11-06"]
    return {
        "run_time": "2026-11-01T08:05",
        "timezone": "Asia/Shanghai",
        "days": days,
        "districts": {
            "从化": {
                "points": ["CH-01"],
                "daily": [day(days[0], 30.0, "大雨", "显著"), day(days[1], 2.0, "小雨"),
                          day(days[2], 0.0, "无雨"), day(days[3], 0.0, "无雨"),
                          day(days[4], 1.0, "小雨")],
                "rolling": [rolling(24, 30.0, "大雨", "显著"), rolling(72, 33.0, "中雨"),
                            rolling(120, 34.0, "中雨")],
                "peak": day(days[0], 30.0, "大雨", "显著"),
                "overall_concern": "显著",
            },
            "花都": {
                "points": ["HD-01"],
                "daily": [day(days[0], 8.0, "小雨"), day(days[1], 1.0, "小雨"),
                          day(days[2], 0.0, "无雨"), day(days[3], 0.0, "无雨"),
                          day(days[4], 0.0, "无雨")],
                "rolling": [rolling(24, 8.0, "小雨"), rolling(72, 9.0, "小雨"),
                            rolling(120, 9.0, "小雨")],
                "peak": day(days[0], 8.0, "小雨"),
                "overall_concern": "无",
            },
            "白云": {
                "points": ["BY-03"],
                "daily": [day(days[0], 3.0, "小雨"), day(days[1], 0.0, "无雨"),
                          day(days[2], 0.0, "无雨"), day(days[3], 0.0, "无雨"),
                          day(days[4], 0.0, "无雨")],
                "rolling": [rolling(24, 3.0, "小雨"), rolling(72, 3.0, "小雨"),
                            rolling(120, 3.0, "小雨")],
                "peak": day(days[0], 3.0, "小雨"),
                "overall_concern": "无",
            },
        },
        "points": {
            "BY-03": {"name": "鸦岗", "district": "白云", "channel": "断面本地",
                      "approx": False, "daily": {}, "daily_level": {},
                      "rolling": {24: 3.0, 72: 3.0, 120: 3.0}},
        },
        "warnings": [],
        "sources": {"openmeteo": {"fetched_at": "2026-11-01T08:05", "models": ["ecmwf_ifs025"]},
                    "qweather": None},
        "agreement": {"status": "single", "summary": "单数据源（未配置和风天气）", "counts": {}},
    }


class TestMessage(unittest.TestCase):
    def test_daily_message_contains_key_sections(self):
        cfg = fake_cfg()
        msg = render.make_message(fake_report(), cfg, kind="daily", daily_slot="08")
        self.assertIn("鸦岗断面周边降雨预报", msg["title"])
        for token in ("1/3/5天累计", "从化", "花都", "白云", "关注提示", "官方预警", "查看看板"):
            self.assertIn(token, msg["html"], token)
        self.assertIn("30", msg["html"])
        # 板块顺序：关注提示 → 官方预警 → 逐日预报 → 1/3/5天累计
        html = msg["html"]
        self.assertLess(html.index("关注提示"), html.index("官方预警"))
        self.assertLess(html.index("官方预警"), html.index("逐日预报（08–08时）"))
        self.assertLess(html.index("逐日预报（08–08时）"), html.index("1/3/5天累计"))
        # 区县顺序：花都 → 白云 → 从化
        seg = msg["text"].split("逐日预报（08–08时）：")[1].split("1/3/5天累计")[0]
        self.assertLess(seg.index("花都"), seg.index("白云"))
        self.assertLess(seg.index("白云"), seg.index("从化"))

    def test_advice_mentions_upstream(self):
        cfg = fake_cfg()
        advice = render.build_advice(fake_report(), cfg)
        self.assertIn("从化", advice)
        self.assertIn("来水增加", advice)
        self.assertTrue(("可能" in advice) or ("预计" in advice))

    def test_advice_multiple_items_and_priority(self):
        cfg = fake_cfg()
        report = copy.deepcopy(fake_report())
        # 11-06：白云 12.7mm、从化 14.1mm，均为中雨，两项都应列出
        report["districts"]["白云"]["daily"][4] = day("2026-11-06", 12.7, "中雨", "关注")
        report["districts"]["从化"]["daily"][4] = day("2026-11-06", 14.1, "中雨", "关注")
        advice = render.build_advice(report, cfg)
        self.assertIn("12.7", advice)
        self.assertIn("14.1", advice)
        # 白云（直接上游）排在从化之前
        self.assertLess(advice.index("白云"), advice.index("从化"))
        # 提前 5 天 → 谨慎措辞 + 不确定性说明
        self.assertIn("不确定性", advice)
        self.assertIn("中长期预报", advice)

    def test_message_link_has_cache_buster(self):
        cfg = fake_cfg()
        msg = render.make_message(fake_report(), cfg, kind="daily", daily_slot="08")
        self.assertIn("?v=202611010805", msg["html"])

    def test_dashboard_no_cache_meta(self):
        cfg = fake_cfg()
        html = render.render_dashboard(fake_report(), [], cfg)
        self.assertIn("Cache-Control", html)
        self.assertIn("no-cache", html)

    def test_event_message(self):
        cfg = fake_cfg()
        event = {"type": "upgrade", "key": "k1", "district": "从化",
                 "level": "大雨", "mm": 30.0, "text": "从化 未来24h预报等级升级为大雨（30mm）"}
        msg = render.make_message(fake_report(), cfg, kind="event", events=[event])
        self.assertIn("鸦岗断面周边降雨提醒", msg["title"])
        self.assertIn("变化提醒", msg["html"])

    def test_dashboard_layout(self):
        cfg = fake_cfg()
        html = render.render_dashboard(fake_report(), [], cfg)
        # 标题两行
        self.assertIn("鸦岗断面周边降雨预报-清积存提醒", html)
        self.assertIn("（广州市环科院）", html)
        # 板块顺序：关注提示 → 官方预警 → 逐日预报 → 滚动累计
        self.assertLess(html.index("关注提示"), html.index("官方预警"))
        self.assertLess(html.index("官方预警"), html.index("逐日预报（水文日"))
        self.assertLess(html.index("逐日预报（水文日"), html.index("滚动累计（距当前"))
        # 区县顺序：逐日表头 花都 → 白云 → 从化
        head = html.split("逐日预报（水文日")[1].split("</tr>")[0]
        self.assertLess(head.index("花都"), head.index("白云"))
        self.assertLess(head.index("白云"), head.index("从化"))


class TestEventDetection(unittest.TestCase):
    def test_upgrade_event_and_dedupe(self):
        cfg = fake_cfg()
        report = fake_report()
        state = {"last_run": None, "daily_reports": {}, "event_counts": {},
                 "pushed_events": [], "prev": {}}
        events = alerts.detect_events(cfg, report, state, __import__("datetime").datetime(2026, 11, 1, 8, 5))
        self.assertTrue(any(e["type"] == "upgrade" and e["district"] == "从化" for e in events))

        # 记录推送后，同一事件不再触发
        alerts.record_event_push(state, __import__("datetime").datetime(2026, 11, 1, 8, 5), events)
        again = alerts.detect_events(cfg, report, state, __import__("datetime").datetime(2026, 11, 1, 9, 5))
        self.assertFalse(any(e.get("key") == "upgrade|从化|大雨|2026-11-02" for e in again))

    def test_no_event_when_previous_level_higher(self):
        cfg = fake_cfg()
        report = fake_report()
        state = {"daily_reports": {}, "event_counts": {}, "pushed_events": [],
                 "prev": {"从化": {"rolling24_mm": 40.0, "rolling24_level": "暴雨"}}}
        events = alerts.detect_events(cfg, report, state, __import__("datetime").datetime(2026, 11, 1, 8, 5))
        self.assertFalse(any(e["district"] == "从化" for e in events))


if __name__ == "__main__":
    unittest.main()
