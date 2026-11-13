"""提醒策略：日报时段判断、事件触发、去重、运行状态持久化。"""

from __future__ import annotations

import copy
import logging
from datetime import datetime, timedelta
from pathlib import Path

from .compute import concern_rank, level_rank
from .config import Config
from .util import iso_minute, read_json, write_json
from .sources.qweather import warning_key

LOG = logging.getLogger("rainwatch.alerts")

STATE_DEFAULTS = {
    "last_run": None,
    "daily_reports": {},   # {"2026-11-01": {"08": "2026-11-01T08:05"}}
    "event_counts": {},    # {"2026-11-01": 2}
    "pushed_events": [],   # [{"key": "...", "ts": "..."}]
    "prev": {},            # {district: {rolling24_mm, rolling24_level, day1_mm, day1_level}}
    "last_failure_notify": None,  # 运行失败提醒的日期（每天最多一次）
}


def load_state(path: str | Path) -> dict:
    state = read_json(Path(path), None) or {}
    merged = copy.deepcopy(STATE_DEFAULTS)
    merged.update(state)
    for key in ("daily_reports", "event_counts", "prev"):
        if not isinstance(merged.get(key), dict):
            merged[key] = {}
    if not isinstance(merged.get("pushed_events"), list):
        merged["pushed_events"] = []
    return merged


def save_state(path: str | Path, state: dict) -> None:
    # 只保留最近7天的日报记录，避免 state 无限膨胀
    daily = state.get("daily_reports") or {}
    cut = (datetime.now() - timedelta(days=7)).date().isoformat()
    state["daily_reports"] = {k: v for k, v in daily.items() if k >= cut}
    write_json(Path(path), state)


def due_daily_slot(now: datetime, state: dict, report_hours: list[int],
                   max_catchup_hours: int = 3) -> str | None:
    """判断当前是否到了（或刚错过不久）某个日报时段，返回未推送的时段标识。

    - 优先补发“最近一个已到点但未推送”的时段（例如 21 点运行补发 19 点晚报）；
    - 超过 max_catchup_hours 的过期时段直接放弃，避免隔半天又补发旧日报。
    """
    today = now.date().isoformat()
    slots = state.setdefault("daily_reports", {}).setdefault(today, {})
    for hour in sorted(report_hours, reverse=True):
        slot = f"{hour:02d}"
        if slot in slots:
            continue
        due_at = now.replace(hour=hour, minute=0, second=0, microsecond=0)
        if now < due_at:
            continue
        if (now - due_at) > timedelta(hours=max_catchup_hours):
            continue
        return slot
    return None


def _pushed_keys(state: dict) -> set[str]:
    return {str(item.get("key")) for item in state.get("pushed_events", []) if item.get("key")}


_OFFICIAL_ORDER = ["无雨", "小雨", "中雨", "大雨", "暴雨", "大暴雨", "特大暴雨"]


def _official_rank(level: str | None) -> int:
    try:
        return _OFFICIAL_ORDER.index(level or "无雨")
    except ValueError:
        return 0


def _official_max(report: dict, district: str) -> str | None:
    """未来 48h 内中国天气网官方等级的最大值。"""
    official = (report.get("chinawx") or {}).get("districts") or {}
    days = (report.get("days") or [])[:2]
    levels = [(official.get(district, {}).get(date) or {}).get("level") for date in days]
    levels = [level for level in levels if level]
    return max(levels, key=_official_rank) if levels else None


def snapshot(report: dict) -> dict:
    """记录本次预报的关键值，供下一次运行判断“是否升级”。"""
    out: dict = {}
    for district, data in report.get("districts", {}).items():
        r24 = next((r for r in data.get("rolling", []) if r.get("hours") == 24), None)
        day1 = next((d for d in data.get("daily", []) if d.get("mm") is not None), None)
        out[district] = {
            "rolling24_mm": r24.get("mm") if r24 else None,
            "rolling24_level": r24.get("level") if r24 else None,
            "day1_mm": day1.get("mm") if day1 else None,
            "day1_level": day1.get("level") if day1 else None,
            "official_level": _official_max(report, district),
        }
    return out


def detect_events(cfg: Config, report: dict, state: dict, now: datetime) -> list[dict]:
    """事件触发：等级升级、预报大幅上调、新增官方预警。"""
    today = now.date().isoformat()
    max_per_day = int(cfg.alerts.get("max_event_push_per_day", 4))
    used = int((state.get("event_counts") or {}).get(today, 0))
    if used >= max_per_day:
        LOG.info("今日事件推送已达上限 %d 次，跳过事件检测", max_per_day)
        return []

    pushed = _pushed_keys(state)
    threshold_rank = level_rank(cfg, str(cfg.alerts.get("event_min_level", "中雨")))
    jump_mm = float(cfg.alerts.get("event_jump_mm", 10.0))
    prev = state.get("prev") or {}
    events: list[dict] = []

    for district, data in report.get("districts", {}).items():
        r24 = next((r for r in data.get("rolling", []) if r.get("hours") == 24), None)
        if not r24 or r24.get("mm") is None:
            continue
        cur_rank = level_rank(cfg, r24.get("level"))
        prev_info = prev.get(district) or {}
        prev_level = prev_info.get("rolling24_level") or "无雨"
        prev_mm = prev_info.get("rolling24_mm")
        prev_rank = level_rank(cfg, prev_level)

        upgraded = cur_rank >= threshold_rank and cur_rank > prev_rank
        jumped = (prev_mm is not None and cur_rank > prev_rank
                  and (r24["mm"] - float(prev_mm)) >= jump_mm)
        if not (upgraded or jumped):
            continue
        key = f"upgrade|{district}|{r24.get('level')}|{report['days'][0]}"
        if key in pushed:
            continue
        reason = "等级升级" if upgraded else "预报上调"
        events.append({
            "type": "upgrade",
            "key": key,
            "district": district,
            "level": r24.get("level"),
            "mm": r24.get("mm"),
            "hours": 24,
            "text": f"{district} 未来24h预报{reason}为{r24.get('level')}（{r24.get('mm')}mm）",
        })

    # 中国天气网官方等级：任一区未来 48h 报中雨以上、且较上次升级 → 事件（防漏报兜底）
    official = (report.get("chinawx") or {}).get("districts") or {}
    for district, data in report.get("districts", {}).items():
        official_days = []
        for date in (report.get("days") or [])[:2]:
            off = official.get(district, {}).get(date) or {}
            level = off.get("level")
            if level and level_rank(cfg, level) >= threshold_rank:
                official_days.append((date, level, off.get("text")))
        if not official_days:
            continue
        date, level, _text = max(official_days, key=lambda item: level_rank(cfg, item[1]))
        prev_info = prev.get(district) or {}
        prev_official = prev_info.get("official_level") or "无雨"
        if level_rank(cfg, level) <= level_rank(cfg, prev_official):
            continue
        key = f"official|{district}|{level}|{date}"
        if key in pushed:
            continue
        day_row = next((d for d in data.get("daily", []) if d.get("date") == date), None)
        mm = day_row.get("mm") if day_row else None
        mm_txt = f"（主源 {mm:g}mm）" if isinstance(mm, (int, float)) else ""
        events.append({
            "type": "official",
            "key": key,
            "district": district,
            "level": level,
            "mm": mm,
            "text": f"中国天气网官方预报：{district} {date[5:]} {level}{mm_txt}",
        })

    for warning in report.get("warnings") or []:
        key = "warn|" + warning_key(warning)
        if key in pushed:
            continue
        events.append({
            "type": "warning",
            "key": key,
            "district": "、".join(warning.get("districts") or []) or "广州市",
            "level": warning.get("level") or "",
            "text": f"官方预警：{warning.get('title')}",
            "warning": warning,
        })

    return events


def record_daily_push(state: dict, now: datetime, slot: str) -> None:
    today = now.date().isoformat()
    state.setdefault("daily_reports", {}).setdefault(today, {})[slot] = iso_minute(now)
    LOG.info("已记录日报推送：%s %s 时段", today, slot)


def record_event_push(state: dict, now: datetime, events: list[dict]) -> None:
    today = now.date().isoformat()
    state.setdefault("event_counts", {})
    state["event_counts"][today] = int(state["event_counts"].get(today, 0)) + 1
    pushed = state.setdefault("pushed_events", [])
    for event in events:
        pushed.append({"key": event.get("key"), "ts": iso_minute(now)})
    # 只保留最近5天的事件键
    cut = (now - timedelta(days=5)).date().isoformat()
    state["pushed_events"] = [item for item in pushed
                              if str(item.get("ts") or "")[:10] >= cut]


def mark_events_notified(state: dict, now: datetime, events: list[dict]) -> None:
    """日报里已经包含的事件标记为已通知，避免下一次运行重复推送。"""
    if not events:
        return
    pushed = state.setdefault("pushed_events", [])
    existing = _pushed_keys(state)
    for event in events:
        key = event.get("key")
        if key and key not in existing:
            pushed.append({"key": key, "ts": iso_minute(now)})


def prune_event_counts(state: dict, now: datetime) -> None:
    cut = (now - timedelta(days=7)).date().isoformat()
    counts = state.get("event_counts") or {}
    state["event_counts"] = {k: v for k, v in counts.items() if k >= cut}

